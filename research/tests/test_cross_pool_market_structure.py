from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

import research.backtester.lp_ledger_attribution as ledger_attribution
from research.backtester.lp_ledger_attribution import (
    RpcVerificationMode,
    ledger_coverage_path,
    load_ledger_attribution_rows,
    load_ledger_coverage,
    load_verified_ledger_attribution_rows,
    opening_capital_usd,
    pool_attribution_orientation,
    write_fixture_ledger_coverage,
    write_rpc_ledger_coverage,
)
from research.backtester.pool_price_semantics import raw_sqrt_mid_from_row
from research.cross_pool import market_structure
from research.cross_pool.contracts import CrossPoolContractError, PoolName
from research.cross_pool.market_structure import (
    DistributionSummary,
    VenueStructureSummary,
    inspect_replay_stream,
    replay_end_block_at_or_before,
    serialize_market_structure,
    summarize_market_structure,
)

START_MS = 1_767_571_200_000
END_MS = START_MS + 4_000
SQRT_BASE = 10**30
OWNER_A = "0x" + "aa" * 20
OWNER_B = "0x" + "22" * 20
OWNER_C = "0x" + "33" * 20
ZERO_OWNER = "0x" + "00" * 20
INCEPTION_BLOCKS: dict[PoolName, int] = {
    "uni-base": 42_926_879,
    "uni-bsc": 84_655_203,
}

REPLAY_FIELDS = (
    "block_time",
    "chain",
    "pool_id",
    "event_type",
    "tx_hash",
    "log_index",
    "block_number",
    "sqrt_price_x96",
    "active_liquidity",
    "fee_rate",
    "amount_usd",
    "cngn_usd_price",
    "token0_symbol",
    "token1_symbol",
)

LEDGER_FIELDS = (
    "chain",
    "pool_id",
    "block_number",
    "tx_hash",
    "log_index",
    "event_order",
    "event_type",
    "token_id",
    "lp_owner",
    "liquidity_delta",
    "amount0_actual",
    "amount1_actual",
    "amount0_attribution_source",
    "amount1_attribution_source",
    "amount_attribution_status",
    "cngn_usd_price_at_event",
    "timestamp_ms",
)


def test_replay_inspection_exposes_provenance_interval_and_cutoff_block(
    tmp_path: Path,
) -> None:
    replay_path = _write_csv(
        tmp_path / "base_replay.csv",
        REPLAY_FIELDS,
        _replay_rows("uni-base"),
    )

    evidence = inspect_replay_stream("uni-base", replay_path)

    assert evidence.pool == "uni-base"
    assert evidence.swap_count == 4
    assert evidence.first_timestamp_ms == START_MS
    assert evidence.last_timestamp_ms == END_MS
    assert replay_end_block_at_or_before(
        "uni-base",
        replay_path,
        cutoff_timestamp_ms=END_MS - 1_000,
    ) == INCEPTION_BLOCKS["uni-base"] + 102


def test_market_structure_uses_the_common_swap_interval_and_exact_capital(
    tmp_path: Path,
) -> None:
    base, bsc = _summaries(tmp_path)

    assert base.pool == "uni-base"
    assert bsc.pool == "uni-bsc"
    assert base.activity_start_timestamp_ms == START_MS
    assert base.activity_end_timestamp_ms == END_MS
    assert base.ledger_cutoff_timestamp_ms == END_MS
    assert base.swap_count == 4
    assert base.meaningful_move_count == 2
    assert base.fee_rate == Decimal("0.0015")
    assert base.update_gaps_ms == DistributionSummary(
        count=3,
        minimum=Decimal("0"),
        median=Decimal("1000"),
        p95=Decimal("2800.00"),
        p99=Decimal("2960.00"),
        maximum=Decimal("3000"),
    )
    assert base.active_liquidity == DistributionSummary(
        count=4,
        minimum=Decimal("10"),
        median=Decimal("25.0"),
        p95=Decimal("38.50"),
        p99=Decimal("39.70"),
        maximum=Decimal("40"),
    )
    assert base.volume_usd == DistributionSummary(
        count=4,
        minimum=Decimal("1"),
        median=Decimal("2.5"),
        p95=Decimal("3.85"),
        p99=Decimal("3.97"),
        maximum=Decimal("4"),
    )
    assert base.total_volume_usd == Decimal("10")

    assert base.ledger_rows == 6
    assert base.opening_count == 5
    assert base.exact_opening_count == 4
    assert base.ambiguous_opening_count == 1
    assert base.known_owner_count == 2
    assert base.exact_unknown_owner_opening_count == 1
    assert base.exact_opening_capital_usd == Decimal("120")
    assert base.exact_known_owner_capital_usd == Decimal("100")
    assert base.exact_known_owner_capital_coverage == Decimal(5) / Decimal(6)
    assert base.ambiguous_opening_liquidity_share == Decimal(5) / Decimal(17)
    assert base.exact_opening_capital_top_owner_share == Decimal("0.75")
    assert base.exact_opening_capital_top_three_share == Decimal("1")
    assert base.exact_opening_capital_hhi == Decimal("0.6250")


