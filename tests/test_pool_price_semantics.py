from decimal import Decimal

from backtester.pool_price_semantics import classify_pool_price_row


BASE_HEADER = {
    "block_time": "2026-01-01T00:00:00+00:00",
    "chain": "base",
    "pool_id": "0xpool",
    "event_type": "swap",
    "tx_hash": "0x1",
    "log_index": "1",
    "block_number": "10",
    "sqrt_price_x96": str(2**96),
    "tick": "0",
    "active_liquidity": "100",
    "fee_rate": "0.0015",
    "token0_symbol": "cNGN",
    "token1_symbol": "USDC",
}


def _row(**overrides: str) -> dict[str, str]:
    row = {
        **BASE_HEADER,
        "amount0": "-1000",
        "amount1": "998.5",
        "amount_usd": "998.5",
        "cngn_usd_price": "1",
    }
    row.update(overrides)
    return row


def test_classifies_sqrt_mid_price_as_canonical_pool_state() -> None:
    semantics = classify_pool_price_row(_row(cngn_usd_price="1"))

    assert semantics.raw_sqrt_mid == Decimal("1")
    assert semantics.stored_cngn_usd_price == Decimal("1")
    assert semantics.amount_ratio_price == Decimal("0.9985")
    assert semantics.stored_price_model == "sqrt_mid"


def test_classifies_legacy_amount_ratio_without_changing_raw_sqrt_mid() -> None:
    semantics = classify_pool_price_row(_row(cngn_usd_price="0.9985"))

    assert semantics.raw_sqrt_mid == Decimal("1")
    assert semantics.stored_cngn_usd_price == Decimal("0.9985")
    assert semantics.amount_ratio_price == Decimal("0.9985")
    assert semantics.stored_price_model == "swap_amount_ratio"


def test_classifies_price_matching_neither_sqrt_nor_amount_ratio_as_unexplained() -> None:
    semantics = classify_pool_price_row(_row(cngn_usd_price="2"))

    assert semantics.raw_sqrt_mid == Decimal("1")
    assert semantics.amount_ratio_price == Decimal("0.9985")
    assert semantics.stored_price_model == "unexplained"
