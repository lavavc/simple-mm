import csv
from decimal import Decimal

from research.scripts.analyze_fair_price_markouts import (
    analyze_markouts,
    load_markout_rows,
    render_markdown_report,
)


def _row(
    *,
    timestamp_ms: str,
    ticker_mid: str,
    top_mid: str,
    executable_mid: str,
    bybit_mid: str,
    label_10s: str,
    label_30s: str = "",
    bybit_age_ms: str | None = None,
    label_10s_lag_ms: str = "1000",
    label_30s_lag_ms: str = "2000",
    owa_mid_topn: str = "",
    microprice_top1: str = "",
    pressure_topn: str = "",
    uni_base_premium_bps: str = "",
) -> dict[str, str]:
    return {
        "timestamp_ms": timestamp_ms,
        "source": "quidax",
        "target_usd": "100",
        "quidax_ticker_mid": ticker_mid,
        "quidax_top_mid": top_mid,
        "quidax_executable_mid": executable_mid,
        "quidax_owa_mid_topn": owa_mid_topn,
        "quidax_microprice_top1": microprice_top1,
        "quidax_cngn_usd_pressure_topn": pressure_topn,
        "uni_base_premium_bps": uni_base_premium_bps,
        "quidax_buy_cngn_usd": executable_mid,
        "quidax_sell_cngn_usd": executable_mid,
        "bybit_mid": bybit_mid,
        "bybit_age_ms": bybit_age_ms if bybit_age_ms is not None else ("0" if bybit_mid else ""),
        "label_10s_timestamp_ms": "11000",
        "label_10s_lag_ms": label_10s_lag_ms,
        "label_10s_buy_cngn_usd": label_10s,
        "label_10s_sell_cngn_usd": label_10s,
        "label_10s_executable_mid": label_10s,
        "label_30s_timestamp_ms": "31000" if label_30s else "",
        "label_30s_lag_ms": label_30s_lag_ms if label_30s else "",
        "label_30s_buy_cngn_usd": label_30s,
        "label_30s_sell_cngn_usd": label_30s,
        "label_30s_executable_mid": label_30s,
    }


def test_load_markout_rows_reads_csv(tmp_path) -> None:
    path = tmp_path / "markouts.csv"
    rows = [
        _row(
            timestamp_ms="1000",
            ticker_mid="0.0011",
            top_mid="0.00105",
            executable_mid="0.0010",
            bybit_mid="1000",
            label_10s="0.0012",
        )
    ]
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    assert load_markout_rows(str(path)) == rows


def test_analyze_markouts_computes_metrics_by_horizon_and_estimator() -> None:
    report = analyze_markouts(
        [
            _row(
                timestamp_ms="1000",
                ticker_mid="0.0011",
                top_mid="0.00105",
                executable_mid="0.0010",
                bybit_mid="1000",
                label_10s="0.0012",
                label_30s="0.0009",
            ),
            _row(
                timestamp_ms="2000",
                ticker_mid="0.0009",
                top_mid="0.0010",
                executable_mid="0.0010",
                bybit_mid="800",
                label_10s="0.0008",
            ),
        ],
        horizons_seconds=[10, 30],
    )

    assert report.total_rows == 2
    assert report.horizons[10].label_count == 2
    assert report.horizons[10].label_lag_observations == 2
    assert report.horizons[10].median_label_lag_ms == Decimal("1000")
    assert report.horizons[10].max_label_lag_ms == Decimal("1000")
    ticker = report.horizons[10].estimators["quidax_ticker_mid"]
    assert ticker.observations == 2
    assert ticker.mae == Decimal("0.0001")
    assert ticker.signed_bias == Decimal("0")
    assert ticker.rmse == Decimal("0.0001")
    assert ticker.direction_observations == 2
    assert ticker.direction_hit_rate == Decimal("1")

    top = report.horizons[10].estimators["quidax_top_mid"]
    assert top.mae == Decimal("0.000175")
    assert top.signed_bias == Decimal("0.000025")
    assert top.direction_observations == 1

    bybit = report.horizons[10].estimators["bybit_p2p_mid"]
    assert bybit.observations == 2
    assert bybit.mae == Decimal("0.000325")

    horizon_30 = report.horizons[30]
    assert horizon_30.label_count == 1
    assert horizon_30.missing_label_count == 1
    assert horizon_30.label_lag_observations == 1
    assert horizon_30.median_label_lag_ms == Decimal("2000")