def test_market_structure_analysis_preserves_verified_coverage_evidence(
    tmp_path: Path,
) -> None:
    base_replay = _write_csv(
        tmp_path / "base_replay.csv",
        REPLAY_FIELDS,
        _replay_rows("uni-base"),
    )
    bsc_replay = _write_csv(
        tmp_path / "bsc_replay.csv",
        REPLAY_FIELDS,
        _replay_rows("uni-bsc", include_outer_rows=True),
    )
    base_ledger = _write_ledger(tmp_path, "uni-base")
    bsc_ledger = _write_ledger(tmp_path, "uni-bsc")

    analysis = market_structure.analyze_market_structure(
        base_replay_path=base_replay,
        bsc_replay_path=bsc_replay,
        base_ledger_path=base_ledger,
        bsc_ledger_path=bsc_ledger,
    )

    assert tuple(row.pool for row in analysis.venues) == ("uni-base", "uni-bsc")
    assert tuple(row.pool for row in analysis.ledger_coverage) == (
        "uni-base",
        "uni-bsc",
    )
    assert analysis.ledger_coverage[0].sidecar_sha256 == hashlib.sha256(
        ledger_coverage_path(base_ledger).read_bytes()
    ).hexdigest()


def test_common_interval_excludes_unmatched_activity_and_post_cutoff_capital(
    tmp_path: Path,
) -> None:
    base, bsc = _summaries(tmp_path)

    assert base.swap_count == bsc.swap_count == 4
    assert bsc.total_volume_usd == Decimal("10")
    assert bsc.activity_start_timestamp_ms == START_MS
    assert bsc.activity_end_timestamp_ms == END_MS
    assert OWNER_C not in serialize_market_structure((base, bsc))
    assert base.exact_opening_capital_usd == Decimal("120")


def test_non_swap_rows_do_not_enter_activity_liquidity_or_volume(
    tmp_path: Path,
) -> None:
    base_rows = list(_replay_rows("uni-base"))
    base_rows.insert(
        2,
        _replay_row(
            "uni-base",
            timestamp_ms=START_MS + 1_000,
            block_number=INCEPTION_BLOCKS["uni-base"] + 101,
            log_index=2,
            sqrt_price_x96=9 * SQRT_BASE,
            active_liquidity=999,
            amount_usd="999",
            event_type="mint",
        ),
    )

    base, _ = _summaries(tmp_path, base_replay_rows=tuple(base_rows))

    assert base.swap_count == 4
    assert base.meaningful_move_count == 2
    assert base.active_liquidity.maximum == Decimal("40")
    assert base.volume_usd.maximum == Decimal("4")
    assert base.total_volume_usd == Decimal("10")


@pytest.mark.parametrize(
    ("pool", "expected"),
    (
        ("uni-base", Decimal("2.7000")),
        ("uni-bsc", Decimal("2.7000")),
    ),
)
def test_opening_capital_uses_the_frozen_pool_orientation(
    pool: PoolName,
    expected: Decimal,
) -> None:
    if pool == "uni-base":
        amount0, amount1 = Decimal("1000"), Decimal("2")
    else:
        amount0, amount1 = Decimal("2"), Decimal("1000")

    assert (
        opening_capital_usd(
            pool,
            amount0_actual=amount0,
            amount1_actual=amount1,
            cngn_usd_price=Decimal("0.0007"),
        )
        == expected
    )


def test_ledger_loader_normalizes_owners_and_preserves_raw_attribution(
    tmp_path: Path,
) -> None:
    path = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        (
            _ledger_row(
                "uni-base",
                block_number=INCEPTION_BLOCKS["uni-base"],
                log_index=1,
                event_order=0,
                timestamp_ms=START_MS,
                owner="0x" + "AA" * 20,
                liquidity_delta="10",
                amount0_actual="10",
                amount1_actual="0",
                status="exact",
                amount0_source="transfer_a",
                amount1_source="transfer_b",
            ),
            _ledger_row(
                "uni-base",
                block_number=INCEPTION_BLOCKS["uni-base"] + 1,
                log_index=1,
                event_order=0,
                timestamp_ms=START_MS + 1_000,
                owner=ZERO_OWNER,
                liquidity_delta="5",
                amount0_actual="0",
                amount1_actual="0",
                status="ambiguous_missing_pair",
            ),
            _ledger_row(
                "uni-base",
                block_number=INCEPTION_BLOCKS["uni-base"] + 2,
                log_index=1,
                event_order=0,
                timestamp_ms=START_MS + 2_000,
                owner="malformed",
                liquidity_delta="-10",
                amount0_actual="0",
                amount1_actual="0",
                status="not_applicable",
            ),
        ),
    )

    rows = load_ledger_attribution_rows("uni-base", path)

    assert rows[0].owner == OWNER_A
    assert rows[0].attribution_class == "exact"
    assert rows[0].opening_capital_usd == Decimal("10")
    assert rows[0].amount0_attribution_source == "transfer_a"
    assert rows[0].amount1_attribution_source == "transfer_b"
    assert rows[1].owner is None
    assert rows[1].attribution_class == "ambiguous"
    assert rows[1].opening_capital_usd is None
    assert rows[2].owner is None
    assert rows[2].attribution_class == "other"


