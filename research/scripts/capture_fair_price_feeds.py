"""Capture fair-price research feeds without starting trading services."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Protocol

from engine.config import settings
from engine.db.backend import PriceStoreProtocol
from engine.db.repository import open_repository
from engine.market.venue_prices import (
    BybitConfig,
    BybitP2PPriceSource,
    QuidaxConfig,
    QuidaxPriceSource,
    VenuePrice,
    VenuePriceAggregator,
    VenuePriceSource,
)

DEFAULT_CAPTURE_SOURCES = ("quidax", "bybit")


class PriceAggregatorProtocol(Protocol):
    async def fetch_all(self) -> dict[str, VenuePrice]: ...


@dataclass(frozen=True)
class CaptureSummary:
    attempted: int
    persisted: int
    failed: int

    def add(self, other: "CaptureSummary") -> "CaptureSummary":
        return CaptureSummary(
            attempted=self.attempted + other.attempted,
            persisted=self.persisted + other.persisted,
            failed=self.failed + other.failed,
        )


async def capture_once(
    *,
    price_aggregator: PriceAggregatorProtocol,
    price_store: PriceStoreProtocol,
) -> CaptureSummary:
    prices = await price_aggregator.fetch_all()
    persisted = 0
    failed = 0
    for price in prices.values():
        if price.quote is None:
            failed += 1
            continue
        await price_store.insert_price_snapshot(price.quote, metadata=price.metadata)
        persisted += 1

    return CaptureSummary(
        attempted=len(prices),
        persisted=persisted,
        failed=failed,
    )


async def capture_loop(
    *,
    price_aggregator: PriceAggregatorProtocol,
    price_store: PriceStoreProtocol,
    interval_seconds: float,
    iterations: int,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CaptureSummary:
    total = CaptureSummary(attempted=0, persisted=0, failed=0)
    next_start = monotonic()
    for index in range(iterations):
        scheduled_start = next_start
        total = total.add(
            await capture_once(
                price_aggregator=price_aggregator,
                price_store=price_store,
            )
        )
        next_start = scheduled_start + interval_seconds
        if index + 1 < iterations:
            delay = next_start - monotonic()
            if delay > 0:
                await sleep(delay)
    return total


def build_capture_aggregator(
    sources: tuple[str, ...],
    *,
    quidax_cache_seconds: int | None = None,
    bybit_cache_seconds: int | None = None,
) -> VenuePriceAggregator:
    if (
        quidax_cache_seconds is not None
        and quidax_cache_seconds < 0
        or bybit_cache_seconds is not None
        and bybit_cache_seconds < 0
    ):
        raise ValueError("cache seconds must be non-negative")

    price_sources: list[VenuePriceSource] = []
    for source in sources:
        if source == "quidax":
            quidax_config = (
                QuidaxConfig(cache_seconds=quidax_cache_seconds)
                if quidax_cache_seconds is not None
                else None
            )
            price_sources.append(QuidaxPriceSource(quidax_config))
        elif source == "bybit":
            bybit_config = (
                BybitConfig(cache_seconds=bybit_cache_seconds)
                if bybit_cache_seconds is not None
                else None
            )
            price_sources.append(BybitP2PPriceSource(bybit_config))
        else:
            raise ValueError(f"Unknown capture source: {source}")
    return VenuePriceAggregator(price_sources)


def parse_sources(raw: str) -> tuple[str, ...]:
    sources = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not sources:
        raise ValueError("At least one source is required")
    return sources


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture Quidax/Bybit fair-price research feeds into price_snapshots.",
    )
    parser.add_argument("--db", default=settings.db_path, help="Path to engine SQLite DB")
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_CAPTURE_SOURCES),
        help="Comma-separated sources. Supported: quidax,bybit",
    )
    parser.add_argument("--interval", type=float, default=10.0, help="Seconds between captures")
    parser.add_argument("--iterations", type=int, default=1, help="Number of capture iterations")
    parser.add_argument(
        "--quidax-cache-seconds",
        type=int,
        default=None,
        help="Override Quidax source cache seconds for research captures.",
    )
    parser.add_argument(
        "--bybit-cache-seconds",
        type=int,
        default=None,
        help="Override Bybit P2P source cache seconds for research captures.",
    )
    args = parser.parse_args()

    if args.iterations < 0:
        raise ValueError("--iterations must be non-negative")
    if args.interval < 0:
        raise ValueError("--interval must be non-negative")

    aggregator = build_capture_aggregator(
        parse_sources(args.sources),
        quidax_cache_seconds=args.quidax_cache_seconds,
        bybit_cache_seconds=args.bybit_cache_seconds,
    )
    repo = await open_repository(args.db)
    try:
        summary = await capture_loop(
            price_aggregator=aggregator,
            price_store=repo.prices,
            interval_seconds=args.interval,
            iterations=args.iterations,
        )
    finally:
        await aggregator.close()
        await repo.close()

    print(json.dumps(asdict(summary), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
