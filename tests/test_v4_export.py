from decimal import Decimal

from eth_abi import encode  # type: ignore[attr-defined]

from backtester.v4_export import (
    POOL_CONFIGS,
    _amounts_from_liquidity,
    _decode_burn_param,
    _decode_increase_or_decrease_param,
    _decode_mint_param,
    decode_modify_liquidities_payload,
    decode_swap_row,
    derive_cngn_price,
)
from engine.lp.types import _V4_LP_BURN_POSITION, _V4_LP_INCREASE_LIQUIDITY, _V4_LP_MINT_POSITION
from web3 import Web3


def _build_modify_input(actions: bytes, params: list[bytes], deadline: int = 123) -> str:
    selector = Web3.keccak(text="modifyLiquidities(bytes,uint256)")[:4]
    unlock_data = encode(["bytes", "bytes[]"], [actions, params])
    calldata = selector + encode(["bytes", "uint256"], [unlock_data, deadline])
    return "0x" + calldata.hex()


class TestV4Export:
    def test_derive_cngn_price_base(self):
        price = derive_cngn_price(1_500_000_000, -1_000_000, POOL_CONFIGS["uni-base"])
        assert price == Decimal("0.0006666666666666666666666666667")

    def test_decode_swap_row_base(self):
        amount0 = int(-1_500 * 10**6).to_bytes(32, "big", signed=True)
        amount1 = int(1 * 10**6).to_bytes(32, "big", signed=True)
        sqrt_p = (2**96).to_bytes(32, "big")
        liquidity = (1_000_000).to_bytes(32, "big")
        tick = (10).to_bytes(32, "big", signed=True)
        fee = (1500).to_bytes(32, "big")
        log = {
            "data": "0x" + (amount0 + amount1 + sqrt_p + liquidity + tick + fee).hex(),
            "transactionHash": "0x" + "11" * 32,
            "logIndex": 7,
            "blockNumber": 123,
        }
        row = decode_swap_row(log, 1_700_000_000, POOL_CONFIGS["uni-base"])
        assert row.event_type == "swap"
        assert row.active_liquidity == 1_000_000
        assert row.amount_usd == 1.0
        assert row.cngn_usd_price == 1 / 1500

    def test_decode_modify_liquidities_payload(self):
        mint_param = encode(
            ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
            [
                (
                    POOL_CONFIGS["uni-base"].token0_address,
                    POOL_CONFIGS["uni-base"].token1_address,
                    1500,
                    30,
                    "0x0000000000000000000000000000000000000000",
                ),
                -120,
                120,
                999,
                1_000_000,
                2_000_000,
                "0x0000000000000000000000000000000000000001",
                b"",
            ],
        )
        inc_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 111, 0, 0, b""])
        burn_param = encode(["uint256", "uint128", "uint128", "bytes"], [55, 0, 0, b""])
        input_data = _build_modify_input(
            bytes([_V4_LP_MINT_POSITION, _V4_LP_INCREASE_LIQUIDITY, _V4_LP_BURN_POSITION]),
            [mint_param, inc_param, burn_param],
        )
        actions, params = decode_modify_liquidities_payload(input_data)
        assert list(actions) == [_V4_LP_MINT_POSITION, _V4_LP_INCREASE_LIQUIDITY, _V4_LP_BURN_POSITION]
        pool_key, tick_lower, tick_upper, liquidity, amount0_max, amount1_max = _decode_mint_param(params[0])
        assert int(pool_key[2]) == 1500
        assert tick_lower == -120
        assert tick_upper == 120
        assert liquidity == 999
        assert amount0_max == 1_000_000
        assert amount1_max == 2_000_000
        token_id, liquidity_delta = _decode_increase_or_decrease_param(params[1])
        assert token_id == 55
        assert liquidity_delta == 111
        assert _decode_burn_param(params[2]) == 55

    def test_amounts_from_liquidity_returns_positive_amounts(self):
        amount0, amount1 = _amounts_from_liquidity(
            liquidity=1_000_000,
            tick_lower=-120,
            tick_upper=120,
            sqrt_price_x96=2**96,
            config=POOL_CONFIGS["uni-base"],
        )
        assert amount0 > 0
        assert amount1 > 0
