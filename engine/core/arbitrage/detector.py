"""Arbitrage opportunity detection using per-venue prices."""

import time
import uuid
from decimal import Decimal
from typing import Optional

import structlog

from engine.api.schemas import ArbitrageParams, ArbitrageOpportunity
from engine.core.venue_prices import VenuePriceAggregator, VenuePrice
from engine.core.arbitrage.inventory import InventoryTracker
from engine.core.price_aggregation import (
    PriceNormalizer,
    BlendedPriceCalculator,
    NormalizedPrice,
    CNGN_USD_PAIRS,
)
from engine.core.arbitrage.pool_state import (
    get_cached_pool_state,
    seed_pool_states,
    v3_swap_token0_for_token1,
    v3_swap_token1_for_token0,
    Q96,
)

logger = structlog.get_logger()

_NON_TRADEABLE_VENUES = frozenset({"bybit", "blockradar", "assetchain"})


def _cex_fill_price(np_price: NormalizedPrice, side: str) -> Decimal:
    """For CEX venues: ask when buying cNGN, bid when selling cNGN.

    Only applies to CNGN_USD_PAIRS where bid/ask are already in cNGN/USD units.
    Falls back to mid if bid/ask are zero or out of range.
    """
    if np_price.basis not in CNGN_USD_PAIRS:
        return np_price.cngn_usd
    quote = np_price.raw_quote
    raw = quote.ask if side == "buy" else quote.bid
    if Decimal("0") < raw < Decimal("1"):
        return raw
    return np_price.cngn_usd


def _optimal_cngn_amount(
    cngn_A: Decimal, stable_A: Decimal,
    cngn_B: Decimal, stable_B: Decimal,
) -> Decimal:
    """Closed-form profit-maximising cNGN trade size between two constant-product pools."""
    k_A = cngn_A * stable_A
    k_B = cngn_B * stable_B
    if k_A <= 0 or k_B <= 0:
        return Decimal("0")
    sqrt_kA = k_A.sqrt()
    sqrt_kB = k_B.sqrt()
    return max(Decimal("0"), (sqrt_kB * cngn_A - sqrt_kA * cngn_B) / (sqrt_kA + sqrt_kB))


def _optimal_cex_dex_amount(
    cngn_pool: Decimal,
    stable_pool: Decimal,
    cex_price: Decimal,
    fee_rate: Decimal,
) -> Decimal:
    """Closed-form optimal cNGN size when one leg is flat-price CEX, other is CPMM pool.

    Works for both directions:
      DEX-buy + CEX-sell: P_eff = cex * (1 - fee)
      CEX-buy + DEX-sell: P_eff = cex * (1 + fee)
    Sign convention: always returns a positive amount to trade.
    """
    k = cngn_pool * stable_pool
    if k <= 0 or cex_price <= 0:
        return Decimal("0")
    p_eff = cex_price * (1 - fee_rate)
    if p_eff <= 0:
        return Decimal("0")
    return max(Decimal("0"), (k / p_eff).sqrt() - cngn_pool)


