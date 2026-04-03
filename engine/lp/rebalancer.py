"""LP rebalance orchestration — drives V4LPAdapter through the position lifecycle."""

from typing import TYPE_CHECKING, Callable, Any

import structlog

from engine.db.backend import ActionStoreProtocol, PriceStoreProtocol, VenueConfigStoreProtocol
from engine.lp import strategy

if TYPE_CHECKING:
    from engine.venues.dex.lp_v4 import V4LPAdapter

logger = structlog.get_logger()


class LPRebalancer:
    """Orchestrates DEX LP rebalancing — decoupled from the scheduler."""

    def __init__(
        self,
        broadcast: Callable[[dict], Any],
        price_store: PriceStoreProtocol,
        venue_config_store: VenueConfigStoreProtocol,
        action_store: ActionStoreProtocol,
    ):
        self.broadcast = broadcast
        self._price_store = price_store
        self._venue_config_store = venue_config_store
        self._action_store = action_store

    async def check_and_rebalance(self, venue: "V4LPAdapter") -> None:
        """Check position state; rebalance if out of range past threshold."""
        token_ids = venue.get_owned_positions()
        if not token_ids:
            amount0, amount1 = venue.calculate_mint_amounts()
            if amount0 > 0 or amount1 > 0:
                logger.info("no_position_funds_available_minting", venue=venue.name)
                await self.create_position(venue)
            else:
                logger.debug("no_dex_position_no_funds", venue=venue.name)
            return

        position = venue.get_position_state(token_ids[0])
        if not position:
            return

        if not position.in_range:
            if position.current_price < position.price_lower and position.price_lower > 0:
                distance_pct = float(
                    (position.price_lower - position.current_price)
                    / position.price_lower * 100
                )
            else:
                distance_pct = float(
                    (position.current_price - position.price_upper)
                    / position.price_upper * 100
                )

            threshold = float(venue.params.rebalance_threshold_percent)
            if distance_pct >= threshold:
                logger.info(
                    "position_out_of_range",
                    venue=venue.name,
                    token_id=position.token_id,
                    range_lower=float(position.price_lower),
                    range_upper=float(position.price_upper),
                    current_price=float(position.current_price),
                    distance_pct=round(distance_pct, 2),
                    threshold_pct=threshold,
                )
                await self.rebalance(venue, position.token_id, position)

    async def create_position(self, venue: "V4LPAdapter", recovery_price: float | None = None) -> bool:
        """Fetch price history, compute tick range, balance funds, mint."""
        try:
            prices = await self._price_store.get_recent_prices(limit=100)
            if len(prices) < 10:
                logger.warning("insufficient_price_history", venue=venue.name, count=len(prices))
                return False

            tick_lower, tick_upper = strategy.calculate_tick_range(
                prices, venue.params, venue.config.tick_spacing,
                venue.config.token0_decimals, venue.config.token1_decimals,
                recovery_price=recovery_price, venue_name=venue.name,
            )
            if recovery_price is not None:
                await self._venue_config_store.update_venue_config(venue.name, venue.params.model_dump(mode="json"))

            if await venue.prepare_lp_balance(tick_lower, tick_upper) is False:
                return False
            amount0, amount1 = venue.calculate_mint_amounts()

            if amount0 == 0 and amount1 == 0:
                logger.warning("no_funds_available_for_mint", venue=venue.name)
                return False

            logger.info(
                "creating_dex_position",
                venue=venue.name,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                amount0=amount0,
                amount1=amount1,
            )

            result = await venue.mint_position(
                amount0=amount0,
                amount1=amount1,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
            )

            if result.status == "confirmed":
                logger.info("dex_position_created", venue=venue.name, tx_hash=result.hash)
                self.broadcast({
                    "type": "action",
                    "data": {"venue": venue.name, "action": "position_created", "tx": result.hash},
                })
                await self._action_store.insert_action(
                    venue=venue.name, action_type="mint_position",
                    status="confirmed", tx_hash=result.hash, triggered_by="auto:rebalance",
                )
                return True
            else:
                logger.error("dex_position_creation_failed", venue=venue.name, error=result.error)
                await self._action_store.insert_action(
                    venue=venue.name, action_type="mint_position",
                    status="failed", error=result.error, triggered_by="auto:rebalance",
                )
                return False

        except Exception as e:
            logger.error("create_dex_position_failed", venue=venue.name, error=str(e))
            return False

    async def rebalance(self, venue: "V4LPAdapter", token_id: int, position) -> bool:
        """Remove existing position and recreate with recovery_price."""
        try:
            logger.info("removing_old_position", venue=venue.name, token_id=token_id)
            result = await venue.remove_position(token_id)

            if result.status != "confirmed":
                logger.error("failed_to_remove_position", venue=venue.name, token_id=token_id, error=result.error)
                await self._action_store.insert_action(
                    venue=venue.name, action_type="remove_position",
                    status="failed", error=result.error, triggered_by="auto:rebalance",
                )
                self.broadcast({
                    "type": "alert", "severity": "error",
                    "message": f"{venue.name} position removal failed: {result.error}",
                })
                return False

            await self._action_store.insert_action(
                venue=venue.name, action_type="remove_position",
                status="confirmed", tx_hash=result.hash, triggered_by="auto:rebalance",
            )
            logger.info("old_position_removed", venue=venue.name, token_id=token_id, tx_hash=result.hash)

            recovery_price = float(position.current_price)
            return await self.create_position(venue, recovery_price=recovery_price)

        except Exception as e:
            logger.error("rebalance_dex_position_failed", venue=venue.name, token_id=token_id, error=str(e))
            return False