def test_analyze_markouts_scores_imbalance_estimators_and_walk_forward_buckets() -> None:
    report = analyze_markouts(
        [
            _row(
                timestamp_ms="1000",
                ticker_mid="1.00",
                top_mid="1.00",
                executable_mid="1.00",
                bybit_mid="",
                owa_mid_topn="1.08",
                microprice_top1="1.07",
                pressure_topn="0.70",
                label_10s="1.10",
            ),
            _row(
                timestamp_ms="2000",
                ticker_mid="1.00",
                top_mid="1.00",
                executable_mid="1.00",
                bybit_mid="",
                owa_mid_topn="0.92",
                microprice_top1="0.91",
                pressure_topn="-0.80",
                label_10s="0.90",
            ),
            _row(
                timestamp_ms="3000",
                ticker_mid="1.00",
                top_mid="1.00",
                executable_mid="1.00",
                bybit_mid="",
                owa_mid_topn="1.03",
                microprice_top1="1.02",
                pressure_topn="0.60",
                label_10s="1.05",
            ),
            _row(
                timestamp_ms="4000",
                ticker_mid="1.00",
                top_mid="1.00",
                executable_mid="1.00",
                bybit_mid="",
                owa_mid_topn="0.97",
                microprice_top1="0.98",
                pressure_topn="-0.60",
                label_10s="0.95",
            ),
        ],
        horizons_seconds=[10],
    )

    horizon = report.horizons[10]
    assert horizon.estimators["quidax_owa_mid_topn"].observations == 4
    assert horizon.estimators["quidax_owa_mid_topn"].direction_hit_rate == Decimal("1")
    assert horizon.estimators["quidax_microprice_top1"].observations == 4

    validation_positive = next(
        bucket
        for bucket in horizon.probability_buckets
        if bucket.feature == "quidax_cngn_usd_pressure_topn"
        and bucket.label == "midpoint_up"
        and bucket.split == "validation_40pct"
        and bucket.bucket == "(0.5,1.0]"
    )
    assert validation_positive.observations == 1
    assert validation_positive.event_count == 1
    assert validation_positive.event_probability == Decimal("1")

    validation_negative = next(
        bucket
        for bucket in horizon.probability_buckets
        if bucket.feature == "quidax_cngn_usd_pressure_topn"
        and bucket.label == "midpoint_up"
        and bucket.split == "validation_40pct"
        and bucket.bucket == "[-1.0,-0.5)"
    )
    assert validation_negative.observations == 1
    assert validation_negative.event_count == 0
    assert validation_negative.event_probability == Decimal("0")

    markdown = render_markdown_report(report)
    assert "Probability calibration buckets:" in markdown
    assert (
        "| feature | label | split | bucket | observations | event_probability | mean_move |"
        in markdown
    )
    assert (
        "| quidax_cngn_usd_pressure_topn | midpoint_up | validation_40pct | "
        "(0.5,1.0] | 1 | 1 | 0.05 |"
        in markdown
    )


def test_render_markdown_report_includes_metrics_table() -> None:
    report = analyze_markouts(
        [
            _row(
                timestamp_ms="1000",
                ticker_mid="0.0011",
                top_mid="0.00105",
                executable_mid="0.0010",
                bybit_mid="",
                label_10s="0.0012",
            )
        ],
        horizons_seconds=[10],
    )

    markdown = render_markdown_report(report)

    assert "# Fair Price Markout Report" in markdown
    assert "## Horizon 10s" in markdown
    assert "Median label lag ms: 1000" in markdown
    assert "| quidax_ticker_mid | 1 | 0.0001 | -0.0001 | 0.0001 | 1 / 1 |" in markdown
    assert "Rows analyzed: 1" in markdown


def test_analyze_markouts_counts_zero_label_lag_as_observed() -> None:
    report = analyze_markouts(
        [
            _row(
                timestamp_ms="1000",
                ticker_mid="0.0011",
                top_mid="0.00105",
                executable_mid="0.0010",
                bybit_mid="1000",
                label_10s="0.0012",
                label_10s_lag_ms="0",
            )
        ],
        horizons_seconds=[10],
    )

    assert report.horizons[10].label_lag_observations == 1
    assert report.horizons[10].median_label_lag_ms == Decimal("0")
    assert report.horizons[10].max_label_lag_ms == Decimal("0")


def test_analyze_markouts_reports_dataset_quality() -> None:
    report = analyze_markouts(
        [
            _row(
                timestamp_ms="1000",
                ticker_mid="0.0011",
                top_mid="0.00105",
                executable_mid="0.0010",
                bybit_mid="1000",
                bybit_age_ms="0",
                label_10s="0.0012",
            ),
            _row(
                timestamp_ms="2000",
                ticker_mid="0.0012",
                top_mid="0.00110",
                executable_mid="0.0011",
                bybit_mid="1000",
                bybit_age_ms="5000",
                label_10s="0.0012",
            ),
            _row(
                timestamp_ms="4000",
                ticker_mid="0.0012",
                top_mid="0.00110",
                executable_mid="",
                bybit_mid="",
                label_10s="0.0012",
            ),
        ],
        horizons_seconds=[10],
    )

    assert report.quality.median_row_gap_ms == Decimal("1500")
    assert report.quality.max_row_gap_ms == Decimal("2000")
    assert report.quality.current_executable_observations == 2
    assert report.quality.missing_current_executable_count == 1
    assert report.quality.unique_current_executable_mid_count == 2
    assert report.quality.bybit_observations == 2
    assert report.quality.median_bybit_age_ms == Decimal("2500")
    assert report.quality.max_bybit_age_ms == Decimal("5000")

    markdown = render_markdown_report(report)
    assert "## Markout Dataset Quality" in markdown
    assert "Unique current executable mids: 2" in markdown
    assert "Median row gap ms: 1500" in markdown
    assert "Median Bybit age ms: 2500" in markdown
