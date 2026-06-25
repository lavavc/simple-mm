from decimal import Decimal

import pytest

from research.backtester.lp_episode_features import build_lp_episode_features
from research.backtester.v4_lp_ledger import LPLedgerRow


def ledger_row(
    event_type: str,
    tx_hash: str,
    liquidity_delta: int,
    timestamp_ms: int,
    *,
    amount0: Decimal = Decimal("0"),
    amount1: Decimal = Decimal("0"),
    collect_amount0: Decimal = Decimal("0"),
    collect_amount1: Decimal = Decimal("0"),
    price: Decimal = Decimal("1"),
) -> LPLedgerRow:
    return LPLedgerRow(
        chain="base",
        pool_id="",
        block_number=timestamp_ms,
        block_time="2026-01-01T00:00:00+00:00",
        tx_hash=tx_hash,
        log_index=0,
        event_order=0,
        event_type=event_type,
        position_manager="0xpm",
        token_id=1,
        lp_owner="0xlp",
        owner_source="transfer",
        tick_lower=-100,
        tick_upper=100,
        liquidity_delta=liquidity_delta,
        liquidity_after=max(liquidity_delta, 0),
        amount0=amount0,
        amount1=amount1,
        amount0_actual=amount0,
        amount1_actual=amount1,
        amount0_attribution_source="fixture",
        amount1_attribution_source="fixture",
        amount_attribution_status="fixture_exact" if liquidity_delta > 0 else "not_applicable",
        amount0_raw=str(amount0),
        amount1_raw=str(amount1),
        collect_amount0=collect_amount0,
        collect_amount1=collect_amount1,
        sqrt_price_x96_at_event=79228162514264337593543950336,
        tick_at_event=0,
        cngn_usd_price_at_event=price,
        timestamp_ms=timestamp_ms,
    )


def receipt(tx_hash: str, native_fee_wei: int) -> dict[str, str]:
    return {
        "chain": "base",
        "tx_hash": tx_hash,
        "block_number": "1",
        "gas_used": "1",
        "effective_gas_price_wei": str(native_fee_wei),
        "native_fee_wei": str(native_fee_wei),
        "tx_from": "0xfrom",
        "tx_to": "0xto",
    }


def test_episode_features_join_open_close_receipts_and_compute_net_pnl() -> None:
    rows = [
        ledger_row(
            "mint",
            "0xaaa",
            100,
            1_000,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
        ),
        ledger_row(
            "burn_collect",
            "0xbbb",
            -100,
            3_000,
            collect_amount0=Decimal("16"),
            collect_amount1=Decimal("16"),
        ),
    ]

    features = build_lp_episode_features(
        rows,
        [receipt("0xaaa", 2_000_000_000_000_000), receipt("0xbbb", 3_000_000_000_000_000)],
        native_token_usd=Decimal("2000"),
    )

    assert len(features) == 1
    feature = features[0]
    assert feature.open_tx_hash == "0xaaa"
    assert feature.close_tx_hash == "0xbbb"
    assert feature.gas_tx_hashes == "0xaaa|0xbbb"
    assert feature.opening_capital == Decimal("20")
    assert feature.closing_capital == Decimal("32")
    assert feature.gross_pnl == Decimal("12")
    assert feature.gas_native_fee_wei == 5_000_000_000_000_000
    assert feature.gas_native_fee == Decimal("0.005")
    assert feature.gas_cost_usd == Decimal("10.000")
    assert feature.net_pnl_after_gas == Decimal("2.000")
    assert feature.net_return_on_capital == Decimal("0.100")
    assert feature.gas_attribution_status == "exact_open_close"
    assert feature.net_pnl_status == "net_usd_available"


def test_episode_features_without_native_price_leave_net_usd_blank() -> None:
    rows = [
        ledger_row("mint", "0xaaa", 100, 1_000, amount0=Decimal("10"), amount1=Decimal("10")),
        ledger_row(
            "burn_collect",
            "0xbbb",
            -100,
            3_000,
            collect_amount0=Decimal("16"),
            collect_amount1=Decimal("16"),
        ),
    ]

    feature = build_lp_episode_features(
        rows,
        [receipt("0xaaa", 2_000_000_000_000_000), receipt("0xbbb", 3_000_000_000_000_000)],
    )[0]

    assert feature.gas_native_fee_wei == 5_000_000_000_000_000
    assert feature.gas_native_fee == Decimal("0.005")
    assert feature.native_token_usd is None
    assert feature.gas_cost_usd is None
    assert feature.net_pnl_after_gas is None
    assert feature.net_return_on_capital is None
    assert feature.net_pnl_status == "native_price_missing"


def test_episode_features_fail_when_matched_receipt_is_missing() -> None:
    rows = [
        ledger_row("mint", "0xaaa", 100, 1_000, amount0=Decimal("10"), amount1=Decimal("10")),
        ledger_row(
            "burn_collect",
            "0xbbb",
            -100,
            3_000,
            collect_amount0=Decimal("16"),
            collect_amount1=Decimal("16"),
        ),
    ]

    with pytest.raises(ValueError, match="missing receipt for episode gas tx_hash=0xbbb"):
        build_lp_episode_features(rows, [receipt("0xaaa", 2_000_000_000_000_000)])


def test_episode_features_resolve_same_timestamp_closes_by_closed_liquidity() -> None:
    rows = [
        ledger_row("mint", "0xopen1", 100, 1_000, amount0=Decimal("10"), amount1=Decimal("10")),
        ledger_row("mint", "0xopen2", 50, 2_000, amount0=Decimal("5"), amount1=Decimal("5")),
        ledger_row(
            "burn_collect",
            "0xclose1",
            -100,
            3_000,
            collect_amount0=Decimal("11"),
            collect_amount1=Decimal("11"),
        ),
        ledger_row(
            "burn_collect",
            "0xclose2",
            -50,
            3_000,
            collect_amount0=Decimal("5.5"),
            collect_amount1=Decimal("5.5"),
        ),
    ]

    features = build_lp_episode_features(
        rows,
        [
            receipt("0xopen1", 1),
            receipt("0xopen2", 2),
            receipt("0xclose1", 3),
            receipt("0xclose2", 4),
        ],
    )

    assert [feature.close_tx_hash for feature in features] == ["0xclose1", "0xclose2"]
    assert [feature.gas_attribution_status for feature in features] == [
        "exact_open_close",
        "exact_open_close",
    ]