def test_meaningful_move_threshold_is_inclusive() -> None:
    assert market_structure._meets_meaningful_move_threshold(Decimal("10"))
    assert not market_structure._meets_meaningful_move_threshold(
        Decimal("9.999999999999999999999999999")
    )


def test_bsc_canonical_mid_matches_the_shared_price_semantics() -> None:
    orientation = pool_attribution_orientation("uni-bsc")
    sqrt_price_x96 = 2_959_134_576_890_254_664_750_393
    expected = raw_sqrt_mid_from_row(
        {
            "sqrt_price_x96": str(sqrt_price_x96),
            "chain": orientation.chain,
            "token0_symbol": orientation.token0_symbol,
            "token1_symbol": orientation.token1_symbol,
        }
    )

    assert (
        market_structure._canonical_mid_from_sqrt_price_x96(
            sqrt_price_x96,
            pool="uni-bsc",
        )
        == expected
    )


def test_serialization_is_canonical_and_never_contains_owner_addresses(
    tmp_path: Path,
) -> None:
    summaries = _summaries(tmp_path)

    forward = serialize_market_structure(summaries)
    reverse = serialize_market_structure(tuple(reversed(summaries)))
    payload = json.loads(forward)

    assert forward == reverse
    assert forward.endswith("\n")
    assert [row["pool"] for row in payload["venues"]] == [
        "uni-base",
        "uni-bsc",
    ]
    assert payload["venues"][0]["exact_opening_capital_hhi"] == "0.6250"
    for owner in (OWNER_A, OWNER_B, OWNER_C, ZERO_OWNER):
        assert owner not in forward
        assert owner.upper() not in forward
    assert "owner_1" not in forward


def test_market_structure_is_independent_of_the_callers_decimal_context(
    tmp_path: Path,
) -> None:
    rows = list(_ledger_rows("uni-base", include_post_cutoff=True))
    rows[0]["liquidity_delta"] = "50.12345678901234567890123456789"
    rows[0]["amount0_actual"] = "50.12345678901234567890123456789"
    materialized = tuple(rows)

    with localcontext() as context:
        context.prec = 9
        low_precision = _summaries(tmp_path, base_ledger_rows=materialized)
    with localcontext() as context:
        context.prec = 50
        high_precision = _summaries(tmp_path, base_ledger_rows=materialized)

    assert serialize_market_structure(low_precision) == serialize_market_structure(high_precision)


