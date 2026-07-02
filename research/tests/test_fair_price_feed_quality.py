from decimal import Decimal

from research.scripts.export_fair_price_markouts import PriceSnapshot
from research.scripts.report_fair_price_feed_quality import (
    SourceFeedQuality,
    analyze_feed_quality,
    render_feed_quality_markdown,
)


def _snapshot(
    source: str,
    timestamp_ms: int,
    *,
    mid: str = "0.000625",
    metadata: dict | None = None,
) -> PriceSnapshot:
    return PriceSnapshot(
        source=source,
        timestamp_ms=timestamp_ms,
        bid=Decimal("0.000620"),
        ask=Decimal("0.000630"),
        mid=Decimal(mid),
        metadata=metadata,
    )


def _quidax_metadata(
    *,
    bid_amount: str = "100",
    ask_amount: str = "100",
) -> dict:
    return {
        "capture_type": "ticker_depth",
        "depth": {
            "bid_levels": [{"price": "1600", "amount": bid_amount}],
            "ask_levels": [{"price": "1610", "amount": ask_amount}],
        },
    }


def test_analyze_feed_quality_reports_raw_source_cadence_and_depth() -> None:
    report = analyze_feed_quality(
        [
            _snapshot("quidax", 1_000, metadata=_quidax_metadata()),
            _snapshot("quidax", 6_000, metadata=_quidax_metadata()),
            _snapshot("quidax", 16_000, metadata={"capture_type": "ticker_depth"}),
            _snapshot(
                "bybit_p2p",
                2_000,
                mid="1400",
                metadata={"capture_type": "p2p_order_ads"},
            ),
        ],
        target_usd=Decimal("10"),
    )

    assert report.sources["quidax"] == SourceFeedQuality(
        source="quidax",
        observations=3,
        first_timestamp_ms=1_000,
        last_timestamp_ms=16_000,
        median_gap_ms=Decimal("7500"),
        max_gap_ms=Decimal("10000"),
        metadata_observations=3,
        missing_metadata_count=0,
        executable_depth_observations=2,
        missing_executable_depth_count=1,
        unique_mid_count=1,
    )
    assert report.sources["bybit_p2p"].observations == 1
    assert report.sources["bybit_p2p"].metadata_observations == 1


def test_analyze_feed_quality_counts_insufficient_depth_as_missing_executable() -> None:
    report = analyze_feed_quality(
        [
            _snapshot(
                "quidax",
                1_000,
                metadata=_quidax_metadata(bid_amount="1", ask_amount="1"),
            )
        ],
        target_usd=Decimal("10"),
    )

    quality = report.sources["quidax"]
    assert quality.executable_depth_observations == 0
    assert quality.missing_executable_depth_count == 1


def test_render_feed_quality_markdown_includes_soundness_guidance() -> None:
    report = analyze_feed_quality(
        [
            _snapshot("quidax", 1_000, metadata=_quidax_metadata()),
            _snapshot("quidax", 6_000, metadata=_quidax_metadata()),
        ],
        target_usd=Decimal("10"),
    )

    markdown = render_feed_quality_markdown(report)

    assert "# Fair Price Feed Quality Report" in markdown
    assert "Target USD: 10" in markdown
    assert "| quidax | 2 | 1000 | 6000 | 5000 | 5000 | 2 | 0 | 2 | 0 | 1 |" in markdown
    assert "Use this report before estimator comparison." in markdown
