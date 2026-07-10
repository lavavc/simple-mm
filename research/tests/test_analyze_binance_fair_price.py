from decimal import Decimal

from research.scripts.analyze_binance_fair_price import (
    BinanceReferencePoint,
    QuidaxTopBookPoint,
    analyze_policy_rows,
    build_policy_rows,
    load_quidax_top_book_json,
    load_reference_price_file,
    render_markdown_report,
)


def test_build_policy_rows_join_binance_reference_and_future_quidax_labels() -> None:
    quidax_rows = [
        QuidaxTopBookPoint(
            timestamp_ms=1000,
            bid=Decimal("1380"),
            ask=Decimal("1400"),
            mid=Decimal("1390"),
            spread_bps=Decimal("143.884892"),
        ),
        QuidaxTopBookPoint(
            timestamp_ms=61000,
            bid=Decimal("1395"),
            ask=Decimal("1415"),
            mid=Decimal("1405"),
            spread_bps=Decimal("142.348754"),
        ),
    ]
    reference_rows = [
        BinanceReferencePoint(timestamp_ms=900, price=Decimal("1400")),
        BinanceReferencePoint(timestamp_ms=30000, price=Decimal("1410")),
    ]

    rows = build_policy_rows(
        quidax_rows,
        reference_rows,
        horizons_seconds=[60],
        max_reference_age_ms=60_000,
        max_label_lag_ms=5_000,
    )

    assert rows[0]["timestamp_ms"] == "1000"
    assert rows[0]["quidax_mid"] == "1390"
    assert rows[0]["binance_reference_price"] == "1400"
    assert rows[0]["binance_reference_age_ms"] == "100"
    assert rows[0]["quidax_mid_minus_binance_bps"] == "-71.428571"
    assert rows[0]["quidax_bid_offset_bps"] == "-142.857143"
    assert rows[0]["quidax_ask_offset_bps"] == "0.000000"
    assert rows[0]["label_60s_quidax_mid"] == "1405"
    assert rows[0]["label_60s_lag_ms"] == "0"
    assert rows[1]["label_60s_quidax_mid"] == ""


def test_analyze_policy_rows_compares_binance_anchor_to_stale_quidax_mid() -> None:
    rows = [
        {
            "timestamp_ms": "1000",
            "quidax_mid": "1390",
            "binance_reference_price": "1400",
            "binance_reference_age_ms": "100",
            "quidax_mid_minus_binance_bps": "-71.428571",
            "quidax_spread_bps": "143.884892",
            "label_60s_quidax_mid": "1405",
            "label_60s_lag_ms": "0",
        },
        {
            "timestamp_ms": "61000",
            "quidax_mid": "1405",
            "binance_reference_price": "1410",
            "binance_reference_age_ms": "31000",
            "quidax_mid_minus_binance_bps": "-35.460993",
            "quidax_spread_bps": "142.348754",
            "label_60s_quidax_mid": "",
            "label_60s_lag_ms": "",
        },
    ]

    report = analyze_policy_rows(rows, horizons_seconds=[60])

    assert report.total_rows == 2
    assert report.reference_observations == 2
    assert report.median_reference_age_ms == Decimal("15550")
    assert report.median_abs_mid_offset_bps == Decimal("53.444782")
    horizon = report.horizons[60]
    assert horizon.label_count == 1
    assert horizon.estimators["current_quidax_mid"].mae == Decimal("15")
    assert horizon.estimators["binance_reference"].mae == Decimal("5")


def test_loaders_accept_quidax_json_and_reference_json(tmp_path) -> None:
    quidax_path = tmp_path / "quidax.json"
    reference_path = tmp_path / "binance.json"
    quidax_path.write_text(
        '[{"ts": 1000, "bid": 1380, "ask": 1400, "mid": 1390, "spread_bps": 143.88}]'
    )
    reference_path.write_text('[{"ts": 900, "reference_price": 1400}]')

    assert load_quidax_top_book_json(quidax_path) == [
        QuidaxTopBookPoint(
            timestamp_ms=1000,
            bid=Decimal("1380"),
            ask=Decimal("1400"),
            mid=Decimal("1390"),
            spread_bps=Decimal("143.88"),
        )
    ]
    assert load_reference_price_file(reference_path) == [
        BinanceReferencePoint(timestamp_ms=900, price=Decimal("1400"))
    ]


def test_render_markdown_report_names_anchor_not_depth_execution() -> None:
    rows = build_policy_rows(
        [
            QuidaxTopBookPoint(
                timestamp_ms=1000,
                bid=Decimal("1380"),
                ask=Decimal("1400"),
                mid=Decimal("1390"),
                spread_bps=Decimal("143.884892"),
            ),
            QuidaxTopBookPoint(
                timestamp_ms=61000,
                bid=Decimal("1395"),
                ask=Decimal("1415"),
                mid=Decimal("1405"),
                spread_bps=Decimal("142.348754"),
            ),
        ],
        [BinanceReferencePoint(timestamp_ms=900, price=Decimal("1400"))],
        horizons_seconds=[60],
        max_reference_age_ms=60_000,
        max_label_lag_ms=5_000,
    )

    markdown = render_markdown_report(analyze_policy_rows(rows, horizons_seconds=[60]))

    assert "Label: future Quidax managed top-book midpoint." in markdown
    assert "Depth-walk execution is not tested by this report." in markdown
    assert "| binance_reference | 1 | 5.000000 | 5.000000 |" in markdown