class ArbitrageDetector:
    """
    Detects arbitrage opportunities by comparing prices across venues.

    Each venue reports its own price. The detector:
    1. Uses PriceNormalizer to convert all prices to a common cNGN/USD basis
    2. Compares all venue pairs for price divergences
    3. Optionally compares each venue against the blended "fair value"
    4. Calculates whether an arb would be profitable after fees
    """

    def __init__(
        self,
        price_aggregator: VenuePriceAggregator,
        params: ArbitrageParams,
        normalizer: PriceNormalizer | None = None,
        blended_calculator: BlendedPriceCalculator | None = None,
        inventory_tracker: InventoryTracker | None = None,
        dex_venues: dict | None = None,
    ):
        """
        Initialize arbitrage detector.

        Args:
            price_aggregator: Aggregator that fetches prices from all venues
            params: Arbitrage detection parameters
            normalizer: Shared price normalizer (creates default if None)
            blended_calculator: Optional blended price calculator for
                fair-value based detection
            dex_venues: Dict of DEX venue name to adapter (for reserve fetching)
        """
        self.price_aggregator = price_aggregator
        self.params = params
        self.normalizer = normalizer or PriceNormalizer()
        self.blended_calculator = blended_calculator
        self.inventory_tracker = inventory_tracker
        self.dex_venues = dex_venues or {}

    async def detect_opportunities(self) -> list[ArbitrageOpportunity]:
        """
        Scan all venue pairs for arbitrage opportunities.

        Returns:
            List of detected opportunities that meet threshold criteria
        """
        # Fetch all venue prices in parallel
        venue_prices = await self.price_aggregator.fetch_all()

        # Normalize all prices to cNGN/USD using shared normalizer
        normalized = self.normalizer.normalize(venue_prices)

        if len(normalized) < 2:
            logger.debug("insufficient_venues_for_arbitrage", count=len(normalized))
            return []

        reserves: dict[str, tuple[Decimal, Decimal]] = {}
        for name, venue in self.dex_venues.items():
            r = venue.get_virtual_reserves()
            if r:
                reserves[name] = r

        opportunities = []

        # --- Strategy 1: Pairwise venue comparison ---
        venue_names = [v for v in normalized.keys() if v not in _NON_TRADEABLE_VENUES]
        for i, buy_venue in enumerate(venue_names):
            for sell_venue in venue_names[i + 1:]:
                # Skip DEX-to-DEX pairs already handled by high-fidelity Simulator
                if buy_venue in self.dex_venues and sell_venue in self.dex_venues:
                    continue

                # Use ask when buying from CEX, bid when selling to CEX; mid for DEX
                buy_is_cex = buy_venue not in self.dex_venues
                sell_is_cex = sell_venue not in self.dex_venues

                opp = self._check_opportunity(
                    buy_venue,
                    _cex_fill_price(normalized[buy_venue], "buy") if buy_is_cex else normalized[buy_venue].cngn_usd,
                    sell_venue,
                    _cex_fill_price(normalized[sell_venue], "sell") if sell_is_cex else normalized[sell_venue].cngn_usd,
                    reserves=reserves,
                )
                if opp:
                    opportunities.append(opp)

                opp_reverse = self._check_opportunity(
                    sell_venue,
                    _cex_fill_price(normalized[sell_venue], "buy") if sell_is_cex else normalized[sell_venue].cngn_usd,
                    buy_venue,
                    _cex_fill_price(normalized[buy_venue], "sell") if buy_is_cex else normalized[buy_venue].cngn_usd,
                    reserves=reserves,
                )
                if opp_reverse:
                    opportunities.append(opp_reverse)

        # Sort by expected profit descending
        opportunities.sort(key=lambda x: x.expected_profit_usd, reverse=True)

        if opportunities:
            logger.info(
                "arbitrage_opportunities_detected",
                count=len(opportunities),
                best_spread_bps=opportunities[0].gross_spread_bps,
                best_profit_usd=float(opportunities[0].expected_profit_usd),
            )

        return opportunities

    def _check_opportunity(
        self,
        buy_venue: str,
        buy_price: Decimal,
        sell_venue: str,
        sell_price: Decimal,
        reserves: dict | None = None,
    ) -> Optional[ArbitrageOpportunity]:
        """
        Check if buying at buy_price and selling at sell_price is profitable.

        For arbitrage to work:
        - Buy where cNGN is CHEAP (low cNGN/USD price)
        - Sell where cNGN is EXPENSIVE (high cNGN/USD price)

        Args:
            buy_venue: Venue to buy from
            buy_price: Price in cNGN/USD (lower is better for buying)
            sell_venue: Venue to sell to
            sell_price: Price in cNGN/USD (higher is better for selling)

        Returns:
            ArbitrageOpportunity if profitable, None otherwise
        """
        if buy_price <= 0:
            return None

        # Calculate gross spread in basis points
        # Spread = (sell - buy) / buy * 10000
        gross_spread_bps = int(
            (sell_price - buy_price) / buy_price * 10000
        )

        total_fees_bps = self._estimate_fees(buy_venue, sell_venue)

        net_spread_bps = gross_spread_bps - total_fees_bps

        if net_spread_bps < self.params.min_net_profit_bps:
            logger.debug(
                "opportunity_rejected_low_net_profit",
                buy_venue=buy_venue,
                sell_venue=sell_venue,
                gross_bps=gross_spread_bps,
                fees_bps=total_fees_bps,
                net_bps=net_spread_bps,
            )
            return None

        recommended_size = self._calculate_recommended_size(
            buy_price, sell_price, reserves, buy_venue, sell_venue,
        )
        expected_profit = recommended_size * Decimal(net_spread_bps) / Decimal("10000")

        return ArbitrageOpportunity(
            id=str(uuid.uuid4()),
            timestamp=int(time.time() * 1000),
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            buy_price=buy_price,
            sell_price=sell_price,
            gross_spread_bps=gross_spread_bps,
            net_spread_bps=net_spread_bps,
            recommended_size_usd=recommended_size,
            expected_profit_usd=expected_profit,
            status="detected",
        )

    def _swap_fee_bps(self, venue: str) -> int:
        """Per-venue swap fee: reads from on-chain adapter if available, else global fallback."""
        if venue in self.dex_venues:
            return self.dex_venues[venue].get_fee_bps(self.params.dex_swap_fee_bps)
        return self.params.dex_swap_fee_bps

    def _estimate_fees(self, buy_venue: str, sell_venue: str) -> int:
        """Estimate total fees for a buy/sell pair in basis points.

        DEX swap fees are read from the pool contract via get_fee_bps().
        Price impact is not included here — it is captured in trade sizing
        via _optimal_cngn_amount when both venues have on-chain reserve data.
        """
        total_bps = 0

        if buy_venue in self.dex_venues:
            total_bps += self._swap_fee_bps(buy_venue)
        else:  # CEX / off-chain
            total_bps += self.params.cex_taker_fee_bps

        if sell_venue in self.dex_venues:
            total_bps += self._swap_fee_bps(sell_venue)
        else:  # CEX / off-chain
            total_bps += self.params.cex_taker_fee_bps

        # Cross-chain DEX pair: add inventory-weighted rebalancing cost
        buy_dex = self.dex_venues.get(buy_venue)
        sell_dex = self.dex_venues.get(sell_venue)
        if (
            buy_dex is not None
            and sell_dex is not None
            and buy_dex.config.chain_id != sell_dex.config.chain_id
        ):
            if self.inventory_tracker:
                total_bps += self.inventory_tracker.get_rebalance_cost_bps(buy_venue)
            else:
                total_bps += self.params.cross_chain_rebalance_bps

        return total_bps

    def _calculate_recommended_size(
        self,
        buy_price: Decimal,
        sell_price: Decimal,
        reserves: dict | None,
        buy_venue: str,
        sell_venue: str,
    ) -> Decimal:
        """Optimal USD trade size from pool reserves.

        Returns the profit-maximising size derived from pool depth.
        Falls back to max_single_trade_usd when reserve data is unavailable.
        """
        # AMM<>AMM: both sides have reserves
        if reserves and buy_venue in reserves and sell_venue in reserves:
            cngn_A, stable_A = reserves[buy_venue]
            cngn_B, stable_B = reserves[sell_venue]
            delta = _optimal_cngn_amount(cngn_A, stable_A, cngn_B, stable_B)
            if delta > 0:
                return delta * buy_price

        # CEX<>DEX: one flat-price side, one AMM
        if reserves:
            buy_is_dex = buy_venue in self.dex_venues and buy_venue in reserves
            sell_is_dex = sell_venue in self.dex_venues and sell_venue in reserves
            if buy_is_dex and sell_venue not in self.dex_venues:
                # Buy DEX, sell CEX — P_eff uses sell (CEX) price with fee
                cngn_pool, stable_pool = reserves[buy_venue]
                fee_rate = Decimal(self._estimate_fees(buy_venue, sell_venue)) / Decimal("10000")
                delta = _optimal_cex_dex_amount(cngn_pool, stable_pool, sell_price, fee_rate)
                if delta > 0:
                    return delta * buy_price
            elif sell_is_dex and buy_venue not in self.dex_venues:
                # Buy CEX, sell DEX — use buy (CEX) price
                cngn_pool, stable_pool = reserves[sell_venue]
                fee_rate = Decimal(self._estimate_fees(buy_venue, sell_venue)) / Decimal("10000")
                delta = _optimal_cex_dex_amount(cngn_pool, stable_pool, buy_price, fee_rate)
                if delta > 0:
                    return delta * buy_price

        return self.params.max_single_trade_usd


