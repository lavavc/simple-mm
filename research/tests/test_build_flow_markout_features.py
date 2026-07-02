from decimal import Decimal

from research.scripts.build_flow_markout_features import build_flow_markout_rows


def test_pre_trade_flow_excludes_current_swap_and_markout_looks_forward() -> None:
    rows = build_flow_markout_rows(
        history_rows=[
            _history_row("a", 1, "-1000", "10"),
            _history_row("b", 2, "500", "5"),
            _history_row("c", 3, "-2000", "20"),
            _history_row("d", 4, "-1000", "10"),
        ],
        feature_rows=[
            _feature_row("a", 1, 1_700_000_000_000, "100"),
            _feature_row("b", 2, 1_700_000_001_000, "110"),
            _feature_row("c", 3, 1_700_000_002_000, "121"),
            _feature_row("d", 4, 1_700_000_003_000, "133.1"),
        ],
        taus=(2,),
        horizons=(1,),
        ew_lambda=Decimal("0.50"),
    )

    assert [row["F_2_signed_usd"] for row in rows] == [
        "0.000000",
        "10.000000",
        "5.000000",
        "15.000000",
    ]
    assert [row["F_2_signed_cngn"] for row in rows] == [
        "0.000000",
        "1000.000000",
        "500.000000",
        "1500.000000",
    ]
    assert rows[0]["markout_1_raw_sqrt_mid_return"] == "0.100000"
    assert rows[1]["markout_1_raw_sqrt_mid_return"] == "0.100000"
    assert rows[3]["markout_1_raw_sqrt_mid_return"] == ""


def test_adaptive_beta_uses_only_matured_prior_markouts() -> None:
    rows = build_flow_markout_rows(
        history_rows=[
            _history_row("a", 1, "-1000", "10"),
            _history_row("b", 2, "-1000", "10"),
            _history_row("c", 3, "1000", "10"),
        ],
        feature_rows=[
            _feature_row("a", 1, 1_700_000_000_000, "100"),
            _feature_row("b", 2, 1_700_000_001_000, "110"),
            _feature_row("c", 3, 1_700_000_002_000, "99"),
        ],
        taus=(1,),
        horizons=(1,),
        ew_lambda=Decimal("0.50"),
    )

    assert rows[0]["beta_1_1_ew"] == ""
    assert rows[1]["beta_1_1_ew"] == ""
    assert rows[2]["beta_1_1_ew"] == "-0.010000"
    assert rows[2]["predicted_markout_1_1"] == "-0.100000"


def _history_row(tx_hash: str, log_index: int, amount0: str, amount_usd: str) -> dict[str, str]:
    return {
        "event_type": "swap",
        "tx_hash": tx_hash,
        "log_index": str(log_index),
        "block_number": str(100 + log_index),
        "token0_symbol": "cNGN",
        "token1_symbol": "USDC",
        "amount0": amount0,
        "amount1": "0",
        "amount_usd": amount_usd,
    }


def _feature_row(
    tx_hash: str,
    log_index: int,
    timestamp_ms: int,
    raw_sqrt_mid: str,
) -> dict[str, str]:
    return {
        "tx_hash": tx_hash,
        "log_index": str(log_index),
        "block_number": str(100 + log_index),
        "timestamp_ms": str(timestamp_ms),
        "raw_sqrt_mid": raw_sqrt_mid,
    }
