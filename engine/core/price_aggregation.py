"""Unified price normalization and blended price computation.

Provides:
- PriceNormalizer: converts all venue prices to a common cNGN/USD basis
- BlendedPriceCalculator: computes TWAP, VWAP, and composite blended prices
- NormalizedPrice / BlendedPrice: data classes for results

This module is the single source of truth for "what is the fair value of cNGN?"
and is consumed by:
- Arbitrage detection (cross-venue comparison)
- LP position management (venue-vs-fair-value divergence)
- Portfolio delta management (USD valuation)
- CEX/Blockradar rate syncing (reference price)
- Dashboard display
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import structlog

from engine.api.schemas import PriceQuote
from engine.core.venue_prices import VenuePriceAggregator, VenuePrice

logger = structlog.get_logger()


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class NormalizedPrice:
    """A venue price converted to the common cNGN/USD basis."""

    venue: str
    cngn_usd: Decimal  # How many USD per 1 cNGN
    raw_quote: PriceQuote  # Original venue quote
    basis: str  # Original pair, e.g. "USDT/NGN", "cNGN/USDC", "cNGN/NGN"
    timestamp: int


@dataclass
class BlendedPrice:
    """Composite price combining multiple venues and time windows."""

    vwap: Decimal  # Cross-venue volume-weighted average at current instant
    twap_5m: Decimal  # 5-minute time-weighted average
    twap_1h: Decimal  # 1-hour time-weighted average
    venue_prices: dict[str, Decimal]  # Per-venue normalized cNGN/USD prices
    timestamp: int
    num_sources: int  # How many venues contributed
    confidence: float  # 0-1, based on source agreement

    @property
    def reference_price_ngn(self) -> Decimal:
        """USDT/NGN equivalent (inverse of cNGN/USD) for CEX/rate syncing."""
        if self.vwap > 0:
            return Decimal("1") / self.vwap
        return Decimal("0")


# =============================================================================
# Price Normalizer
# =============================================================================


# Venue classification by native pair type
USDT_NGN_VENUES = {"bybit"}
CNGN_USD_VENUES = {"quidax", "aerodrome", "pancakeswap"}
CNGN_NGN_VENUES = {"blockradar"}


def classify_venue(venue: str) -> str:
    """Return the pair basis for a venue name."""
    if venue in USDT_NGN_VENUES:
        return "USDT/NGN"
    elif venue in CNGN_USD_VENUES:
        return "cNGN/USDC"
    elif venue in CNGN_NGN_VENUES:
        return "cNGN/NGN"
    return "unknown"


class PriceNormalizer:
    """Converts all venue prices to cNGN/USD common basis.

    Normalization rules:
    - USDT/NGN venues (Bybit, Quidax): cNGN/USD = 1 / mid  (cNGN ≈ NGN, USDT ≈ USD)
    - cNGN/USDC venues (Aerodrome, PancakeSwap): already cNGN/USD  (USDC ≈ USD)
    - cNGN/NGN venues (Blockradar): needs a USDT/NGN cross-rate to normalise

    Blockradar normalization:
      The Blockradar mid is in NGN-per-cNGN (should be ~1.0 if cNGN=NGN).
      To get cNGN/USD we need: cNGN_USD = blockradar_mid / usdt_ngn_rate
      We use the best available USDT/NGN rate from other venues for the cross.
    """

    def normalize(
        self,
        venue_prices: dict[str, VenuePrice],
    ) -> dict[str, NormalizedPrice]:
        """Normalize all venue prices to cNGN/USD.

        Args:
            venue_prices: Raw venue prices from VenuePriceAggregator.

        Returns:
            Dict of venue name -> NormalizedPrice (only valid venues included).
        """
        normalized: dict[str, NormalizedPrice] = {}

        # First pass: normalize everything except cNGN/NGN venues
        # (we need USDT/NGN from other venues to cross-rate Blockradar)
        usdt_ngn_mid: Optional[Decimal] = None

        for venue, price in venue_prices.items():
            if not price.is_valid or price.quote is None:
                continue

            mid = price.quote.mid
            basis = classify_venue(venue)

            if venue in USDT_NGN_VENUES:
                if mid > 0:
                    cngn_usd = Decimal("1") / mid
                    normalized[venue] = NormalizedPrice(
                        venue=venue,
                        cngn_usd=cngn_usd,
                        raw_quote=price.quote,
                        basis=basis,
                        timestamp=price.quote.timestamp,
                    )
                    # Keep track of a USDT/NGN mid for Blockradar cross-rate
                    if usdt_ngn_mid is None:
                        usdt_ngn_mid = mid

            elif venue in CNGN_USD_VENUES:
                if mid > 0:
                    normalized[venue] = NormalizedPrice(
                        venue=venue,
                        cngn_usd=mid,
                        raw_quote=price.quote,
                        basis=basis,
                        timestamp=price.quote.timestamp,
                    )

            elif venue in CNGN_NGN_VENUES:
                # Handled in second pass
                pass

            else:
                # Unknown venue – use directly if it looks like cNGN/USD
                if Decimal("0") < mid < Decimal("1"):
                    normalized[venue] = NormalizedPrice(
                        venue=venue,
                        cngn_usd=mid,
                        raw_quote=price.quote,
                        basis="unknown",
                        timestamp=price.quote.timestamp,
                    )

        # Second pass: normalize Blockradar using USDT/NGN cross-rate
        if usdt_ngn_mid and usdt_ngn_mid > 0:
            for venue, price in venue_prices.items():
                if venue not in CNGN_NGN_VENUES:
                    continue
                if not price.is_valid or price.quote is None:
                    continue

                mid = price.quote.mid  # NGN per cNGN (~1.0)
                if mid > 0:
                    cngn_usd = mid / usdt_ngn_mid
                    basis = classify_venue(venue)
                    normalized[venue] = NormalizedPrice(
                        venue=venue,
                        cngn_usd=cngn_usd,
                        raw_quote=price.quote,
                        basis=basis,
                        timestamp=price.quote.timestamp,
                    )

        logger.debug(
            "prices_normalized",
            venues=list(normalized.keys()),
            prices={v: float(p.cngn_usd) for v, p in normalized.items()},
        )

        return normalized


# =============================================================================
# Blended Price Calculator
# =============================================================================


class BlendedPriceCalculator:
    """Computes composite prices from multiple venues.

    Combines:
    - VWAP: volume/weight-weighted average across venues at a single point in time
    - TWAP: time-weighted average from stored price snapshots over configurable windows

    The blended price is the primary "fair value" reference used by all consumers.
    """

    def __init__(
        self,
        price_aggregator: VenuePriceAggregator,
        normalizer: PriceNormalizer | None = None,
        venue_weights: dict[str, Decimal] | None = None,
    ):
        """
        Args:
            price_aggregator: Source of raw venue prices.
            normalizer: Price normalizer (creates default if None).
            venue_weights: Optional explicit weights for VWAP. Defaults to equal.
        """
        self.price_aggregator = price_aggregator
        self.normalizer = normalizer or PriceNormalizer()
        self.venue_weights = venue_weights or {}

        # Caches
        self._last_blended: Optional[BlendedPrice] = None
        self._last_blended_time: float = 0

    # ------------------------------------------------------------------
    # VWAP
    # ------------------------------------------------------------------

    def compute_vwap(
        self,
        normalized_prices: dict[str, NormalizedPrice],
        weights: dict[str, Decimal] | None = None,
    ) -> Decimal:
        """Volume-weighted average across venues at a point in time.

        If explicit weights are provided they are used; otherwise venues
        are weighted equally.

        Args:
            normalized_prices: Normalized venue prices.
            weights: Optional {venue: weight} map.

        Returns:
            VWAP in cNGN/USD.
        """
        if not normalized_prices:
            return Decimal("0")

        effective_weights = weights or self.venue_weights

        total_weight = Decimal("0")
        weighted_sum = Decimal("0")

        for venue, np in normalized_prices.items():
            w = effective_weights.get(venue, Decimal("1"))
            weighted_sum += np.cngn_usd * w
            total_weight += w

        if total_weight == 0:
            return Decimal("0")

        return weighted_sum / total_weight

    # ------------------------------------------------------------------
    # TWAP
    # ------------------------------------------------------------------

    async def compute_twap(
        self,
        window_seconds: int = 300,
        venue: str | None = None,
    ) -> Decimal:
        """Time-weighted average from stored price snapshots.

        Queries the database for snapshots within the window and computes
        a simple time-weighted mean of their normalized cNGN/USD values.

        If *venue* is None, all snapshots are included (cross-venue TWAP).
        If *venue* is specified, only that venue's snapshots are used.

        Args:
            window_seconds: Lookback window in seconds.
            venue: Optional venue name filter.

        Returns:
            TWAP in cNGN/USD, or Decimal("0") if insufficient data.
        """
        from engine.db import get_db

        db = await get_db()
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - (window_seconds * 1000)

        snapshots = await db.get_price_snapshots_in_window(
            from_ts=from_ms,
            to_ts=now_ms,
            source=venue,
        )

        if not snapshots:
            return Decimal("0")

        # Normalize each snapshot and time-weight
        # Snapshots are dicts with keys: timestamp, source, bid, ask, mid
        total_weight = Decimal("0")
        weighted_sum = Decimal("0")

        for i, snap in enumerate(snapshots):
            # Compute the time span this snapshot represents
            # (from this snapshot to the next, or to now for the last one)
            ts = snap["timestamp"]
            if i + 1 < len(snapshots):
                next_ts = snapshots[i + 1]["timestamp"]
            else:
                next_ts = now_ms

            duration = Decimal(str(max(next_ts - ts, 1)))

            # Normalize the snapshot price
            source = snap["source"]
            mid = Decimal(str(snap["mid"]))

            if mid <= 0:
                continue

            # Determine the venue from the source string
            # Source may be "quidax", "bybit_p2p", "aerodrome_pool", etc.
            venue_name = self._source_to_venue(source)
            cngn_usd = self._normalize_single_price(venue_name, mid)

            if cngn_usd and cngn_usd > 0:
                weighted_sum += cngn_usd * duration
                total_weight += duration

        if total_weight == 0:
            return Decimal("0")

        return weighted_sum / total_weight

    # ------------------------------------------------------------------
    # Blended price
    # ------------------------------------------------------------------

    async def get_blended_price(
        self,
        force_refresh: bool = False,
    ) -> BlendedPrice:
        """Compute and return the blended composite price.

        Combines current VWAP with TWAP over 5m and 1h windows.

        Uses a short cache (5s) to avoid redundant computation when
        multiple consumers call in the same scheduler tick.
        """
        # Short-lived cache
        if (
            not force_refresh
            and self._last_blended is not None
            and time.time() - self._last_blended_time < 5
        ):
            return self._last_blended

        # Fetch and normalize current venue prices
        venue_prices = await self.price_aggregator.fetch_all()
        normalized = self.normalizer.normalize(venue_prices)

        # VWAP across venues right now
        vwap = self.compute_vwap(normalized)

        # TWAP over 5 minutes and 1 hour
        twap_5m = await self.compute_twap(window_seconds=300)
        twap_1h = await self.compute_twap(window_seconds=3600)

        # Fallbacks: if TWAP windows have no data, use VWAP
        if twap_5m == 0:
            twap_5m = vwap
        if twap_1h == 0:
            twap_1h = vwap

        # Confidence: based on how many sources agree within 1%
        confidence = self._compute_confidence(normalized, vwap)

        venue_price_map = {v: np.cngn_usd for v, np in normalized.items()}

        blended = BlendedPrice(
            vwap=vwap,
            twap_5m=twap_5m,
            twap_1h=twap_1h,
            venue_prices=venue_price_map,
            timestamp=int(time.time() * 1000),
            num_sources=len(normalized),
            confidence=confidence,
        )

        self._last_blended = blended
        self._last_blended_time = time.time()

        logger.info(
            "blended_price_computed",
            vwap=float(vwap),
            twap_5m=float(twap_5m),
            twap_1h=float(twap_1h),
            num_sources=len(normalized),
            confidence=round(confidence, 3),
        )

        return blended

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _source_to_venue(source: str) -> str:
        """Map a price snapshot 'source' field to its canonical venue name."""
        source_lower = source.lower()
        if "bybit" in source_lower:
            return "bybit"
        if "quidax" in source_lower:
            return "quidax"
        if "aerodrome" in source_lower:
            return "aerodrome"
        if "pancakeswap" in source_lower or "pancake" in source_lower:
            return "pancakeswap"
        if "blockradar" in source_lower:
            return "blockradar"
        return source_lower

    @staticmethod
    def _normalize_single_price(venue: str, mid: Decimal) -> Optional[Decimal]:
        """Normalize a single (venue, mid) pair to cNGN/USD."""
        if mid <= 0:
            return None

        if venue in USDT_NGN_VENUES:
            return Decimal("1") / mid
        elif venue in CNGN_USD_VENUES:
            return mid
        elif venue in CNGN_NGN_VENUES:
            # Cannot normalize without a cross-rate; skip in TWAP
            return None
        else:
            # Heuristic: if small, treat as cNGN/USD; if large, treat as USDT/NGN
            if mid < Decimal("1"):
                return mid
            elif mid > Decimal("100"):
                return Decimal("1") / mid
            return None

    @staticmethod
    def _compute_confidence(
        normalized: dict[str, NormalizedPrice],
        vwap: Decimal,
    ) -> float:
        """Compute confidence score 0-1 based on source agreement.

        Confidence is the fraction of venues whose price is within 1% of VWAP.
        """
        if not normalized or vwap <= 0:
            return 0.0

        agreeing = 0
        for np in normalized.values():
            if np.cngn_usd > 0:
                deviation = abs(np.cngn_usd - vwap) / vwap
                if deviation <= Decimal("0.01"):  # Within 1%
                    agreeing += 1

        return agreeing / len(normalized)
