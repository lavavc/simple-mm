"""LP rebalance orchestration — drives V4LPAdapter through the position lifecycle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

import structlog

from engine.db.backend import ActionStoreProtocol, PriceStoreProtocol, VenueConfigStoreProtocol
from engine.lp import strategy
from engine.venues.dex.lp_v4 import LPBalanceSwapResult

if TYPE_CHECKING:
    from engine.venues.dex.lp_v4 import V4LPAdapter

logger = structlog.get_logger()


class LPRebalancer:
    """Orchestrates DEX LP rebalancing — decoupled from the scheduler."""

    def __init__(
        self,
        broadcast: Callable[[dict[str, Any]], Any],
        price_store: PriceStoreProtocol,
        venue_config_store: VenueConfigStoreProtocol,
        action_store: ActionStoreProtocol,
        auto_management_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.broadcast = broadcast
        self._price_store = price_store
        self._venue_config_store = venue_config_store
        self._action_store = action_store
        self._auto_management_enabled = auto_management_enabled or (lambda: True)
        self._venue_locks: dict[str, asyncio.Lock] = {}
        self._active_multi_position_incidents: dict[str, str] = {}

    def _get_venue_lock(self, venue_name: str) -> asyncio.Lock:
        return self._venue_locks.setdefault(venue_name, asyncio.Lock())

    def _auto_actions_allowed(self) -> bool:
        return self._auto_management_enabled()

    @staticmethod
    def _multi_position_incident_key(venue_name: str, token_ids: list[int]) -> str:
        normalized = ",".join(str(token_id) for token_id in sorted(token_ids))
        return f"{venue_name}:{normalized}"

    @staticmethod
    def _pool_source_name(venue: "V4LPAdapter") -> str:
        return f"{venue.name}_pool"

    @staticmethod
    def _token_symbol(venue: "V4LPAdapter", token_address: str) -> str:
        if token_address.lower() == venue.config.token0_address.lower():
            return getattr(venue.config, "token0_symbol", venue.config.token0_address)
        if token_address.lower() == venue.config.token1_address.lower():
            return getattr(venue.config, "token1_symbol", venue.config.token1_address)
        return token_address

    @staticmethod
    def _normalize_amount(raw_amount: int | None, decimals: int) -> float | None:
        if raw_amount is None:
            return None
        return float(raw_amount) / float(10 ** decimals)

    async def check_and_rebalance(self, venue: "V4LPAdapter") -> None:
        """Check position state; rebalance if out of range past threshold."""
        async with self._get_venue_lock(venue.name):
            if not self._auto_actions_allowed():
                logger.info("lp_auto_management_skipped", venue=venue.name, reason="disabled")
                return
            await self._check_and_rebalance_locked(venue)

    async def _check_and_rebalance_locked(self, venue: "V4LPAdapter") -> None:
        """Check position state; rebalance if out of range past threshold."""
        token_ids = venue.get_owned_positions()
        if len(token_ids) > 1:
            incident_key = self._multi_position_incident_key(venue.name, token_ids)
            message = (
                f"{venue.name} has multiple LP positions ({token_ids}); "
                "automatic LP management halted until resolved."
            )
            logger.warning("multiple_lp_positions_halt_auto_management", venue=venue.name, token_ids=token_ids)
            await self._action_store.insert_action(
                venue=venue.name,
                action_type="lp_management_halted",
                status="failed",
                error=message,
                triggered_by="auto:multi_position_halt",
                metadata={"token_ids": token_ids},
                idempotency_key=f"lp_management_halted:{incident_key}",
            )
            if self._active_multi_position_incidents.get(venue.name) != incident_key:
                self.broadcast({"type": "alert", "severity": "warning", "message": message})
            self._active_multi_position_incidents[venue.name] = incident_key
            return
        self._active_multi_position_incidents.pop(venue.name, None)
        if not token_ids:
            amount0, amount1 = venue.calculate_mint_amounts()
            if amount0 > 0 or amount1 > 0:
                logger.info("no_position_funds_available_minting", venue=venue.name)
                await self._create_position_locked(venue, triggered_by="auto:initial_mint")
            else:
                logger.debug("no_dex_position_no_funds", venue=venue.name)
            return

        position = venue.get_position_state(token_ids[0])
        if not position:
            logger.warning(
                "lp_rebalance_skipped",
                venue=venue.name,
                token_id=token_ids[0],
                owned_token_ids=token_ids,
                reason="position_state_unavailable",
            )
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
                await self._rebalance_locked(
                    venue,
                    position.token_id,
                    position,
                    triggered_by="auto:range_exit_rebalance",
                )

    async def create_position(
        self,
        venue: "V4LPAdapter",
        recovery_price: float | None = None,
        triggered_by: str = "auto:initial_mint",
    ) -> bool:
        """Fetch price history, compute tick range, balance funds, mint."""
        async with self._get_venue_lock(venue.name):
            return await self._create_position_locked(
                venue,
                recovery_price=recovery_price,
                triggered_by=triggered_by,
            )

    async def _record_ratio_swap(
        self,
        venue: "V4LPAdapter",
        swap_result: LPBalanceSwapResult,
        *,
        triggered_by: str,
        tick_lower: int,
        tick_upper: int,
    ) -> None:
        token_in_decimals = (
            venue.config.token0_decimals
            if swap_result.token_in.lower() == venue.config.token0_address.lower()
            else venue.config.token1_decimals
        )
        token_out_decimals = (
            venue.config.token0_decimals
            if swap_result.token_out.lower() == venue.config.token0_address.lower()
            else venue.config.token1_decimals
        )
        await self._action_store.insert_action(
            venue=venue.name,
            action_type="lp_ratio_swap",
            triggered_by=triggered_by,
            status=swap_result.tx_result.status,
            direction=swap_result.direction,
            amount_in=self._normalize_amount(swap_result.amount_in_raw, token_in_decimals),
            token_in=self._token_symbol(venue, swap_result.token_in),
            amount_out=self._normalize_amount(swap_result.tx_result.output_raw, token_out_decimals),
            token_out=self._token_symbol(venue, swap_result.token_out),
            tx_hash=swap_result.tx_result.hash or None,
            error=swap_result.tx_result.error,
            metadata={
                "amount_in_raw": swap_result.amount_in_raw,
                "amount_out_raw": swap_result.tx_result.output_raw,
                "min_out_raw": swap_result.min_out_raw,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
            },
        )

    async def _create_position_locked(
        self,
        venue: "V4LPAdapter",
        recovery_price: float | None = None,
        triggered_by: str = "auto:initial_mint",
    ) -> bool:
        """Fetch venue-local price history, compute tick range, balance funds, mint."""
        try:
            prices = await self._price_store.get_recent_prices_for_source(
                self._pool_source_name(venue),
                limit=100,
            )
            if len(prices) < 10:
                logger.warning(
                    "insufficient_price_history",
                    venue=venue.name,
                    source=self._pool_source_name(venue),
                    count=len(prices),
                )
                return False
            if venue.config.tick_spacing is None:
                logger.warning("missing_tick_spacing", venue=venue.name)
                return False

            tick_lower, tick_upper = strategy.calculate_tick_range(
                prices, venue.params, venue.config.tick_spacing,
                venue.config.token0_decimals, venue.config.token1_decimals,
                recovery_price=recovery_price, venue_name=venue.name,
            )
            if recovery_price is not None:
                await self._venue_config_store.update_venue_config(
                    venue.name,
                    venue.params.model_dump(mode="json"),
                )

            prep_result = await venue.prepare_lp_balance(tick_lower, tick_upper)
            if prep_result is not None:
                await self._record_ratio_swap(
                    venue,
                    prep_result,
                    triggered_by=triggered_by,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                )
                if prep_result.tx_result.status != "confirmed":
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
                    venue=venue.name,
                    action_type="mint_position",
                    status="confirmed",
                    tx_hash=result.hash,
                    triggered_by=triggered_by,
                    metadata={
                        "tick_lower": tick_lower,
                        "tick_upper": tick_upper,
                        "amount0_raw": amount0,
                        "amount1_raw": amount1,
                        "recovery_price": recovery_price,
                    },
                )
                return True
            else:
                logger.error("dex_position_creation_failed", venue=venue.name, error=result.error)
                await self._action_store.insert_action(
                    venue=venue.name,
                    action_type="mint_position",
                    status="failed",
                    error=result.error,
                    triggered_by=triggered_by,
                    metadata={
                        "tick_lower": tick_lower,
                        "tick_upper": tick_upper,
                        "amount0_raw": amount0,
                        "amount1_raw": amount1,
                        "recovery_price": recovery_price,
                    },
                )
                return False

        except Exception as e:
            logger.error("create_dex_position_failed", venue=venue.name, error=str(e))
            return False

    async def rebalance(
        self,
        venue: "V4LPAdapter",
        token_id: int,
        position: Any,
        triggered_by: str = "auto:range_exit_rebalance",
    ) -> bool:
        """Remove existing position and recreate with recovery_price."""
        async with self._get_venue_lock(venue.name):
            return await self._rebalance_locked(
                venue,
                token_id,
                position,
                triggered_by=triggered_by,
            )

    async def _rebalance_locked(
        self,
        venue: "V4LPAdapter",
        token_id: int,
        position: Any,
        triggered_by: str,
    ) -> bool:
        """Remove existing position and recreate with recovery_price."""
        try:
            logger.info("removing_old_position", venue=venue.name, token_id=token_id)
            result = await venue.remove_position(token_id)

            if result.status != "confirmed":
                logger.error("failed_to_remove_position", venue=venue.name, token_id=token_id, error=result.error)
                await self._action_store.insert_action(
                    venue=venue.name,
                    action_type="remove_position",
                    status="failed",
                    error=result.error,
                    triggered_by=triggered_by,
                    metadata={"token_id": token_id},
                )
                self.broadcast({
                    "type": "alert", "severity": "error",
                    "message": f"{venue.name} position removal failed: {result.error}",
                })
                return False

            await self._action_store.insert_action(
                venue=venue.name,
                action_type="remove_position",
                status="confirmed",
                tx_hash=result.hash,
                triggered_by=triggered_by,
                metadata={"token_id": token_id},
            )
            logger.info("old_position_removed", venue=venue.name, token_id=token_id, tx_hash=result.hash)

            recovery_price = float(position.current_price)
            return await self._create_position_locked(
                venue,
                recovery_price=recovery_price,
                triggered_by=triggered_by,
            )

        except Exception as e:
            logger.error("rebalance_dex_position_failed", venue=venue.name, token_id=token_id, error=str(e))
            return False

    async def withdraw_positions(
        self,
        venue: "V4LPAdapter",
        *,
        recipient: str | None = None,
        action_type: str = "manual_withdraw",
        triggered_by: str = "manual:withdraw",
    ) -> list[dict[str, Any]]:
        """Remove all positions for a venue under the shared LP lifecycle lock."""
        async with self._get_venue_lock(venue.name):
            results: list[dict[str, Any]] = []
            for token_id in venue.get_owned_positions():
                result = await venue.remove_position(token_id, recipient=recipient)
                metadata: dict[str, Any] = {"token_id": token_id}
                if recipient:
                    metadata["recipient"] = recipient
                await self._action_store.insert_action(
                    venue=venue.name,
                    action_type=action_type,
                    triggered_by=triggered_by,
                    status=result.status,
                    tx_hash=result.hash or None,
                    error=result.error,
                    metadata=metadata,
                )
                results.append(
                    {
                        "token_id": token_id,
                        "status": result.status,
                        "hash": result.hash,
                        "error": result.error,
                    }
                )
                if result.status != "confirmed":
                    self.broadcast(
                        {
                            "type": "alert",
                            "severity": "error",
                            "message": f"{venue.name} LP withdrawal failed for token {token_id}: {result.error}",
                        }
                    )
            return results

    async def unwind_all_positions(
        self,
        venues: list["V4LPAdapter"],
        *,
        triggered_by: str = "system:shutdown_unwind",
    ) -> dict[str, list[dict[str, Any]]]:
        """Unwind all LP positions across venues using the shared lifecycle path."""
        results: dict[str, list[dict[str, Any]]] = {}
        for venue in venues:
            results[venue.name] = await self.withdraw_positions(
                venue,
                action_type="shutdown_unwind",
                triggered_by=triggered_by,
            )
        return results