def test_market_structure_contract_rejects_stale_derived_fields(
    tmp_path: Path,
) -> None:
    base, _ = _summaries(tmp_path)

    with pytest.raises(CrossPoolContractError, match="coverage"):
        replace(base, exact_known_owner_capital_coverage=Decimal("0.5"))
    with pytest.raises(CrossPoolContractError, match="update-gap count"):
        replace(base, swap_count=5)
    with pytest.raises(CrossPoolContractError, match="nonnegative"):
        DistributionSummary(
            count=1,
            minimum=Decimal("-1"),
            median=Decimal("-1"),
            p95=Decimal("-1"),
            p99=Decimal("-1"),
            maximum=Decimal("-1"),
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("duplicate_identity", "duplicate replay identity"),
        ("reversed_order", "block/log order"),
        ("backward_time", "nondecreasing"),
        ("changed_fee", "one fee rate"),
        ("uniform_wrong_fee", "frozen fee rate"),
        ("zero_sqrt", "sqrt_price_x96"),
        ("zero_liquidity", "active_liquidity"),
        ("fractional_liquidity", "active_liquidity must be an integer"),
        ("negative_volume", "amount_usd"),
        ("nonfinite", "amount_usd"),
        ("wrong_chain", "orientation"),
    ),
)
def test_replay_validation_fails_closed(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    base_rows = list(_replay_rows("uni-base"))
    if mutation == "duplicate_identity":
        base_rows[1]["tx_hash"] = base_rows[0]["tx_hash"]
        base_rows[1]["log_index"] = base_rows[0]["log_index"]
    elif mutation == "reversed_order":
        base_rows[1]["block_number"] = str(INCEPTION_BLOCKS["uni-base"] + 99)
    elif mutation == "backward_time":
        base_rows[1]["block_time"] = _iso_timestamp(START_MS - 1)
    elif mutation == "changed_fee":
        base_rows[1]["fee_rate"] = "0.003"
    elif mutation == "uniform_wrong_fee":
        for row in base_rows:
            row["fee_rate"] = "0.003"
    elif mutation == "zero_sqrt":
        base_rows[1]["sqrt_price_x96"] = "0"
    elif mutation == "zero_liquidity":
        base_rows[1]["active_liquidity"] = "0"
    elif mutation == "fractional_liquidity":
        base_rows[1]["active_liquidity"] = "1.5"
    elif mutation == "negative_volume":
        base_rows[1]["amount_usd"] = "-1"
    elif mutation == "nonfinite":
        base_rows[1]["amount_usd"] = "NaN"
    elif mutation == "wrong_chain":
        base_rows[1]["chain"] = "bsc"
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        _summaries(tmp_path, base_replay_rows=tuple(base_rows))


def test_replay_requires_canonical_raw_columns_and_two_common_swaps(
    tmp_path: Path,
) -> None:
    base_path = _write_csv(
        tmp_path / "base_replay.csv",
        tuple(field for field in REPLAY_FIELDS if field != "sqrt_price_x96"),
        _replay_rows("uni-base"),
    )
    bsc_path = _write_csv(
        tmp_path / "bsc_replay.csv",
        REPLAY_FIELDS,
        _replay_rows("uni-bsc"),
    )

    with pytest.raises(CrossPoolContractError, match="sqrt_price_x96"):
        summarize_market_structure(
            base_replay_path=base_path,
            bsc_replay_path=bsc_path,
            base_ledger_path=_write_ledger(tmp_path, "uni-base"),
            bsc_ledger_path=_write_ledger(tmp_path, "uni-bsc"),
        )

    one_common = tuple(row for index, row in enumerate(_replay_rows("uni-base")) if index == 0)
    with pytest.raises(CrossPoolContractError, match="at least two swaps"):
        _summaries(tmp_path, base_replay_rows=one_common)


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("duplicate_identity", "duplicate ledger identity"),
        ("reversed_order", "canonical order"),
        ("backward_time", "nondecreasing"),
        ("negative_amount", "amount0_actual"),
        ("zero_price", "positive price"),
        ("unsupported_opening", "unsupported attribution"),
        ("wrong_chain", "orientation"),
        ("wrong_pool_id", "orientation"),
        ("nonfinite", "amount1_actual"),
    ),
)
def test_ledger_validation_fails_closed(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    rows = list(_ledger_rows("uni-base"))
    if mutation == "duplicate_identity":
        rows[1]["tx_hash"] = rows[0]["tx_hash"]
        rows[1]["log_index"] = rows[0]["log_index"]
        rows[1]["event_order"] = rows[0]["event_order"]
    elif mutation == "reversed_order":
        rows[1]["block_number"] = str(INCEPTION_BLOCKS["uni-base"] - 1)
    elif mutation == "backward_time":
        rows[1]["timestamp_ms"] = str(START_MS - 2_000)
    elif mutation == "negative_amount":
        rows[1]["amount0_actual"] = "-1"
    elif mutation == "zero_price":
        rows[1]["cngn_usd_price_at_event"] = "0"
    elif mutation == "unsupported_opening":
        rows[1]["amount_attribution_status"] = "not_applicable"
    elif mutation == "wrong_chain":
        rows[1]["chain"] = "bsc"
    elif mutation == "wrong_pool_id":
        rows[1]["pool_id"] = "0xwrong"
    elif mutation == "nonfinite":
        rows[1]["amount1_actual"] = "Infinity"
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)
    ledger_path = _write_csv(tmp_path / "base_ledger.csv", LEDGER_FIELDS, rows)

    with pytest.raises(CrossPoolContractError, match=match):
        load_ledger_attribution_rows("uni-base", ledger_path)


def test_ledger_loader_requires_owner_and_rejects_unknown_pool(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "ledger.csv",
        tuple(field for field in LEDGER_FIELDS if field != "lp_owner"),
        _ledger_rows("uni-base"),
    )

    with pytest.raises(CrossPoolContractError, match="lp_owner"):
        load_ledger_attribution_rows("uni-base", path)
    with pytest.raises(CrossPoolContractError, match="unsupported pool"):
        opening_capital_usd(
            "unknown",
            amount0_actual=Decimal("1"),
            amount1_actual=Decimal("1"),
            cngn_usd_price=Decimal("1"),
        )


def test_ledger_loader_rejects_a_canonical_but_truncated_history(
    tmp_path: Path,
) -> None:
    rows = _ledger_rows("uni-base")[1:]
    path = _write_csv(tmp_path / "ledger.csv", LEDGER_FIELDS, rows)

    with pytest.raises(CrossPoolContractError, match="pool inception block"):
        load_ledger_attribution_rows("uni-base", path)