async def generate_v3_profit_curve() -> dict:
    """Generates the side-by-side exact V3 curve data over a set of investment sizes from CACHED memory."""
    from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, uni_bsc_b0, uni_bsc_b1, uni_bsc_ts, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, uni_base_b0, uni_base_b1, uni_base_ts, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)
    asset_sqrt, asset_liq, asset_b0, asset_b1, asset_ts, asset_fee = get_cached_pool_state(ASSETCHAIN_POOL_READ_CONFIG.pool_address)

    # Hard-block on execution venue fees (uni-bsc + uni-base).
    missing_execution_fees = [
        name for name, fee in [("uni-bsc", uni_bsc_fee), ("uni-base", uni_base_fee)]
        if fee is None
    ]
    if missing_execution_fees:
        logger.error(
            "v3_profit_curve_blocked_missing_fees",
            pools=missing_execution_fees,
            note="arb curve generation aborted — re-seed will be attempted",
        )
        import asyncio
        asyncio.create_task(seed_pool_states())
        return {}

    if not uni_bsc_sqrt or not uni_base_sqrt:
        logger.warning("v3_profit_curve_cache_miss_aborting_calc")
        import asyncio
        asyncio.create_task(seed_pool_states())
        return {}

    # Prices (USD per cNGN)
    uni_bsc_raw = ((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    uni_bsc_price_usd = float(Decimal(1) / uni_bsc_raw)

    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    asset_price_usd = float(Decimal(1) / (((asset_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6)))) if asset_sqrt else None

    uni_bsc_stable, uni_bsc_cngn = uni_bsc_b0, uni_bsc_b1  # token0=USDT, token1=cNGN
    uni_base_cngn, uni_base_stable = uni_base_b0, uni_base_b1  # token0=cNGN, token1=USDC
    asset_stable, asset_cngn = asset_b0, asset_b1

    # Minimum pool balance gate — block if either execution venue is critically thin.
    MIN_POOL_STABLE_USD = Decimal("500")
    thin_pools = [
        name for name, bal in [("uni-bsc", uni_bsc_stable), ("uni-base", uni_base_stable)]
        if bal is None or bal < MIN_POOL_STABLE_USD
    ]
    if thin_pools:
        logger.warning("v3_profit_curve_blocked_thin_pools", pools=thin_pools, min_usd=float(MIN_POOL_STABLE_USD))
        return {}

    # Per-vector USD cap from pool balances:
    # V1 (buy BSC cNGN, sell into Base): limited by cNGN available in BSC pool and USDC in Base pool.
    # V2 (buy Base cNGN, sell into BSC): limited by cNGN available in Base pool and USDT in BSC pool.
    ABSOLUTE_MAX_USD = Decimal("15000")
    max_usd_v1 = min(uni_bsc_cngn * Decimal(str(uni_bsc_price_usd)), uni_base_stable, ABSOLUTE_MAX_USD)
    max_usd_v2 = min(uni_base_cngn * Decimal(str(uni_base_price_usd)), uni_bsc_stable, ABSOLUTE_MAX_USD)

    # --------------------------------------------------------------------------------
    # PHASE 1: FAST EXECUTION SIGNAL DISCOVERY
    # Find the peak profit size instantly before wasting CPU on plotting visual curves.
    # --------------------------------------------------------------------------------
    best_profit = Decimal("-999999")
    best_size = Decimal("0")
    best_dir = None
    best_cngn = Decimal("0")
    usd_out_expected = Decimal("0")
    best_spread_bps = 0

    step = 10
    # DELTA BALANCING VECTOR 1: Buy on uni-bsc, sell identical cNGN from uni-base inventory
    for size in range(10, int(max_usd_v1) + step, step):
        usd_in_bsc = Decimal(size)
        cngn_acquired_bsc = v3_swap_token0_for_token1(usd_in_bsc, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        usd_out_base = v3_swap_token0_for_token1(cngn_acquired_bsc, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        if usd_out_base - usd_in_bsc > best_profit:
            best_profit = usd_out_base - usd_in_bsc
            best_size = usd_in_bsc
            best_dir = "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE"
            best_cngn = cngn_acquired_bsc
            usd_out_expected = usd_out_base

    # DELTA BALANCING VECTOR 2: Buy on uni-base, sell identical cNGN from uni-bsc inventory
    for size in range(10, int(max_usd_v2) + step, step):
        usd_in_base = Decimal(size)
        cngn_acquired_base = v3_swap_token1_for_token0(usd_in_base, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        usd_out_bsc = v3_swap_token1_for_token0(cngn_acquired_base, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        if usd_out_bsc - usd_in_base > best_profit:
            best_profit = usd_out_bsc - usd_in_base
            best_size = usd_in_base
            best_dir = "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE"
            best_cngn = cngn_acquired_base
            usd_out_expected = usd_out_bsc

    if best_size > 0:
        best_spread_bps = int(((usd_out_expected - best_size) / best_size) * 10000)

    # Future Execution Hook:
    # if best_profit > MIN_PROFIT_THRESHOLD and best_dir:
    #     asyncio.create_task(execute_arbitrage({ ... payloads ... }))
    
    # --------------------------------------------------------------------------------
    # PHASE 2: VISUAL CURVE GENERATION
    # Generate high-resolution curve data for the dashboard UI.
    # --------------------------------------------------------------------------------
    # Cap at $1000 max trade size per user request, maximizing detail in the crucial area.
    test_sizes = list(range(1, 1001))

    curve = []
    for size in test_sizes:
        investment_usd = Decimal(str(size))

        if best_dir == "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE":
            cngn_acquired = v3_swap_token1_for_token0(investment_usd, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
            cngn_acquired_no_fee = v3_swap_token1_for_token0(investment_usd, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)
            usd_returned = v3_swap_token1_for_token0(cngn_acquired, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
            usd_returned_no_fee = v3_swap_token1_for_token0(cngn_acquired_no_fee, uni_bsc_sqrt, uni_bsc_liq, Decimal(0), 18, 6)
        else:
            cngn_acquired = v3_swap_token0_for_token1(investment_usd, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
            cngn_acquired_no_fee = v3_swap_token0_for_token1(investment_usd, uni_bsc_sqrt, uni_bsc_liq, Decimal(0), 18, 6)
            usd_returned = v3_swap_token0_for_token1(cngn_acquired, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
            usd_returned_no_fee = v3_swap_token0_for_token1(cngn_acquired_no_fee, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)

        slippage_tolerance = Decimal("0.0010")
        min_usd_acceptable = usd_returned * (Decimal("1") - slippage_tolerance)

        curve.append({
            "size": size,
            "cngn_acquired": float(cngn_acquired),
            "profit": float(usd_returned - investment_usd),
            "profit_no_fee": float(usd_returned_no_fee - investment_usd),
            "profit_after_slippage": float(min_usd_acceptable - investment_usd),
            "min_acceptable_usd": float(min_usd_acceptable)
        })

    uni_bsc_fee_bps = int(uni_bsc_fee * 10000) if uni_bsc_fee else 0
    uni_base_fee_bps = int(uni_base_fee * 10000) if uni_base_fee else 0

    return {
        "timestamp": int(time.time() * 1000),
        "prices": {
            "uni-bsc": uni_bsc_price_usd,
            "uni-base": uni_base_price_usd,
            "assetchain": asset_price_usd,
        },
        "stats": {
            "uni_bsc_liquidity_cngn_raw": str(uni_bsc_liq),
            "uni_base_liquidity_cngn_raw": str(uni_base_liq),
            "assetchain_liquidity_cngn_raw": str(asset_liq),
            "uni_bsc_stable": float(uni_bsc_stable or 0),
            "uni_bsc_cngn": float(uni_bsc_cngn or 0),
            "uni_base_stable": float(uni_base_stable or 0),
            "uni_base_cngn": float(uni_base_cngn or 0),
            "assetchain_stable": float(asset_stable or 0),
            "assetchain_cngn": float(asset_cngn or 0),
            "uni_bsc_ts": float(uni_bsc_ts or 0),
            "uni_base_ts": float(uni_base_ts or 0),
            "assetchain_ts": float(asset_ts or 0),
        },
        "curve": curve,
        "optimal_arb": {
            "direction": best_dir,
            "optimal_size_usd": float(best_size),
            "expected_profit_usd": float(best_profit),
            "cngn_transferred": float(best_cngn),
            "expected_usd_out": float(usd_out_expected),
            "net_spread_bps": best_spread_bps,
            "slippage_tolerance_bps": 10,
            "uni_bsc_fee_bps": uni_bsc_fee_bps,
            "uni_base_fee_bps": uni_base_fee_bps,
            "assetchain_fee_bps": 30,
            "estimated_gas_usd": 0.07
        }
    }
