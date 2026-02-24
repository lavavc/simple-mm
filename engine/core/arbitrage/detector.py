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
    USDT_NGN_VENUES,
    CNGN_USD_VENUES,
    CNGN_NGN_VENUES,
    VENUE_CHAINS,
    DEX_VENUES,
)

logger = structlog.get_logger()


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
        venue_names = list(normalized.keys())
        for i, buy_venue in enumerate(venue_names):
            for sell_venue in venue_names[i + 1:]:
                # Check both directions
                opp = self._check_opportunity(
                    buy_venue,
                    normalized[buy_venue].cngn_usd,
                    sell_venue,
                    normalized[sell_venue].cngn_usd,
                    reserves=reserves,
                )
                if opp:
                    opportunities.append(opp)

                opp_reverse = self._check_opportunity(
                    sell_venue,
                    normalized[sell_venue].cngn_usd,
                    buy_venue,
                    normalized[buy_venue].cngn_usd,
                    reserves=reserves,
                )
                if opp_reverse:
                    opportunities.append(opp_reverse)

        # --- Strategy 2: Fair-value divergence detection ---
        if self.blended_calculator is not None:
            fair_value_opps = await self._detect_fair_value_divergences(normalized)
            opportunities.extend(fair_value_opps)

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

    async def _detect_fair_value_divergences(
        self,
        normalized: dict[str, NormalizedPrice],
    ) -> list[ArbitrageOpportunity]:
        """Compare each venue against the blended fair value.

        If a venue's price diverges from the TWAP/VWAP by more than
        min_spread_bps, it signals an opportunity to trade that venue
        back toward fair value.

        Returns:
            List of fair-value based opportunities.
        """
        assert self.blended_calculator is not None

        try:
            blended = await self.blended_calculator.get_blended_price()
        except Exception as e:
            logger.warning("blended_price_unavailable_for_arb", error=str(e))
            return []

        fair_value = blended.vwap
        if fair_value <= 0:
            return []

        opportunities = []

        for venue, np in normalized.items():
            venue_price = np.cngn_usd
            if venue_price <= 0:
                continue

            # Check if venue is cheap relative to fair value (buy at venue, "sell" at fair value)
            opp = self._check_opportunity(
                buy_venue=venue,
                buy_price=venue_price,
                sell_venue="fair_value",
                sell_price=fair_value,
            )
            if opp:
                opportunities.append(opp)

            # Check if venue is expensive relative to fair value
            opp_reverse = self._check_opportunity(
                buy_venue="fair_value",
                buy_price=fair_value,
                sell_venue=venue,
                sell_price=venue_price,
            )
            if opp_reverse:
                opportunities.append(opp_reverse)

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

        # Must have positive spread to profit
        if gross_spread_bps < self.params.min_spread_bps:
            return None

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
            buy_price, reserves, buy_venue, sell_venue,
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

        if buy_venue in CNGN_USD_VENUES:  # DEX
            total_bps += self._swap_fee_bps(buy_venue)
        elif buy_venue in USDT_NGN_VENUES:  # CEX/P2P
            total_bps += self.params.cex_taker_fee_bps
        # "fair_value" has no execution fees (it's a reference)

        if sell_venue in CNGN_USD_VENUES:  # DEX
            total_bps += self._swap_fee_bps(sell_venue)
        elif sell_venue in USDT_NGN_VENUES:  # CEX/P2P
            total_bps += self.params.cex_taker_fee_bps

        # Cross-chain DEX pair: add inventory-weighted rebalancing cost
        if (
            buy_venue in DEX_VENUES
            and sell_venue in DEX_VENUES
            and VENUE_CHAINS.get(buy_venue) != VENUE_CHAINS.get(sell_venue)
        ):
            if self.inventory_tracker:
                total_bps += self.inventory_tracker.get_rebalance_cost_bps(buy_venue)
            else:
                total_bps += self.params.cross_chain_rebalance_bps

        return total_bps

    def _calculate_recommended_size(
        self,
        buy_price: Decimal,
        reserves: dict | None,
        buy_venue: str,
        sell_venue: str,
    ) -> Decimal:
        """Optimal USD trade size from pool reserves.

        Returns the profit-maximising size derived from both pools' depth.
        Falls back to max_single_trade_usd when reserve data is unavailable
        (e.g. DEX+CEX pairs where only one side is a constant-product pool).
        """
        if reserves and buy_venue in reserves and sell_venue in reserves:
            cngn_A, stable_A = reserves[buy_venue]
            cngn_B, stable_B = reserves[sell_venue]
            delta = _optimal_cngn_amount(cngn_A, stable_A, cngn_B, stable_B)
            if delta > 0:
                return delta * buy_price
        return self.params.max_single_trade_usd
