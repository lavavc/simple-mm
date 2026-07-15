from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from research.cross_pool.contracts import CrossPoolContractError, GapQuantiles
from research.cross_pool.io import load_pool_events
from research.cross_pool.qa import validate_stream
from research.scripts.build_pool_feature_table import output_fields

FEATURE_FIELDS = (
    "timestamp_ms",
    "pool",
    "block_number",
    "tx_hash",
    "log_index",
    "raw_sqrt_mid",
    "fee_adjusted_bid",
    "fee_adjusted_ask",
    "stored_cngn_usd_price",
    "stored_price_model",
)


def test_load_pool_events_preserves_canonical_and_diagnostic_prices(
    tmp_path: Path,
) -> None:
    path = _write_feature_csv(
        tmp_path,
        [
            _row(block_number="99"),
            _row(
                timestamp_ms="2000",
                block_number="100",
                tx_hash="0x2",
                log_index="2",
                raw_sqrt_mid="1.01",
                fee_adjusted_bid="1.008",
                fee_adjusted_ask="1.012",
                stored_cngn_usd_price="1.01",
            ),
        ],
    )

    events = load_pool_events(path, expected_pool="uni-base")
    quality = validate_stream(events, transition_block=100)

    assert events[0].raw_mid == Decimal("1")
    assert events[0].stored_cngn_usd_price == Decimal("1")
    assert events[1].stored_cngn_usd_price == Decimal("1.01")
    assert quality.pool == "uni-base"
    assert quality.rows == 2
    assert quality.first_timestamp_ms == 1000
    assert quality.last_timestamp_ms == 2000
    assert quality.update_gap_quantiles_ms == GapQuantiles(p50=1000, p95=1000, p99=1000)
    assert quality.pre_transition_rows == 1
    assert quality.post_transition_rows == 1


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("duplicate_identity", "duplicate pool event identity"),
        ("non_monotonic_time", "timestamps must be strictly increasing"),
        ("missing_mid", "raw_sqrt_mid"),
        ("wrong_pool", "expected pool uni-base"),
        ("crossed_band", "fee-adjusted band"),
        ("unsupported_model", "stored_price_model"),
    ],
)
def test_load_pool_events_fails_closed(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    rows = [
        _row(),
        _row(
            timestamp_ms="2000",
            block_number="101",
            tx_hash="0x2",
            log_index="2",
        ),
    ]
    if mutation == "duplicate_identity":
        rows[1]["tx_hash"] = rows[0]["tx_hash"]
        rows[1]["log_index"] = rows[0]["log_index"]
    elif mutation == "non_monotonic_time":
        rows[1]["timestamp_ms"] = rows[0]["timestamp_ms"]
    elif mutation == "missing_mid":
        rows[0]["raw_sqrt_mid"] = ""
    elif mutation == "wrong_pool":
        rows[0]["pool"] = "uni-bsc"
    elif mutation == "crossed_band":
        rows[0]["fee_adjusted_bid"] = "1.001"
    elif mutation == "unsupported_model":
        rows[0]["stored_price_model"] = "swap_amount_ratio"
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        load_pool_events(_write_feature_csv(tmp_path, rows), expected_pool="uni-base")


@pytest.mark.parametrize(
    "field",
    [
        "raw_sqrt_mid",
        "fee_adjusted_bid",
        "fee_adjusted_ask",
        "stored_cngn_usd_price",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_load_pool_events_rejects_non_positive_or_non_finite_prices(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    row = _row(**{field: value})

    with pytest.raises(CrossPoolContractError, match=field):
        load_pool_events(_write_feature_csv(tmp_path, [row]), expected_pool="uni-base")


def test_load_pool_events_rejects_missing_columns(tmp_path: Path) -> None:
    path = _write_feature_csv(tmp_path, [_row()], fields=FEATURE_FIELDS[:-1])

    with pytest.raises(CrossPoolContractError, match="missing required columns"):
        load_pool_events(path, expected_pool="uni-base")


def test_load_pool_events_rejects_blank_stored_diagnostic_price(tmp_path: Path) -> None:
    path = _write_feature_csv(tmp_path, [_row(stored_cngn_usd_price="")])

    with pytest.raises(CrossPoolContractError, match="stored_cngn_usd_price"):
        load_pool_events(path, expected_pool="uni-base")


def test_load_pool_events_preserves_stored_price_mismatch_as_diagnostic(
    tmp_path: Path,
) -> None:
    path = _write_feature_csv(tmp_path, [_row(stored_cngn_usd_price="0.999")])

    event = load_pool_events(path, expected_pool="uni-base")[0]

    assert event.raw_mid == Decimal("1")
    assert event.stored_cngn_usd_price == Decimal("0.999")


def test_load_pool_events_accepts_full_producer_schema_with_blank_optional_fields(
    tmp_path: Path,
) -> None:
    fields = tuple(output_fields((3_600, 86_400, 604_800)))
    path = _write_feature_csv(tmp_path, [_row()], fields=fields)

    events = load_pool_events(path, expected_pool="uni-base")

    assert len(fields) == 46
    assert len(events) == 1


def test_load_pool_events_normalizes_hashes_and_accepts_distinct_logs(
    tmp_path: Path,
) -> None:
    path = _write_feature_csv(
        tmp_path,
        [
            _row(tx_hash="0xAbC"),
            _row(timestamp_ms="1001", block_number="99", tx_hash="0xabc", log_index="2"),
        ],
    )

    events = load_pool_events(path, expected_pool="uni-base")

    assert [event.tx_hash for event in events] == ["0xabc", "0xabc"]
    quality = validate_stream(events, transition_block=99)
    assert quality.update_gap_quantiles_ms == GapQuantiles(p50=1, p95=1, p99=1)
    assert quality.pre_transition_rows == 1
    assert quality.post_transition_rows == 1


def test_load_pool_events_adds_prefix_when_normalizing_unprefixed_hex_hash(
    tmp_path: Path,
) -> None:
    path = _write_feature_csv(tmp_path, [_row(tx_hash="ABCDEF")])

    event = load_pool_events(path, expected_pool="uni-base")[0]

    assert event.tx_hash == "0xabcdef"


def test_load_pool_events_rejects_case_insensitive_duplicate_identity(
    tmp_path: Path,
) -> None:
    path = _write_feature_csv(
        tmp_path,
        [
            _row(tx_hash="0xAbC"),
            _row(timestamp_ms="1001", block_number="99", tx_hash="0xabc"),
        ],
    )

    with pytest.raises(CrossPoolContractError, match="duplicate pool event identity"):
        load_pool_events(path, expected_pool="uni-base")


@pytest.mark.parametrize("field", ["timestamp_ms", "block_number", "log_index"])
def test_load_pool_events_wraps_invalid_integer_fields(
    tmp_path: Path,
    field: str,
) -> None:
    path = _write_feature_csv(tmp_path, [_row(**{field: "not-an-integer"})])

    with pytest.raises(CrossPoolContractError, match=field):
        load_pool_events(path, expected_pool="uni-base")


@pytest.mark.parametrize(
    "field",
    [
        "raw_sqrt_mid",
        "fee_adjusted_bid",
        "fee_adjusted_ask",
        "stored_cngn_usd_price",
    ],
)
def test_load_pool_events_wraps_blank_required_price_fields(
    tmp_path: Path,
    field: str,
) -> None:
    path = _write_feature_csv(tmp_path, [_row(**{field: ""})])

    with pytest.raises(CrossPoolContractError, match=field):
        load_pool_events(path, expected_pool="uni-base")


def test_load_pool_events_wraps_truncated_rows(tmp_path: Path) -> None:
    path = tmp_path / "features.csv"
    path.write_text(",".join(FEATURE_FIELDS) + "\n1000,uni-base\n")

    with pytest.raises(CrossPoolContractError, match="block_number"):
        load_pool_events(path, expected_pool="uni-base")


def test_validate_stream_pins_type7_gap_quantiles(tmp_path: Path) -> None:
    timestamps = (1_000, 1_001, 1_003, 1_103, 2_103)
    rows = [
        _row(
            timestamp_ms=str(timestamp),
            block_number=str(98 + index),
            tx_hash=f"0x{index + 1}",
            log_index=str(index + 1),
        )
        for index, timestamp in enumerate(timestamps)
    ]
    events = load_pool_events(_write_feature_csv(tmp_path, rows), expected_pool="uni-base")

    quality = validate_stream(events, transition_block=100)

    assert quality.update_gap_quantiles_ms == GapQuantiles(p50=51, p95=865, p99=973)


def test_validate_stream_has_no_gap_quantiles_for_one_event(tmp_path: Path) -> None:
    events = load_pool_events(
        _write_feature_csv(tmp_path, [_row()]), expected_pool="uni-base"
    )

    quality = validate_stream(events, transition_block=100)

    assert quality.update_gap_quantiles_ms is None


def test_validate_stream_rejects_empty_events() -> None:
    with pytest.raises(CrossPoolContractError, match="at least one pool event"):
        validate_stream((), transition_block=100)


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "timestamp_ms": "1000",
        "pool": "uni-base",
        "block_number": "98",
        "tx_hash": "0x1",
        "log_index": "1",
        "raw_sqrt_mid": "1",
        "fee_adjusted_bid": "0.998",
        "fee_adjusted_ask": "1.002",
        "stored_cngn_usd_price": "1",
        "stored_price_model": "sqrt_mid",
    }
    row.update(overrides)
    return row


def _write_feature_csv(
    tmp_path: Path,
    rows: list[dict[str, str]],
    *,
    fields: tuple[str, ...] = FEATURE_FIELDS,
) -> Path:
    path = tmp_path / "features.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path