def test_ledger_coverage_sidecar_is_canonical_and_strict(tmp_path: Path) -> None:
    ledger = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-base"),
    )
    _write_verified_coverage(ledger, "uni-base")
    sidecar = ledger_coverage_path(ledger)
    first = sidecar.read_bytes()
    _write_verified_coverage(ledger, "uni-base")

    assert sidecar.read_bytes() == first
    assert first.endswith(b"\n")
    assert b": " not in first
    assert load_ledger_coverage("uni-base", ledger).verification_mode == "rpc_verified"

    sidecar.write_bytes(first[:-1] + b"\r\n")
    with pytest.raises(CrossPoolContractError, match="canonical JSON bytes"):
        load_ledger_coverage("uni-base", ledger)

    payload = json.loads(first)
    sidecar.write_text(json.dumps(payload, indent=2) + "\n")
    with pytest.raises(CrossPoolContractError, match="canonical JSON bytes"):
        load_ledger_coverage("uni-base", ledger)

    payload = json.loads(first)
    payload["unexpected"] = True
    _write_canonical_json(sidecar, payload)
    with pytest.raises(CrossPoolContractError, match="unknown fields"):
        load_ledger_coverage("uni-base", ledger)

    sidecar.write_text('{"schema_version":"1.0.0","schema_version":"1.0.0"}\n')
    with pytest.raises(CrossPoolContractError, match="duplicate JSON key"):
        load_ledger_coverage("uni-base", ledger)

    sidecar.write_bytes(first.replace(b'"chain_id":8453', b'"chain_id":NaN'))
    with pytest.raises(CrossPoolContractError, match="non-finite JSON constant"):
        load_ledger_coverage("uni-base", ledger)


def test_fixture_coverage_range_must_contain_observed_ledger_rows(
    tmp_path: Path,
) -> None:
    ledger = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-base"),
    )

    with pytest.raises(CrossPoolContractError, match="outside the requested block range"):
        write_fixture_ledger_coverage(
            "uni-base",
            ledger,
            requested_start_block=INCEPTION_BLOCKS["uni-base"] + 1,
            requested_end_block=INCEPTION_BLOCKS["uni-base"] + 1_000,
            fixture_input_sha256={
                "decoded_actions": "1" * 64,
                "ownership_events": "2" * 64,
                "price_events": "3" * 64,
            },
        )


def test_verified_ledger_load_uses_one_immutable_ledger_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-base"),
    )
    _write_verified_coverage(ledger, "uni-base")
    sidecar = ledger_coverage_path(ledger)
    expected_sidecar_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    original_read = ledger_attribution._read_path_bytes
    ledger_reads = 0

    def mutate_after_ledger_snapshot(path: Path, *, label: str) -> bytes:
        nonlocal ledger_reads
        raw_bytes = original_read(path, label=label)
        if path == ledger:
            ledger_reads += 1
            ledger.write_bytes(raw_bytes + b"\n")
        return raw_bytes

    monkeypatch.setattr(
        ledger_attribution,
        "_read_path_bytes",
        mutate_after_ledger_snapshot,
    )

    loaded = load_verified_ledger_attribution_rows(
        "uni-base",
        ledger,
        required_end_block=INCEPTION_BLOCKS["uni-base"] + 103,
        required_end_timestamp_ms=END_MS,
    )

    assert ledger_reads == 1
    assert len(loaded.rows) == len(_ledger_rows("uni-base"))
    assert loaded.coverage.sidecar_sha256 == expected_sidecar_sha
    assert loaded.coverage.chain_id == 8453
    assert loaded.coverage.candidate_scan_status == "producer_attested"


def test_candidate_set_is_canonical_and_digest_bound(tmp_path: Path) -> None:
    ledger = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-base"),
    )
    _write_verified_coverage(
        ledger,
        "uni-base",
        candidate_transaction_hashes=(
            "0X" + "AA" * 32,
            "0x" + "11" * 32,
        ),
    )
    sidecar = ledger_coverage_path(ledger)
    payload = json.loads(sidecar.read_bytes())

    assert payload["candidate_transaction_hashes"] == [
        "0x" + "11" * 32,
        "0x" + "aa" * 32,
    ]
    payload["candidate_transaction_hashes"].reverse()
    _write_canonical_json(sidecar, payload)
    with pytest.raises(CrossPoolContractError, match="canonical order"):
        load_ledger_coverage("uni-base", ledger)

    with pytest.raises(CrossPoolContractError, match="must be unique"):
        _write_verified_coverage(
            ledger,
            "uni-base",
            candidate_transaction_hashes=(
                "0x" + "AA" * 32,
                "0x" + "aa" * 32,
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("chain_id", 56, "pool orientation"),
        ("pool_id", "0x" + "ff" * 32, "pool orientation"),
        ("covered_end_block_hash", "0x1234", "32-byte hex hash"),
        ("ledger_rows", 999, "row facts"),
        ("candidate_transactions_sha256", "0" * 64, "digest is inconsistent"),
    ),
)
def test_coverage_identity_and_attestation_mutations_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    match: str,
) -> None:
    ledger = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-base"),
    )
    _write_verified_coverage(ledger, "uni-base")
    sidecar = ledger_coverage_path(ledger)
    payload = json.loads(sidecar.read_bytes())
    payload[field] = value
    _write_canonical_json(sidecar, payload)

    with pytest.raises(CrossPoolContractError, match=match):
        load_ledger_coverage("uni-base", ledger)


