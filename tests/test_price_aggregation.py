"""Tests for price normalization and blended price computation."""

import pytest
from decimal import Decimal

from engine.api.schemas import PriceQuote
from engine.market.price_aggregation import (
    PriceNormalizer,
    BlendedPriceCalculator,
    NormalizedPrice,
    BlendedPrice,
    CNGN_USD_PAIRS,
    INVERTED_PAIRS,
)
from engine.market.venue_prices import VenuePrice


# =============================================================================
# Helpers
# =============================================================================


def _make_venue_price(
    venue: str,
    pair: str,
    bid: Decimal,
    ask: Decimal,
    mid: Decimal,
    source: str | None = None,
) -> VenuePrice:
    """Create a VenuePrice with a valid quote."""
    quote = PriceQuote(
        source=source or venue,
        timestamp=1700000000000,
        bid=bid,
        ask=ask,
        mid=mid,
    )
    return VenuePrice(venue=venue, pair=pair, quote=quote)


def _make_venue_prices() -> dict[str, VenuePrice]:
    """Standard set of venue prices for testing."""
    return {
        "bybit": _make_venue_price(
            "bybit", "USDT/NGN",
            bid=Decimal("1435"), ask=Decimal("1437"), mid=Decimal("1436"),
            source="bybit_p2p",
        ),
        "quidax": _make_venue_price(
            "quidax", "cNGN/USDT",
            bid=Decimal("0.000696"), ask=Decimal("0.000698"), mid=Decimal("0.000697"),
            source="quidax",
        ),
        "uni-base": _make_venue_price(
            "uni-base", "cNGN/USDC",
            bid=Decimal("0.000695"), ask=Decimal("0.000697"), mid=Decimal("0.000696"),
            source="uni-base_pool",
        ),
        "uni-bsc": _make_venue_price(
            "uni-bsc", "cNGN/USDT",
            bid=Decimal("0.000700"), ask=Decimal("0.000702"), mid=Decimal("0.000701"),
            source="uni-bsc_pool",
        ),
    }


# =============================================================================
# Venue classification
# =============================================================================


class TestPairClassification:
    """Pair strings must be in the right normalization set.

    To add a new pair: add its string to CNGN_USD_PAIRS or INVERTED_PAIRS
    in price_aggregation.py — nothing else needs changing.
    """

    def test_cngn_usdc_is_direct(self):
        assert "cNGN/USDC" in CNGN_USD_PAIRS

    def test_cngn_usdt_is_direct(self):
        assert "cNGN/USDT" in CNGN_USD_PAIRS

    def test_usdt_ngn_is_inverted(self):
        assert "USDT/NGN" in INVERTED_PAIRS

    def test_usdc_cngn_is_inverted(self):
        assert "USDC/cNGN" in INVERTED_PAIRS

    def test_usdt_cngn_is_inverted(self):
        assert "USDT/cNGN" in INVERTED_PAIRS


# =============================================================================
# PriceNormalizer
# =============================================================================


