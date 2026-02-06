"""Arbitrage opportunity detection using per-venue prices."""

import time
import uuid
from decimal import Decimal
from typing import Optional

import structlog

from engine.api.schemas import ArbitrageParams, ArbitrageOpportunity
from engine.core.venue_prices import VenuePriceAggregator, VenuePrice
from engine.core.price_aggregation import (
    PriceNormalizer,
    BlendedPriceCalculator,
    NormalizedPrice,
    USDT_NGN_VENUES,
    CNGN_USD_VENUES,
    CNGN_NGN_VENUES,
)

logger = structlog.get_logger()


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
    ):
        """
        Initialize arbitrage detector.

        Args:
            price_aggregator: Aggregator that fetches prices from all venues
            params: Arbitrage detection parameters
            normalizer: Shared price normalizer (creates default if None)
            blended_calculator: Optional blended price calculator for
                fair-value based detection
        """
        self.price_aggregator = price_aggregator
        self.params = params
        self.normalizer = normalizer or PriceNormalizer()
        self.blended_calculator = blended_calculator

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
                )
                if opp:
                    opportunities.append(opp)

                opp_reverse = self._check_opportunity(
                    sell_venue,
                    normalized[sell_venue].cngn_usd,
                    buy_venue,
                    normalized[buy_venue].cngn_usd,
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

        # Estimate total fees
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

        # Calculate recommended size and expected profit
        recommended_size = self._calculate_recommended_size(net_spread_bps)
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

    def _estimate_fees(self, buy_venue: str, sell_venue: str) -> int:
        """
        Estimate total fees for a buy/sell pair in basis points.

        Args:
            buy_venue: Venue to buy from
            sell_venue: Venue to sell to

        Returns:
            Estimated total fees in basis points
        """
        total_bps = 0

        # Buy side fees
        if buy_venue in CNGN_USD_VENUES:  # DEX
            total_bps += self.params.dex_swap_fee_bps
            total_bps += self.params.dex_slippage_bps
        elif buy_venue in USDT_NGN_VENUES:  # CEX/P2P
            total_bps += self.params.cex_taker_fee_bps
        # "fair_value" has no execution fees (it's a reference)

        # Sell side fees
        if sell_venue in CNGN_USD_VENUES:  # DEX
            total_bps += self.params.dex_swap_fee_bps
            total_bps += self.params.dex_slippage_bps
        elif sell_venue in USDT_NGN_VENUES:  # CEX/P2P
            total_bps += self.params.cex_taker_fee_bps

        return total_bps

    def _calculate_recommended_size(self, net_spread_bps: int) -> Decimal:
        """
        Calculate recommended trade size in USD.

        Considers:
        - Max single trade limit
        - Higher spread = can trade larger size

        Args:
            net_spread_bps: Net spread after fees

        Returns:
            Recommended trade size in USD
        """
        # Start with max allowed
        size = self.params.max_single_trade_usd

        # Scale down if spread is close to minimum
        spread_buffer = net_spread_bps - self.params.min_net_profit_bps
        if spread_buffer < 50:  # Less than 0.5% buffer
            size = size * Decimal("0.5")
        elif spread_buffer < 100:  # Less than 1% buffer
            size = size * Decimal("0.75")

        return size