def test_verified_coverage_start_must_equal_pool_inception(tmp_path: Path) -> None:
    ledger = _write_csv(
        tmp_path / "ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-base"),
    )
    _write_verified_coverage(ledger, "uni-base")
    sidecar = ledger_coverage_path(ledger)
    payload = json.loads(sidecar.read_bytes())
    payload["covered_start_block"] = INCEPTION_BLOCKS["uni-base"] - 1
    _write_canonical_json(sidecar, payload)

    with pytest.raises(CrossPoolContractError, match="begin at pool inception"):
        load_verified_ledger_attribution_rows(
            "uni-base",
            ledger,
            required_end_block=INCEPTION_BLOCKS["uni-base"] + 103,
            required_end_timestamp_ms=END_MS,
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("missing", "coverage sidecar"),
        ("ledger_hash", "ledger SHA-256"),
        ("short_block", "common-interval swap block"),
        ("short_time", "common activity end"),
        ("candidate_list", "full RPC scan"),
        ("fixture", "full RPC scan"),
    ),
)
def test_market_structure_requires_verified_tail_coverage(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    with pytest.raises(CrossPoolContractError, match=match):
        _summaries(tmp_path, base_coverage_mutation=mutation)


def test_quiet_pool_coverage_must_reach_the_other_pool_common_cutoff(
    tmp_path: Path,
) -> None:
    bsc_rows = tuple(
        row
        for row in _replay_rows("uni-bsc", include_outer_rows=True)
        if row["block_time"] != _iso_timestamp(END_MS)
    )

    with pytest.raises(CrossPoolContractError, match="common activity end"):
        _summaries(
            tmp_path,
            bsc_replay_rows=bsc_rows,
            bsc_coverage_end_timestamp_ms=END_MS - 1,
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("no_openings", "tracked opening liquidity"),
        ("zero_exact_capital", "positive exact opening capital"),
        ("no_known_capital", "positive exact known-owner capital"),
    ),
)
def test_undefined_owner_concentration_fails_closed(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    rows = list(_ledger_rows("uni-base"))
    if mutation == "no_openings":
        rows = [
            _ledger_row(
                "uni-base",
                block_number=INCEPTION_BLOCKS["uni-base"],
                log_index=1,
                event_order=0,
                timestamp_ms=START_MS - 1_000,
                owner=OWNER_A,
                liquidity_delta="-1",
                amount0_actual="0",
                amount1_actual="0",
                status="not_applicable",
                event_type="burn_collect",
            )
        ]
    elif mutation == "zero_exact_capital":
        for row in rows:
            if row["amount_attribution_status"] == "exact":
                row["amount0_actual"] = "0"
                row["amount1_actual"] = "0"
    elif mutation == "no_known_capital":
        for row in rows:
            if row["amount_attribution_status"] == "exact":
                row["lp_owner"] = ZERO_OWNER
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        _summaries(tmp_path, base_ledger_rows=tuple(rows))


def _summaries(
    tmp_path: Path,
    *,
    base_replay_rows: tuple[dict[str, str], ...] | None = None,
    bsc_replay_rows: tuple[dict[str, str], ...] | None = None,
    base_ledger_rows: tuple[dict[str, str], ...] | None = None,
    base_coverage_mutation: str | None = None,
    bsc_coverage_end_timestamp_ms: int = END_MS + 1_000,
) -> tuple[VenueStructureSummary, VenueStructureSummary]:
    base_replay_path = _write_csv(
        tmp_path / "base_replay.csv",
        REPLAY_FIELDS,
        base_replay_rows or _replay_rows("uni-base"),
    )
    bsc_replay_path = _write_csv(
        tmp_path / "bsc_replay.csv",
        REPLAY_FIELDS,
        bsc_replay_rows or _replay_rows("uni-bsc", include_outer_rows=True),
    )
    base_ledger_path = _write_csv(
        tmp_path / "base_ledger.csv",
        LEDGER_FIELDS,
        base_ledger_rows or _ledger_rows("uni-base", include_post_cutoff=True),
    )
    _write_verified_coverage(base_ledger_path, "uni-base")
    if base_coverage_mutation == "missing":
        ledger_coverage_path(base_ledger_path).unlink()
    elif base_coverage_mutation == "ledger_hash":
        with base_ledger_path.open("a") as handle:
            handle.write("\n")
    elif base_coverage_mutation == "short_block":
        _write_verified_coverage(
            base_ledger_path,
            "uni-base",
            covered_end_block=INCEPTION_BLOCKS["uni-base"] + 102,
        )
    elif base_coverage_mutation == "short_time":
        _write_verified_coverage(
            base_ledger_path,
            "uni-base",
            covered_end_timestamp_ms=END_MS - 1,
        )
    elif base_coverage_mutation == "candidate_list":
        _write_verified_coverage(
            base_ledger_path,
            "uni-base",
            verification_mode="candidate_list_unverified",
        )
    elif base_coverage_mutation == "fixture":
        write_fixture_ledger_coverage(
            "uni-base",
            base_ledger_path,
            requested_start_block=INCEPTION_BLOCKS["uni-base"],
            requested_end_block=INCEPTION_BLOCKS["uni-base"] + 1_000,
            fixture_input_sha256={
                "decoded_actions": "1" * 64,
                "ownership_events": "2" * 64,
                "price_events": "3" * 64,
            },
        )
    elif base_coverage_mutation is not None:
        raise AssertionError(base_coverage_mutation)
    bsc_ledger_path = _write_csv(
        tmp_path / "uni-bsc_ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows("uni-bsc"),
    )
    _write_verified_coverage(
        bsc_ledger_path,
        "uni-bsc",
        covered_end_timestamp_ms=bsc_coverage_end_timestamp_ms,
    )
    return summarize_market_structure(
        base_replay_path=base_replay_path,
        bsc_replay_path=bsc_replay_path,
        base_ledger_path=base_ledger_path,
        bsc_ledger_path=bsc_ledger_path,
    )


def _replay_rows(
    pool: PoolName,
    *,
    include_outer_rows: bool = False,
) -> tuple[dict[str, str], ...]:
    rows = [
        _replay_row(
            pool,
            timestamp_ms=timestamp_ms,
            block_number=INCEPTION_BLOCKS[pool] + 100 + index,
            log_index=1,
            sqrt_price_x96=sqrt_price_x96,
            active_liquidity=10 * (index + 1),
            amount_usd=str(index + 1),
        )
        for index, (timestamp_ms, sqrt_price_x96) in enumerate(
            zip(
                (START_MS, START_MS + 1_000, START_MS + 1_000, END_MS),
                (SQRT_BASE, 2 * SQRT_BASE, 2 * SQRT_BASE, SQRT_BASE),
                strict=True,
            )
        )
    ]
    if include_outer_rows:
        rows.insert(
            0,
            _replay_row(
                pool,
                timestamp_ms=START_MS - 1_000,
                block_number=INCEPTION_BLOCKS[pool] + 99,
                log_index=1,
                sqrt_price_x96=SQRT_BASE,
                active_liquidity=999,
                amount_usd="999",
            ),
        )
        rows.append(
            _replay_row(
                pool,
                timestamp_ms=END_MS + 1_000,
                block_number=INCEPTION_BLOCKS[pool] + 200,
                log_index=1,
                sqrt_price_x96=SQRT_BASE,
                active_liquidity=999,
                amount_usd="999",
            )
        )
    return tuple(rows)


def _replay_row(
    pool: PoolName,
    *,
    timestamp_ms: int,
    block_number: int,
    log_index: int,
    sqrt_price_x96: int,
    active_liquidity: int,
    amount_usd: str,
    event_type: str = "swap",
) -> dict[str, str]:
    orientation = pool_attribution_orientation(pool)
    return {
        "block_time": _iso_timestamp(timestamp_ms),
        "chain": orientation.chain,
        "pool_id": orientation.pool_id,
        "event_type": event_type,
        "tx_hash": f"0x{block_number:062x}{log_index:02x}",
        "log_index": str(log_index),
        "block_number": str(block_number),
        "sqrt_price_x96": str(sqrt_price_x96),
        "active_liquidity": str(active_liquidity),
        "fee_rate": str(orientation.fee_rate),
        "amount_usd": amount_usd,
        "cngn_usd_price": "999999",
        "token0_symbol": orientation.token0_symbol,
        "token1_symbol": orientation.token1_symbol,
    }


def _ledger_rows(
    pool: PoolName,
    *,
    include_post_cutoff: bool = False,
) -> tuple[dict[str, str], ...]:
    if pool == "uni-base":
        inputs = (
            (OWNER_A.upper().replace("0X", "0x"), "50", "exact"),
            (OWNER_A, "25", "exact"),
            (OWNER_B, "25", "exact"),
            ("malformed", "20", "exact"),
            (ZERO_OWNER, "50", "ambiguous_missing_pair"),
        )
    else:
        inputs = (
            (OWNER_A, "40", "exact"),
            (OWNER_B, "10", "exact"),
            (ZERO_OWNER, "10", "ambiguous_missing_pair"),
        )
    rows = [
        _opening_row(
            pool,
            block_number=INCEPTION_BLOCKS[pool] + index,
            timestamp_ms=START_MS - 1_000 + index * 500,
            owner=owner,
            capital=capital,
            status=status,
        )
        for index, (owner, capital, status) in enumerate(inputs)
    ]
    rows.append(
        _ledger_row(
            pool,
            block_number=INCEPTION_BLOCKS[pool] + 80,
            log_index=1,
            event_order=0,
            timestamp_ms=END_MS,
            owner=OWNER_A,
            liquidity_delta="-1",
            amount0_actual="0",
            amount1_actual="0",
            status="not_applicable",
            event_type="burn_collect",
        )
    )
    if include_post_cutoff:
        rows.append(
            _opening_row(
                pool,
                block_number=INCEPTION_BLOCKS[pool] + 100,
                timestamp_ms=END_MS + 1_000,
                owner=OWNER_C,
                capital="1000",
                status="exact",
            )
        )
    return tuple(rows)


def _opening_row(
    pool: PoolName,
    *,
    block_number: int,
    timestamp_ms: int,
    owner: str,
    capital: str,
    status: str,
) -> dict[str, str]:
    if status == "exact":
        amount0, amount1 = (capital, "0") if pool == "uni-base" else ("0", capital)
    else:
        amount0, amount1 = "0", "0"
    return _ledger_row(
        pool,
        block_number=block_number,
        log_index=1,
        event_order=0,
        timestamp_ms=timestamp_ms,
        owner=owner,
        liquidity_delta=capital,
        amount0_actual=amount0,
        amount1_actual=amount1,
        status=status,
    )


def _ledger_row(
    pool: PoolName,
    *,
    block_number: int,
    log_index: int,
    event_order: int,
    timestamp_ms: int,
    owner: str,
    liquidity_delta: str,
    amount0_actual: str,
    amount1_actual: str,
    status: str,
    amount0_source: str = "source0",
    amount1_source: str = "source1",
    event_type: str = "mint",
) -> dict[str, str]:
    orientation = pool_attribution_orientation(pool)
    return {
        "chain": orientation.chain,
        "pool_id": orientation.pool_id,
        "block_number": str(block_number),
        "tx_hash": f"0x{block_number:062x}{log_index:02x}",
        "log_index": str(log_index),
        "event_order": str(event_order),
        "event_type": event_type,
        "token_id": str(block_number),
        "lp_owner": owner,
        "liquidity_delta": liquidity_delta,
        "amount0_actual": amount0_actual,
        "amount1_actual": amount1_actual,
        "amount0_attribution_source": amount0_source,
        "amount1_attribution_source": amount1_source,
        "amount_attribution_status": status,
        "cngn_usd_price_at_event": "1",
        "timestamp_ms": str(timestamp_ms),
    }


def _write_ledger(tmp_path: Path, pool: PoolName) -> Path:
    path = _write_csv(
        tmp_path / f"{pool}_ledger.csv",
        LEDGER_FIELDS,
        _ledger_rows(pool),
    )
    _write_verified_coverage(path, pool)
    return path


def _write_verified_coverage(
    path: Path,
    pool: PoolName,
    *,
    covered_end_block: int | None = None,
    covered_end_timestamp_ms: int = END_MS + 1_000,
    verification_mode: RpcVerificationMode = "rpc_verified",
    candidate_transaction_hashes: tuple[str, ...] = ("0x" + "33" * 32,),
) -> None:
    write_rpc_ledger_coverage(
        pool,
        path,
        chain_id=pool_attribution_orientation(pool).chain_id,
        covered_start_block=INCEPTION_BLOCKS[pool],
        covered_start_block_hash="0x" + "11" * 32,
        covered_start_timestamp_ms=START_MS - 2_000,
        covered_end_block=(
            INCEPTION_BLOCKS[pool] + 1_000 if covered_end_block is None else covered_end_block
        ),
        covered_end_block_hash="0x" + "22" * 32,
        covered_end_timestamp_ms=covered_end_timestamp_ms,
        candidate_transaction_hashes=candidate_transaction_hashes,
        verification_mode=verification_mode,
    )


def _write_csv(
    path: Path,
    fieldnames: tuple[str, ...],
    rows: tuple[dict[str, str], ...] | list[dict[str, str]],
) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_canonical_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def _iso_timestamp(timestamp_ms: int) -> str:
    seconds, milliseconds = divmod(timestamp_ms, 1_000)
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .replace(microsecond=milliseconds * 1_000)
        .isoformat()
    )
