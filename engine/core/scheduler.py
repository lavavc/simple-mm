"""Trading scheduler and orchestrator using APScheduler."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Any, TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import structlog

from engine.core.venue_prices import VenuePriceAggregator
from engine.core.price_aggregation import BlendedPriceCalculator
from engine.db import get_db
from engine.venues.base import VenueAdapter
from engine.venues.dex.base import BaseDexAdapter

if TYPE_CHECKING:
    from engine.core.arbitrage import ArbitrageEngine
    from engine.core.accounts import AccountManager

logger = structlog.get_logger()


@dataclass
class SchedulerConfig:
    """Configuration for scheduler intervals (in seconds)."""

    price_update_interval: int = 30
    position_sync_interval: int = 60
    dex_check_interval: int = 120
    cex_sync_interval: int = 300
    rate_sync_interval: int = 300
    rebalance_check_interval: int = 120
    arbitrage_scan_interval: int = 30
    balance_check_interval: int = 300
    portfolio_delta_interval: int = 120

    # Portfolio delta management
    target_delta_ratio: Decimal = Decimal("0.5")  # 50/50 cNGN/USD
    delta_alert_threshold_percent: Decimal = Decimal("10.0")

    # Venue-vs-fair-value divergence threshold for LP rebalance (basis points)
    venue_divergence_rebalance_bps: int = 200


class TradingScheduler:
    """Orchestrates all automated trading tasks.

    Manages scheduled jobs for:
    - Price feed updates (all venues in parallel)
    - Position synchronization
    - DEX rebalancing (tick-range + venue-vs-fair-value divergence)
    - CEX order ladder syncing
    - Blockradar rate syncing
    - Arbitrage opportunity scanning
    - Portfolio delta monitoring
    """

    def __init__(
        self,
        price_aggregator: VenuePriceAggregator,
        venues: dict[str, VenueAdapter],
        config: SchedulerConfig,
        broadcast: Callable[[dict], Any],
        blended_calculator: BlendedPriceCalculator | None = None,
        arbitrage_engine: "ArbitrageEngine | None" = None,
        account_manager: "AccountManager | None" = None,
        token_contracts: dict[str, str] | None = None,
    ):
        self.price_aggregator = price_aggregator
        self.venues = venues
        self.config = config
        self.broadcast = broadcast
        self.blended_calculator = blended_calculator
        self.arbitrage_engine = arbitrage_engine
        self.account_manager = account_manager
        self.token_contracts = token_contracts or {}

        self.scheduler = AsyncIOScheduler()
        self._trading_enabled = True
        self._started = False

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    def start(self):
        """Start all scheduled jobs."""
        if self._started:
            return

        self.scheduler.add_job(
            self._update_price,
            IntervalTrigger(seconds=self.config.price_update_interval),
            id="price_update",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._sync_positions,
            IntervalTrigger(seconds=self.config.position_sync_interval),
            id="position_sync",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._check_dex_rebalance,
            IntervalTrigger(seconds=self.config.dex_check_interval),
            id="dex_rebalance",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._sync_cex_orders,
            IntervalTrigger(seconds=self.config.cex_sync_interval),
            id="cex_sync",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._sync_blockradar_rates,
            IntervalTrigger(seconds=self.config.rate_sync_interval),
            id="rate_sync",
            replace_existing=True,
        )

        if self.arbitrage_engine:
            self.scheduler.add_job(
                self._check_arbitrage,
                IntervalTrigger(seconds=self.config.arbitrage_scan_interval),
                id="arbitrage_scan",
                replace_existing=True,
            )
            logger.info("arbitrage_scan_job_registered")

        if self.account_manager:
            self.scheduler.add_job(
                self._check_balances,
                IntervalTrigger(seconds=self.config.balance_check_interval),
                id="balance_check",
                replace_existing=True,
            )
            logger.info("balance_check_job_registered")

        if self.blended_calculator:
            self.scheduler.add_job(
                self._check_portfolio_delta,
                IntervalTrigger(seconds=self.config.portfolio_delta_interval),
                id="portfolio_delta",
                replace_existing=True,
            )
            logger.info("portfolio_delta_job_registered")

        self.scheduler.start()
        self._started = True
        logger.info("scheduler_started")

    def stop(self):
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            logger.info("scheduler_stopped")

    async def pause(self):
        self._trading_enabled = False
        db = await get_db()
        await db.set_system_state("trading_enabled", "false")
        self.broadcast({"type": "system", "status": "paused"})
        logger.info("trading_paused")

    async def resume(self):
        self._trading_enabled = True
        db = await get_db()
        await db.set_system_state("trading_enabled", "true")
        self.broadcast({"type": "system", "status": "running"})
        logger.info("trading_resumed")

    # ------------------------------------------------------------------
    # Price updates
    # ------------------------------------------------------------------

    async def _update_price(self):
        """Fetch prices from all venues and broadcast."""
        try:
            venue_prices = await self.price_aggregator.fetch_all()

            prices_data = []
            for venue, price in venue_prices.items():
                prices_data.append({
                    "venue": price.venue,
                    "pair": price.pair,
                    "quote": price.quote.model_dump() if price.quote else None,
                    "error": price.error,
                    "age_seconds": price.age_seconds,
                })

                if price.quote:
                    db = await get_db()
                    await db.insert_price_snapshot(price.quote)

            self.broadcast({"type": "venue_prices", "data": prices_data})

            valid_count = sum(1 for p in venue_prices.values() if p.is_valid)
            logger.debug(
                "venue_prices_updated",
                total=len(venue_prices),
                valid=valid_count,
            )

        except Exception as e:
            logger.error("price_update_failed", error=str(e))
            self.broadcast({
                "type": "alert",
                "severity": "warning",
                "message": f"Price fetch error: {e}",
            })

    # ------------------------------------------------------------------
    # Position sync
    # ------------------------------------------------------------------

    async def _sync_positions(self):
        positions = []
        db = await get_db()

        for name, venue in self.venues.items():
            try:
                pos = await venue.get_position()
                positions.append(pos)
                await db.insert_position(pos)
            except Exception as e:
                logger.error("position_sync_failed", venue=name, error=str(e))

        self.broadcast({
            "type": "positions",
            "data": [p.model_dump() for p in positions],
        })

    # ------------------------------------------------------------------
    # DEX rebalance (tick-range + venue-vs-fair-value divergence)
    # ------------------------------------------------------------------

    async def _check_dex_rebalance(self):
        """Check if DEX positions need rebalancing.

        Two checks:
        1. Is the LP position still in-range?
        2. Has the venue's price drifted from blended fair value?

        Check 2 catches situations where the LP is technically in-range
        but the pool price has moved away from fair value, indicating the
        position is being arbed against and should be repositioned.
        """
        if not self._trading_enabled:
            return

        blended = None
        if self.blended_calculator:
            try:
                blended = await self.blended_calculator.get_blended_price()
            except Exception as e:
                logger.warning("blended_price_unavailable_for_rebalance", error=str(e))

        for name in ["aerodrome", "pancakeswap"]:
            if name not in self.venues:
                continue

            venue = self.venues[name]
            if not isinstance(venue, BaseDexAdapter):
                continue

            if venue.paused:
                continue

            try:
                token_ids = venue.get_owned_positions()
                if not token_ids:
                    logger.debug("no_dex_position", venue=name)
                    continue

                position = venue.get_position_state(token_ids[0])
                if not position:
                    continue

                needs_rebalance = False
                rebalance_reason = ""

                # Check 1: tick range
                if not position.in_range:
                    needs_rebalance = True
                    rebalance_reason = "position_out_of_range"
                    logger.info(
                        "position_out_of_range",
                        venue=name,
                        token_id=position.token_id,
                        range_lower=float(position.price_lower),
                        range_upper=float(position.price_upper),
                    )

                # Check 2: venue price vs blended fair value
                if not needs_rebalance and blended and blended.vwap > 0:
                    venue_price = blended.venue_prices.get(name)
                    if venue_price and venue_price > 0:
                        divergence_bps = int(
                            abs(venue_price - blended.vwap)
                            / blended.vwap
                            * 10000
                        )
                        if divergence_bps > self.config.venue_divergence_rebalance_bps:
                            needs_rebalance = True
                            rebalance_reason = "venue_diverged_from_fair_value"
                            logger.info(
                                "venue_price_diverged",
                                venue=name,
                                venue_price=float(venue_price),
                                fair_value=float(blended.vwap),
                                divergence_bps=divergence_bps,
                                threshold_bps=self.config.venue_divergence_rebalance_bps,
                            )

                if needs_rebalance:
                    self.broadcast({
                        "type": "alert",
                        "severity": "warning",
                        "message": f"{name} needs rebalancing: {rebalance_reason}",
                    })
                    await self._rebalance_dex_position(venue, position.token_id)

            except Exception as e:
                logger.error("dex_rebalance_check_failed", venue=name, error=str(e))

    # ------------------------------------------------------------------
    # CEX order ladder sync
    # ------------------------------------------------------------------

    async def _sync_cex_orders(self):
        if not self._trading_enabled:
            return

        quidax = self.venues.get("quidax")
        if not quidax or quidax.paused:
            return

        try:
            reference_price = await self._get_reference_price_ngn()
            if reference_price:
                await quidax.sync_order_ladder(reference_price)
        except Exception as e:
            logger.error("cex_sync_failed", error=str(e))

    # ------------------------------------------------------------------
    # Blockradar rate sync
    # ------------------------------------------------------------------

    async def _sync_blockradar_rates(self):
        if not self._trading_enabled:
            return

        blockradar = self.venues.get("blockradar")
        if not blockradar or blockradar.paused:
            return

        try:
            reference_price = await self._get_reference_price_ngn()
            if reference_price:
                await blockradar.sync_rates(reference_price)
        except Exception as e:
            logger.error("blockradar_sync_failed", error=str(e))

    # ------------------------------------------------------------------
    # Portfolio delta monitoring
    # ------------------------------------------------------------------

    async def _check_portfolio_delta(self):
        """Monitor portfolio delta-neutrality using blended price."""
        if not self.blended_calculator:
            return

        try:
            blended = await self.blended_calculator.get_blended_price()
            if blended.vwap <= 0:
                logger.warning("blended_vwap_zero_for_delta_check")
                return

            total_cngn = Decimal("0")
            total_usdt = Decimal("0")
            total_usdc = Decimal("0")

            for name, venue in self.venues.items():
                try:
                    pos = await venue.get_position()
                    total_cngn += pos.balances.get("cngn", Decimal("0"))
                    total_usdt += pos.balances.get("usdt", Decimal("0"))
                    total_usdc += pos.balances.get("usdc", Decimal("0"))
                except Exception as e:
                    logger.warning("position_fetch_failed_delta", venue=name, error=str(e))

            cngn_usd_value = total_cngn * blended.vwap
            total_stable_usd = total_usdt + total_usdc
            total_usd_value = cngn_usd_value + total_stable_usd

            if total_usd_value <= 0:
                return

            delta_ratio = cngn_usd_value / total_usd_value
            target = self.config.target_delta_ratio
            deviation_percent = (
                abs(delta_ratio - target) / target * 100 if target > 0 else Decimal("0")
            )

            self.broadcast({
                "type": "portfolio_delta",
                "data": {
                    "total_cngn": float(total_cngn),
                    "total_usdt": float(total_usdt),
                    "total_usdc": float(total_usdc),
                    "cngn_usd_value": float(cngn_usd_value),
                    "total_usd_value": float(total_usd_value),
                    "delta_ratio": float(delta_ratio),
                    "target_delta": float(target),
                    "deviation_percent": float(deviation_percent),
                    "blended_vwap": float(blended.vwap),
                    "blended_twap_5m": float(blended.twap_5m),
                    "blended_twap_1h": float(blended.twap_1h),
                    "confidence": blended.confidence,
                },
            })

            logger.info(
                "portfolio_delta_checked",
                delta_ratio=float(delta_ratio),
                target=float(target),
                deviation_percent=float(deviation_percent),
                total_usd=float(total_usd_value),
                blended_vwap=float(blended.vwap),
            )

            if deviation_percent > self.config.delta_alert_threshold_percent:
                direction = "overweight cNGN" if delta_ratio > target else "underweight cNGN"
                msg = (
                    f"Portfolio delta {float(delta_ratio):.1%} deviates "
                    f"{float(deviation_percent):.1f}% from target {float(target):.1%} "
                    f"({direction})"
                )
                logger.warning("portfolio_delta_alert", message=msg)

                db = await get_db()
                await db.insert_alert(
                    severity="warning",
                    category="delta",
                    message=msg,
                )

                self.broadcast({
                    "type": "alert",
                    "severity": "warning",
                    "message": msg,
                })

        except Exception as e:
            logger.error("portfolio_delta_check_failed", error=str(e))

    # ------------------------------------------------------------------
    # Reference price
    # ------------------------------------------------------------------

    async def _get_reference_price_ngn(self) -> Decimal | None:
        """Get reference USDT/NGN price for CEX and rate syncing.

        Uses blended VWAP (cNGN/USD) converted to NGN, falling back to
        the best available single-venue USDT/NGN quote.
        """
        if self.blended_calculator:
            try:
                blended = await self.blended_calculator.get_blended_price()
                if blended.vwap > 0:
                    return blended.reference_price_ngn
            except Exception as e:
                logger.warning("blended_reference_fallback", error=str(e))

        # Fallback: Bybit reports USDT/NGN directly
        bybit = self.price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return bybit.quote.mid

        # Quidax reports cNGN/USDT, invert to get USDT/NGN (cNGN ≈ NGN)
        quidax = self.price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return Decimal("1") / quidax.quote.mid

        return None

    # ------------------------------------------------------------------
    # DEX position management
    # ------------------------------------------------------------------

    async def _create_dex_position(self, venue: BaseDexAdapter) -> bool:
        """Create a new DEX LP position using capital allocation settings."""
        db = await get_db()

        try:
            prices = await db.get_recent_prices(limit=100)
            if len(prices) < 10:
                logger.warning(
                    "insufficient_price_history",
                    venue=venue.name,
                    count=len(prices),
                )
                return False

            tick_lower, tick_upper = venue.calculate_tick_range(prices)

            reference_price_usd = None
            if self.blended_calculator:
                try:
                    blended = await self.blended_calculator.get_blended_price()
                    if blended.vwap > 0:
                        reference_price_usd = blended.vwap
                except Exception:
                    pass

            if reference_price_usd is None:
                ref_ngn = await self._get_reference_price_ngn()
                if ref_ngn and ref_ngn > 0:
                    reference_price_usd = Decimal("1") / ref_ngn

            amount0, amount1 = venue.calculate_mint_amounts(reference_price_usd)

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
                    "data": {
                        "venue": venue.name,
                        "action": "position_created",
                        "tx": result.hash,
                    },
                })
                await db.insert_action(
                    venue=venue.name,
                    action_type="mint_position",
                    status="confirmed",
                    tx_hash=result.hash,
                    triggered_by="auto:rebalance",
                )
                return True
            else:
                logger.error(
                    "dex_position_creation_failed",
                    venue=venue.name,
                    error=result.error,
                )
                await db.insert_action(
                    venue=venue.name,
                    action_type="mint_position",
                    status="failed",
                    error=result.error,
                    triggered_by="auto:rebalance",
                )
                return False

        except Exception as e:
            logger.error("create_dex_position_failed", venue=venue.name, error=str(e))
            return False

    async def _rebalance_dex_position(self, venue: BaseDexAdapter, token_id: int) -> bool:
        """Rebalance a DEX position by removing old and creating new."""
        db = await get_db()

        try:
            logger.info("removing_old_position", venue=venue.name, token_id=token_id)

            result = await venue.remove_position(token_id)

            if result.status != "confirmed":
                logger.error(
                    "failed_to_remove_position",
                    venue=venue.name,
                    token_id=token_id,
                    error=result.error,
                )
                await db.insert_action(
                    venue=venue.name,
                    action_type="remove_position",
                    status="failed",
                    error=result.error,
                    triggered_by="auto:rebalance",
                )
                return False

            await db.insert_action(
                venue=venue.name,
                action_type="remove_position",
                status="confirmed",
                tx_hash=result.hash,
                triggered_by="auto:rebalance",
            )

            logger.info(
                "old_position_removed",
                venue=venue.name,
                token_id=token_id,
                tx_hash=result.hash,
            )

            return await self._create_dex_position(venue)

        except Exception as e:
            logger.error(
                "rebalance_dex_position_failed",
                venue=venue.name,
                token_id=token_id,
                error=str(e),
            )
            return False

    # ------------------------------------------------------------------
    # Arbitrage scanning
    # ------------------------------------------------------------------

    async def _check_arbitrage(self):
        if not self.arbitrage_engine or not self.arbitrage_engine.enabled:
            return

        try:
            opportunities = await self.arbitrage_engine.scan()
            if opportunities:
                logger.info(
                    "arbitrage_scan_complete",
                    opportunities_found=len(opportunities),
                )
        except Exception as e:
            logger.error("arbitrage_scan_failed", error=str(e))

    # ------------------------------------------------------------------
    # Account balance monitoring
    # ------------------------------------------------------------------

    async def _check_balances(self):
        if not self.account_manager:
            return

        try:
            balances = await self.account_manager.check_all_balances(self.token_contracts)
            db = await get_db()

            for balance in balances:
                if balance.needs_refill:
                    logger.warning(
                        "account_needs_refill",
                        role=balance.role,
                        address=balance.address,
                        reasons=balance.refill_reasons,
                    )

                    await db.insert_alert(
                        severity="warning",
                        category="refill",
                        message=f"Account {balance.role} needs refill: {', '.join(balance.refill_reasons)}",
                    )

                    self.broadcast({
                        "type": "refill_alert",
                        "data": {
                            "role": balance.role,
                            "address": balance.address,
                            "chain_id": balance.chain_id,
                            "native_balance": float(balance.native_balance),
                            "token_balances": {k: float(v) for k, v in balance.token_balances.items()},
                            "reasons": balance.refill_reasons,
                        },
                    })

            self.broadcast({
                "type": "account_balances",
                "data": [
                    {
                        "role": b.role,
                        "address": b.address,
                        "chain_id": b.chain_id,
                        "native_balance": float(b.native_balance),
                        "native_symbol": b.native_symbol,
                        "token_balances": {k: float(v) for k, v in b.token_balances.items()},
                        "needs_refill": b.needs_refill,
                    }
                    for b in balances
                ],
            })

        except Exception as e:
            logger.error("balance_check_failed", error=str(e))