class TestPriceNormalizer:
    """Test price normalization to cNGN/USD."""

    def setup_method(self):
        self.normalizer = PriceNormalizer()

    def test_normalize_bybit_usdt_ngn(self):
        """Bybit USDT/NGN should be inverted: cNGN/USD = 1/mid."""
        prices = {
            "bybit": _make_venue_price(
                "bybit", "USDT/NGN",
                bid=Decimal("1436"), ask=Decimal("1438"), mid=Decimal("1437"),
                source="bybit_p2p",
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "bybit" in result
        # 1/1437 ≈ 0.000696
        expected = Decimal("1") / Decimal("1437")
        assert abs(result["bybit"].cngn_usd - expected) < Decimal("0.000001")
        assert result["bybit"].basis == "USDT/NGN"

    def test_normalize_quidax_cngn_usdt(self):
        """Quidax cNGN/USDT is already in cNGN/USD form."""
        prices = {
            "quidax": _make_venue_price(
                "quidax", "cNGN/USDT",
                bid=Decimal("0.000696"), ask=Decimal("0.000698"), mid=Decimal("0.000697"),
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "quidax" in result
        assert result["quidax"].cngn_usd == Decimal("0.000697")

    def test_normalize_uni_base_cngn_usdc(self):
        """uni-base cNGN/USDC is already cNGN/USD."""
        prices = {
            "uni-base": _make_venue_price(
                "uni-base", "cNGN/USDC",
                bid=Decimal("0.000695"), ask=Decimal("0.000697"), mid=Decimal("0.000696"),
                source="uni-base_pool",
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "uni-base" in result
        assert result["uni-base"].cngn_usd == Decimal("0.000696")

    def test_normalize_blockradar_direct_cngn_usd(self):
        """Blockradar now reports cNGN/USD directly — used as-is."""
        prices = {
            "blockradar": _make_venue_price(
                "blockradar", "cNGN/USDC",
                bid=Decimal("0.000720"), ask=Decimal("0.000724"), mid=Decimal("0.000722"),
                source="blockradar",
            ),
        }
        result = self.normalizer.normalize(prices)

        assert "blockradar" in result
        assert result["blockradar"].cngn_usd == Decimal("0.000722")

    def test_normalize_unknown_pair_skipped(self):
        """Unknown pairs are silently skipped."""
        prices = {
            "blockradar": _make_venue_price(
                "blockradar", "cNGN/NGN",
                bid=Decimal("0.998"), ask=Decimal("1.002"), mid=Decimal("1.0"),
            ),
        }
        result = self.normalizer.normalize(prices)
        assert "blockradar" not in result

    def test_skip_invalid_venue_prices(self):
        """Venues with no quote or zero mid should be skipped."""
        zero_quote = PriceQuote(
            source="test", timestamp=1700000000000,
            bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0"),
        )
        prices = {
            "quidax": VenuePrice(venue="quidax", pair="cNGN/USDT", quote=zero_quote),
        }
        result = self.normalizer.normalize(prices)
        assert "quidax" not in result

    def test_skip_venue_with_no_quote(self):
        """Venues with None quote should be skipped."""
        prices = {
            "quidax": VenuePrice(venue="quidax", pair="cNGN/USDT", quote=None, error="timeout"),
        }
        result = self.normalizer.normalize(prices)
        assert len(result) == 0

    def test_normalize_multiple_venues(self):
        """All valid venues should be normalized."""
        prices = _make_venue_prices()
        result = self.normalizer.normalize(prices)

        assert len(result) == 4
        assert all(v in result for v in ["bybit", "quidax", "uni-base", "uni-bsc"])

        # All should be in a similar range (~0.0007)
        for np in result.values():
            assert Decimal("0.0005") < np.cngn_usd < Decimal("0.001")


# =============================================================================
# BlendedPriceCalculator — VWAP (sync portion)
# =============================================================================


class TestVWAP:
    """Test cross-venue VWAP computation."""

    def test_vwap_explicit_equal_weights_is_arithmetic_mean(self):
        """Explicit equal weights produce arithmetic mean."""
        normalizer = PriceNormalizer()
        prices = _make_venue_prices()
        normalized = normalizer.normalize(prices)

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        weights = {v: Decimal("1") for v in normalized}
        vwap = calc.compute_vwap(normalized, weights)

        # Mean of ~0.000696, 0.000697, 0.000696, 0.000701
        values = [np.cngn_usd for np in normalized.values()]
        expected = sum(values) / len(values)
        assert abs(vwap - expected) < Decimal("0.000001")

    def test_vwap_volume_weighted(self):
        """When venues carry volume_24h_usd, VWAP is weighted by it."""
        normalizer = PriceNormalizer()
        prices = _make_venue_prices()
        normalized = normalizer.normalize(prices)

        # Assign volumes: uni-base dominates
        normalized["uni-base"].volume_24h_usd = Decimal("200000")
        normalized["quidax"].volume_24h_usd = Decimal("50000")
        normalized["bybit"].volume_24h_usd = Decimal("1000000")
        normalized["uni-bsc"].volume_24h_usd = Decimal("66000")

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        vwap = calc.compute_vwap(normalized)

        # bybit dominates — result should be closer to bybit's price than simple mean
        bybit_price = normalized["bybit"].cngn_usd
        simple_mean = sum(np.cngn_usd for np in normalized.values()) / len(normalized)
        assert abs(vwap - bybit_price) < abs(vwap - simple_mean)

    def test_vwap_skips_venue_with_no_volume(self):
        """Venues with no volume_24h_usd are excluded from volume-weighted VWAP."""
        normalizer = PriceNormalizer()
        prices = _make_venue_prices()
        normalized = normalizer.normalize(prices)

        # Only quidax gets volume — result should equal quidax's price exactly
        normalized["quidax"].volume_24h_usd = Decimal("50000")

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        vwap = calc.compute_vwap(normalized)
        assert vwap == normalized["quidax"].cngn_usd

    def test_vwap_custom_weights(self):
        """VWAP with custom weights should bias toward heavier venues."""
        normalizer = PriceNormalizer()
        prices = _make_venue_prices()
        normalized = normalizer.normalize(prices)

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}

        weights = {
            "uni-base": Decimal("10"),
            "quidax": Decimal("1"),
            "bybit": Decimal("1"),
            "uni-bsc": Decimal("1"),
        }
        vwap = calc.compute_vwap(normalized, weights)

        # Should be closer to uni-base's price than the simple mean
        uni_base_price = normalized["uni-base"].cngn_usd
        assert abs(vwap - uni_base_price) < Decimal("0.00001")

    def test_vwap_empty_input(self):
        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        assert calc.compute_vwap({}) == Decimal("0")

    def test_vwap_single_venue(self):
        normalizer = PriceNormalizer()
        prices = {
            "quidax": _make_venue_price(
                "quidax", "cNGN/USDT",
                bid=Decimal("0.000696"), ask=Decimal("0.000698"), mid=Decimal("0.000697"),
            ),
        }
        normalized = normalizer.normalize(prices)
        normalized["quidax"].volume_24h_usd = Decimal("50000")

        calc = BlendedPriceCalculator.__new__(BlendedPriceCalculator)
        calc.venue_weights = {}
        vwap = calc.compute_vwap(normalized)
        assert vwap == Decimal("0.000697")

    @pytest.mark.asyncio
    async def test_calculate_current_does_not_proxy_uni_bsc_volume(self):
        class DummyAggregator:
            async def fetch_all(self):
                prices = _make_venue_prices()
                prices["bybit"].volume_24h_usd = Decimal("1000000")
                prices["quidax"].volume_24h_usd = Decimal("50000")
                prices["uni-base"].volume_24h_usd = Decimal("200000")
                prices["uni-bsc"].volume_24h_usd = None
                return prices

        calc = BlendedPriceCalculator(
            price_aggregator=DummyAggregator(),
            normalizer=PriceNormalizer(),
        )

        async def _zero_twap(window_seconds: int = 300, venue: str | None = None) -> Decimal:
            return Decimal("0")

        calc.compute_twap = _zero_twap  # type: ignore[method-assign]

        blended = await calc.get_blended_price(force_refresh=True)

        expected = (
            (Decimal("1") / Decimal("1436")) * Decimal("1000000")
            + Decimal("0.000697") * Decimal("50000")
            + Decimal("0.000696") * Decimal("200000")
        ) / Decimal("1250000")

        assert abs(blended.vwap - expected) < Decimal("0.0000001")
        assert blended.dex_volume_24h_usd["uni-base"] == Decimal("200000")
        assert blended.dex_volume_24h_usd["uni-bsc"] is None


# =============================================================================
# Source-to-venue mapping
# =============================================================================


class TestSourceToVenue:
    """Test the _source_to_venue static method."""

    def test_bybit_p2p(self):
        assert BlendedPriceCalculator._source_to_venue("bybit_p2p") == "bybit"

    def test_quidax(self):
        assert BlendedPriceCalculator._source_to_venue("quidax") == "quidax"

    def test_uni_base_pool(self):
        assert BlendedPriceCalculator._source_to_venue("uni-base_pool") == "uni-base"

    def test_uni_bsc_pool(self):
        assert BlendedPriceCalculator._source_to_venue("uni-bsc_pool") == "uni-bsc"

    def test_blockradar(self):
        assert BlendedPriceCalculator._source_to_venue("blockradar") == "blockradar"

    def test_assetchain_pool(self):
        assert BlendedPriceCalculator._source_to_venue("assetchain_pool") == "assetchain"

    def test_unknown_passthrough(self):
        assert BlendedPriceCalculator._source_to_venue("something_new") == "something_new"


# =============================================================================
# Single-price normalization
# =============================================================================


class TestNormalizeSinglePrice:
    """Test _normalize_single_price helper."""

    def test_bybit_usdt_ngn_inverted(self):
        result = BlendedPriceCalculator._normalize_single_price("bybit", Decimal("1437"))
        assert result is not None
        assert abs(result - Decimal("1") / Decimal("1437")) < Decimal("0.000001")

    def test_quidax_passthrough(self):
        result = BlendedPriceCalculator._normalize_single_price("quidax", Decimal("0.000697"))
        assert result == Decimal("0.000697")

    def test_uni_base_passthrough(self):
        result = BlendedPriceCalculator._normalize_single_price("uni-base", Decimal("0.000696"))
        assert result == Decimal("0.000696")

    def test_blockradar_returns_none(self):
        """Blockradar can't be normalized without cross-rate."""
        result = BlendedPriceCalculator._normalize_single_price("blockradar", Decimal("1.0"))
        assert result is None

    def test_zero_price_returns_none(self):
        result = BlendedPriceCalculator._normalize_single_price("quidax", Decimal("0"))
        assert result is None

    def test_negative_price_returns_none(self):
        result = BlendedPriceCalculator._normalize_single_price("quidax", Decimal("-1"))
        assert result is None


# =============================================================================
# Confidence score
# =============================================================================


class TestConfidence:
    """Test confidence score computation: 90% max, -20% per missing venue."""

    def _np(self, venue: str) -> NormalizedPrice:
        return NormalizedPrice(
            venue=venue, cngn_usd=Decimal("0.000700"),
            raw_quote=PriceQuote(source=venue, timestamp=0, bid=Decimal("0"), ask=Decimal("0"), mid=Decimal("0")),
            basis="cNGN/USDC", timestamp=0,
        )

    def test_all_six_venues_caps_at_90(self):
        """Full house across all current venues must not exceed 90%."""
        venues = ["bybit", "quidax", "uni-base", "uni-bsc", "assetchain", "blockradar"]
        normalized = {v: self._np(v) for v in venues}
        assert BlendedPriceCalculator._compute_confidence(normalized, 6) == pytest.approx(0.9)

    def test_ceiling_prevents_over_90(self):
        """More venues reporting than total must not push confidence above 90%."""
        normalized = {v: self._np(v) for v in ["a", "b", "c", "d", "e"]}
        assert BlendedPriceCalculator._compute_confidence(normalized, 4) == pytest.approx(0.9)

    def test_one_missing(self):
        venues = ["bybit", "quidax", "uni-base", "uni-bsc", "assetchain"]
        normalized = {v: self._np(v) for v in venues}
        assert BlendedPriceCalculator._compute_confidence(normalized, 6) == pytest.approx(0.7)

    def test_two_missing(self):
        normalized = {v: self._np(v) for v in ["bybit", "quidax", "uni-base", "uni-bsc"]}
        assert BlendedPriceCalculator._compute_confidence(normalized, 6) == pytest.approx(0.5)

    def test_one_venue(self):
        assert BlendedPriceCalculator._compute_confidence({"bybit": self._np("bybit")}, 6) == pytest.approx(0.0)

    def test_empty_floors_at_zero(self):
        assert BlendedPriceCalculator._compute_confidence({}, 6) == pytest.approx(0.0)


# =============================================================================
# BlendedPrice dataclass
# =============================================================================


class TestBlendedPrice:
    """Test BlendedPrice dataclass properties."""

    def test_reference_price_ngn(self):
        """reference_price_ngn should be 1/vwap."""
        bp = BlendedPrice(
            vwap=Decimal("0.000700"),
            twap_5m=Decimal("0.000700"),
            twap_1h=Decimal("0.000700"),
            venue_prices={},
            timestamp=0,
            num_sources=3,
            confidence=1.0,
        )
        expected = Decimal("1") / Decimal("0.000700")
        assert abs(bp.reference_price_ngn - expected) < Decimal("0.01")

    def test_reference_price_ngn_zero_vwap(self):
        bp = BlendedPrice(
            vwap=Decimal("0"),
            twap_5m=Decimal("0"),
            twap_1h=Decimal("0"),
            venue_prices={},
            timestamp=0,
            num_sources=0,
            confidence=0.0,
        )
        assert bp.reference_price_ngn == Decimal("0")
