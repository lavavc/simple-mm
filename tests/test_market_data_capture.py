from decimal import Decimal

import pytest

from engine.market.venue_prices import (
    BybitConfig,
    BybitP2PPriceSource,
    P2PAd,
    VenuePriceAggregator,
    VenuePriceSource,
    _build_quidax_depth_metadata,
)
from engine.types import PriceQuote


def test_quidax_depth_metadata_summarizes_top_levels() -> None:
    metadata = _build_quidax_depth_metadata(
        {
            "timestamp": 1_700_000_000_000,
            "bids": [["1600", "2"], ["1590", "3"]],
            "asks": [["1610", "1.5"], ["1620", "0.5"]],
        },
        top_levels=2,
    )

    assert metadata["best_bid_cngn_per_usdt"] == "1600"
    assert metadata["best_ask_cngn_per_usdt"] == "1610"
    assert metadata["spread_bps_native"] == 62
    assert metadata["bid_depth_usdt"] == "5"
    assert metadata["ask_depth_usdt"] == "2.0"
    assert metadata["bid_depth_cngn"] == "7970"
    assert metadata["ask_depth_cngn"] == "3225.0"


def test_bybit_p2p_aggregation_reports_filter_counts_and_depth() -> None:
    source = BybitP2PPriceSource(
        BybitConfig(
            min_completed_orders=10,
            min_completion_rate=0.80,
            max_avg_release_time=900,
            max_deviation_from_median=0.02,
        )
    )
    ads = [
        P2PAd(Decimal("1435"), 20, 0.95, 120, True, Decimal("100")),
        P2PAd(Decimal("1436"), 25, 0.96, 130, True, Decimal("200")),
        P2PAd(Decimal("1436"), 30, 0.97, 140, True, Decimal("300")),
        P2PAd(Decimal("1438"), 35, 0.98, 150, True, Decimal("400")),
        P2PAd(Decimal("1500"), 5, 0.98, 150, True, Decimal("500")),
    ]

    result = source._aggregate_ads(ads)

    assert result is not None
    assert result.price == Decimal("1436")
    assert result.raw_count == 5
    assert result.reputable_count == 4
    assert result.filtered_count == 4
    assert result.filtered_depth_usdt == Decimal("1000")

    metadata = source._p2p_aggregation_metadata(result)
    assert metadata["price"] == "1436"
    assert metadata["filtered_depth_usdt"] == "1000"


class _MetadataSource(VenuePriceSource):
    name = "metadata-test"
    pair = "cNGN/USDT"

    async def fetch_price(self) -> PriceQuote:
        self.volume_24h_usd = Decimal("123")
        self.latest_metadata = {"capture_type": "test", "depth": {"levels": 2}}
        return PriceQuote(
            source="metadata-test",
            timestamp=1_700_000_000_000,
            bid=Decimal("0.000700"),
            ask=Decimal("0.000702"),
            mid=Decimal("0.000701"),
        )


@pytest.mark.asyncio
async def test_venue_price_aggregator_propagates_capture_metadata() -> None:
    prices = await VenuePriceAggregator([_MetadataSource()]).fetch_all()

    price = prices["metadata-test"]
    assert price.quote is not None
    assert price.volume_24h_usd == Decimal("123")
    assert price.metadata == {"capture_type": "test", "depth": {"levels": 2}}
