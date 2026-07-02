import pytest

from research.backtester.pool_features import derive_swap_flow


def test_base_cngn_token0_negative_amount_is_buy_pressure():
    flow = derive_swap_flow(
        {
            "event_type": "swap",
            "chain": "base",
            "token0_symbol": "cNGN",
            "token1_symbol": "USDC",
            "amount0": "-1000",
            "amount1": "0.72",
            "amount_usd": "0.72",
        }
    )

    assert flow.cngn_flow_direction == 1
    assert str(flow.signed_cngn_amount) == "1000"
    assert str(flow.signed_usd_notional) == "0.72"


def test_bsc_cngn_token1_negative_amount_is_buy_pressure():
    flow = derive_swap_flow(
        {
            "event_type": "swap",
            "chain": "bsc",
            "token0_symbol": "USDT",
            "token1_symbol": "cNGN",
            "amount0": "0.72",
            "amount1": "-1000",
            "amount_usd": "0.72",
        }
    )

    assert flow.cngn_flow_direction == 1
    assert str(flow.signed_cngn_amount) == "1000"
    assert str(flow.signed_usd_notional) == "0.72"


def test_cngn_positive_amount_is_sell_pressure():
    flow = derive_swap_flow(
        {
            "event_type": "swap",
            "chain": "base",
            "token0_symbol": "cNGN",
            "token1_symbol": "USDC",
            "amount0": "1000",
            "amount1": "-0.72",
            "amount_usd": "0.72",
        }
    )

    assert flow.cngn_flow_direction == -1
    assert str(flow.signed_cngn_amount) == "-1000"
    assert str(flow.signed_usd_notional) == "-0.72"


def test_non_swap_row_returns_zero_flow():
    flow = derive_swap_flow(
        {
            "event_type": "mint",
            "chain": "base",
            "token0_symbol": "USDC",
            "token1_symbol": "USDT",
            "amount0": "1",
            "amount1": "1",
            "amount_usd": "2",
        }
    )

    assert flow.cngn_flow_direction == 0
    assert str(flow.signed_cngn_amount) == "0"
    assert str(flow.signed_usd_notional) == "0"


def test_swap_without_cngn_raises_value_error():
    with pytest.raises(ValueError, match="cNGN"):
        derive_swap_flow(
            {
                "event_type": "swap",
                "chain": "base",
                "token0_symbol": "USDC",
                "token1_symbol": "USDT",
                "amount0": "-1",
                "amount1": "1",
                "amount_usd": "1",
            }
        )
