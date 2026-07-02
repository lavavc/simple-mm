from decimal import Decimal

import pytest

from engine.market.venue_prices import VenuePrice
from engine.types import PriceQuote
from research.scripts.capture_fair_price_feeds import (
    CaptureSummary,
    build_capture_aggregator,
    capture_loop,
    capture_once,
    parse_sources,
)


class _FakeAggregator:
    def __init__(self, prices: dict[str, VenuePrice]) -> None:
        self._prices = prices
        self.fetch_count = 0

    async def fetch_all(self) -> dict[str, VenuePrice]:
        self.fetch_count += 1
        return self._prices


class _FakePriceStore:
    def __init__(self) -> None:
        self.inserted: list[tuple[PriceQuote, dict | None]] = []

    async def insert_price_snapshot(
        self,
        quote: PriceQuote,
        metadata: dict | None = None,
    ) -> None:
        self.inserted.append((quote, metadata))


class _ManualClock:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class _TimedAggregator:
    def __init__(self, quote: PriceQuote, clock: _ManualClock) -> None:
        self.quote = quote
        self.clock = clock
        self.fetch_count = 0
        self.starts: list[float] = []
        self.fetch_durations = [2.0, 7.0, 1.0]

    async def fetch_all(self) -> dict[str, VenuePrice]:
        self.starts.append(self.clock.now)
        self.clock.now += self.fetch_durations[self.fetch_count]
        self.fetch_count += 1
        return {
            "quidax": VenuePrice(
                venue="quidax",
                pair="cNGN/USDT",
                quote=self.quote,
                metadata={"capture_type": "ticker_depth"},
            )
        }


@pytest.mark.asyncio
async def test_capture_once_persists_valid_quotes_with_metadata() -> None:
    quote = PriceQuote(
        source="quidax",
        timestamp=1_700_000_000_000,
        bid=Decimal("0.000620"),
        ask=Decimal("0.000630"),
        mid=Decimal("0.000625"),
    )
    metadata = {"capture_type": "ticker_depth"}
    aggregator = _FakeAggregator(
        {
            "quidax": VenuePrice(
                venue="quidax",
                pair="cNGN/USDT",
                quote=quote,
                metadata=metadata,
            )
        }
    )
    store = _FakePriceStore()

    summary = await capture_once(
        price_aggregator=aggregator,
        price_store=store,
    )

    assert summary == CaptureSummary(attempted=1, persisted=1, failed=0)
    assert aggregator.fetch_count == 1
    assert store.inserted == [(quote, metadata)]


@pytest.mark.asyncio
async def test_capture_once_skips_invalid_prices() -> None:
    aggregator = _FakeAggregator(
        {
            "quidax": VenuePrice(
                venue="quidax",
                pair="cNGN/USDT",
                quote=None,
                error="timeout",
            )
        }
    )
    store = _FakePriceStore()

    summary = await capture_once(
        price_aggregator=aggregator,
        price_store=store,
    )

    assert summary == CaptureSummary(attempted=1, persisted=0, failed=1)
    assert store.inserted == []


def test_capture_script_does_not_import_trading_scheduler() -> None:
    import research.scripts.capture_fair_price_feeds as module

    assert not hasattr(module, "TradingScheduler")
    assert not hasattr(module, "QuidaxAdapter")
    assert isinstance(module.DEFAULT_CAPTURE_SOURCES, tuple)
    assert set(module.DEFAULT_CAPTURE_SOURCES) == {"quidax", "bybit"}


@pytest.mark.asyncio
async def test_capture_loop_aggregates_iteration_summaries() -> None:
    quote = PriceQuote(
        source="quidax",
        timestamp=1_700_000_000_000,
        bid=Decimal("0.000620"),
        ask=Decimal("0.000630"),
        mid=Decimal("0.000625"),
    )
    aggregator = _FakeAggregator(
        {
            "quidax": VenuePrice(
                venue="quidax",
                pair="cNGN/USDT",
                quote=quote,
                metadata={"capture_type": "ticker_depth"},
            )
        }
    )
    store = _FakePriceStore()

    summary = await capture_loop(
        price_aggregator=aggregator,
        price_store=store,
        interval_seconds=0,
        iterations=2,
    )

    assert summary == CaptureSummary(attempted=2, persisted=2, failed=0)
    assert aggregator.fetch_count == 2
    assert len(store.inserted) == 2


@pytest.mark.asyncio
async def test_capture_loop_uses_fixed_rate_start_schedule() -> None:
    quote = PriceQuote(
        source="quidax",
        timestamp=1_700_000_000_000,
        bid=Decimal("0.000620"),
        ask=Decimal("0.000630"),
        mid=Decimal("0.000625"),
    )
    clock = _ManualClock(now=100.0)
    aggregator = _TimedAggregator(quote, clock)
    store = _FakePriceStore()

    summary = await capture_loop(
        price_aggregator=aggregator,
        price_store=store,
        interval_seconds=5,
        iterations=3,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert summary == CaptureSummary(attempted=3, persisted=3, failed=0)
    assert aggregator.starts == [100.0, 105.0, 112.0]
    assert clock.sleeps == [3.0]


def test_parse_sources_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="At least one source"):
        parse_sources("")


def test_build_capture_aggregator_applies_cache_overrides() -> None:
    aggregator = build_capture_aggregator(
        ("quidax", "bybit"),
        quidax_cache_seconds=0,
        bybit_cache_seconds=10,
    )

    assert aggregator.sources["quidax"].config.cache_seconds == 0
    assert aggregator.sources["bybit"].config.cache_seconds == 10


def test_build_capture_aggregator_rejects_negative_cache_overrides() -> None:
    with pytest.raises(ValueError, match="cache seconds must be non-negative"):
        build_capture_aggregator(
            ("quidax",),
            quidax_cache_seconds=-1,
            bybit_cache_seconds=None,
        )
