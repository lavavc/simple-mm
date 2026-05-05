"""Tier 1 fair price: multi-factor VWAP with recency, spread, volume, and liquidity weights.

Provides:
- MarketFairPrice: blended cNGN/USD price with per-venue weight diagnostics
- VarianceTracker: online EWMA variance of MarketFairPrice log-returns (for Tier 3)
- MarketFairPriceCalculator: computes MarketFairPrice from already-fetched venue data
"""

import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog

from engine.market.price_aggregation import NormalizedPrice, FAIR_VALUE_EXCLUDED
from engine.market.venue_prices import VenuePrice
from engine.market.pool_state import get_cached_pool_state

logger = structlog.get_logger()


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class MarketFairPrice:
    price: Decimal            # cNGN/USD blended price
    weights: dict[str, Decimal]  # venue → normalized weight (for diagnostics)
    confidence: float         # 0–0.9, same formula as BlendedPriceCalculator
    timestamp: int            # ms since epoch


# =============================================================================
# VarianceTracker
# =============================================================================


class VarianceTracker:
    """Online EWMA variance of MarketFairPrice log-returns.

    Used by StrategyPriceCalculator to compute the Avellaneda-Stoikov
    inventory skew. Separate from the per-venue LP range-width EWMA.
    """

    def __init__(self, lam: float = 0.975, floor_bps: float = 3.0):
        self._lam = lam
        self._floor = (floor_bps / 10_000) ** 2
        self._var: float = self._floor
        self._last_price: float | None = None

    def update(self, market_price: float) -> float:
        """Update with new MarketFairPrice; return current σ²."""
        if self._last_price is not None and self._last_price > 0:
            r = math.log(market_price / self._last_price)
            # Jump filter: skip if |r| > 3σ (likely data artifact, not real vol)
            if abs(r) <= 3.0 * math.sqrt(self._var):
                self._var = self._lam * self._var + (1 - self._lam) * r * r
        self._last_price = market_price
        return self.sigma_sq

    @property
    def sigma_sq(self) -> float:
        return max(self._var, self._floor)

    @property
    def sigma(self) -> float:
        return math.sqrt(self.sigma_sq)


# =============================================================================
# MarketFairPriceCalculator
# =============================================================================


class MarketFairPriceCalculator:
    """Multi-factor VWAP: volume × liquidity × spread_quality × recency per venue."""

    def __init__(
        self,
        lam_recency: float = 0.008,
        pool_addresses: Optional[dict[str, str]] = None,
    ):
        self._lam_recency = lam_recency
        self._variance_tracker = VarianceTracker()

        if pool_addresses is not None:
            self._pool_addresses = pool_addresses
        else:
            from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
            from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
            self._pool_addresses = {
                "uni-base": UNISWAP_BASE_POOL_READ_CONFIG.pool_address,
                "uni-bsc": UNISWAP_BSC_POOL_READ_CONFIG.pool_address,
            }

    @property
    def variance_tracker(self) -> VarianceTracker:
        return self._variance_tracker

    def _recency_weight(self, age_seconds: float) -> float:
        return math.exp(-self._lam_recency * age_seconds)

    def _spread_quality(
        self,
        venue: str,
        np: NormalizedPrice,
        vp: VenuePrice,
    ) -> float:
        """1 / max(spread_bps, 1.0). For DEX point prices, use fee as proxy."""
        quote = np.raw_quote
        mid = float(quote.mid)
        bid = float(quote.bid)
        ask = float(quote.ask)

        if mid <= 0:
            return 1.0 / 10.0  # fallback: 10 bps

        if bid == ask:
            # DEX sqrtPrice is a single point — use pool fee as spread proxy
            pool_addr = self._pool_addresses.get(venue)
            fee_decimal: Optional[Decimal] = None
            if pool_addr:
                _, _, _, fee_decimal = get_cached_pool_state(pool_addr)

            if fee_decimal is not None and fee_decimal > 0:
                # fee is stored as a fraction (e.g. 0.0015 = 15 bps)
                spread_bps = float(fee_decimal) * 10_000.0
            else:
                spread_bps = 10.0  # default fallback

            return 1.0 / max(spread_bps, 1.0)

        spread_bps = (ask - bid) / mid * 10_000.0
        return 1.0 / max(spread_bps, 1.0)

    def _volume_weight(self, np: NormalizedPrice) -> float:
        if np.volume_24h_usd is not None:
            return float(np.volume_24h_usd)
        return 1.0

    def _liquidity_weight(self, venue: str, np: NormalizedPrice) -> float:
        pool_addr = self._pool_addresses.get(venue)
        if pool_addr:
            _, liquidity, _, _ = get_cached_pool_state(pool_addr)
            if liquidity is not None:
                return float(liquidity)
            return 1.0
        # CEX (quidax): use 1% of daily volume as depth proxy
        if np.volume_24h_usd is not None:
            return float(np.volume_24h_usd) * 0.01
        return 1.0

    def compute(
        self,
        normalized_prices: dict[str, NormalizedPrice],
        venue_prices: dict[str, VenuePrice],
    ) -> MarketFairPrice:
        """Compute MarketFairPrice from already-fetched, already-normalized venue prices."""
        # Non-excluded venues that were attempted
        total_venues = sum(
            1 for v in normalized_prices if v not in FAIR_VALUE_EXCLUDED
        )
        # Also count venues that are in venue_prices but not in normalized_prices
        # (they failed normalization and count as missing for confidence)
        all_attempted = {v for v in venue_prices if v not in FAIR_VALUE_EXCLUDED}
        total_venues = len(all_attempted)

        raw_weights: dict[str, float] = {}
        contributing_venues: list[str] = []

        for venue, np in normalized_prices.items():
            if venue in FAIR_VALUE_EXCLUDED:
                continue

            vp = venue_prices.get(venue)
            if vp is None:
                continue

            age = vp.age_seconds
            recency = self._recency_weight(age)
            spread_q = self._spread_quality(venue, np, vp)
            vol_w = self._volume_weight(np)
            liq_w = self._liquidity_weight(venue, np)

            raw = vol_w * liq_w * spread_q * recency
            raw_weights[venue] = raw
            contributing_venues.append(venue)

        total_raw = sum(raw_weights.values())
        if total_raw == 0.0:
            raise ValueError("No valid venues for MarketFairPrice computation")

        # Normalize weights to sum to 1
        norm_weights: dict[str, Decimal] = {
            v: Decimal(str(w / total_raw)) for v, w in raw_weights.items()
        }

        # Weighted blended price
        blended = sum(
            (norm_weights[v] * normalized_prices[v].cngn_usd for v in contributing_venues),
            Decimal("0"),
        )

        # Confidence: same formula as BlendedPriceCalculator._compute_confidence
        missing = total_venues - len(contributing_venues)
        confidence = min(0.9, max(0.0, 0.9 - 0.2 * missing))

        self._variance_tracker.update(float(blended))

        logger.debug(
            "market_fair_price_computed",
            price=float(blended),
            venues=contributing_venues,
            confidence=round(confidence, 3),
        )

        return MarketFairPrice(
            price=blended,
            weights=norm_weights,
            confidence=confidence,
            timestamp=int(time.time() * 1000),
        )
