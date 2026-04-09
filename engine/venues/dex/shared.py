"""Shared DEX math, read configs, and low-level contract helpers."""

import math
from dataclasses import dataclass
from decimal import Decimal

# Uniswap V4 fixed-point constant — used across tick math and liquidity calculations
_Q96 = 2 ** 96


_MAX_TICK = 887272


def _tick_to_sqrt_price_x96(tick: int) -> int:
    """Exact integer TickMath matching Uniswap V4 TickMath.getSqrtPriceAtTick().

    Returns sqrtPriceX96 as a Q64.96 integer — bit-for-bit identical to the
    on-chain contract. Eliminates the catastrophic cancellation that occurs when
    subtracting nearly-equal float64 values for large tick magnitudes.
    """
    abs_tick = abs(tick)
    if abs_tick > _MAX_TICK:
        raise ValueError(f"tick {tick} out of range")

    ratio = 0x100000000000000000000000000000000 if (abs_tick & 0x1) == 0 else 0xfffcb933bd6fad37aa2d162d1a594001
    if abs_tick & 0x2:     ratio = ratio * 0xfff97272373d413259a46990580e213a >> 128
    if abs_tick & 0x4:     ratio = ratio * 0xfff2e50f5f656932ef12357cf3c7fdcc >> 128
    if abs_tick & 0x8:     ratio = ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0 >> 128
    if abs_tick & 0x10:    ratio = ratio * 0xffcb9843d60f6159c9db58835c926644 >> 128
    if abs_tick & 0x20:    ratio = ratio * 0xff973b41fa98c081472e6896dfb254c0 >> 128
    if abs_tick & 0x40:    ratio = ratio * 0xff2ea16466c96a3843ec78b326b52861 >> 128
    if abs_tick & 0x80:    ratio = ratio * 0xfe5dee046a99a2a811c461f1969c3053 >> 128
    if abs_tick & 0x100:   ratio = ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4 >> 128
    if abs_tick & 0x200:   ratio = ratio * 0xf987a7253ac413176f2b074cf7815e54 >> 128
    if abs_tick & 0x400:   ratio = ratio * 0xf3392b0822b70005940c7a398e4b70f3 >> 128
    if abs_tick & 0x800:   ratio = ratio * 0xe7159475a2c29b7443b29c7fa6e889d9 >> 128
    if abs_tick & 0x1000:  ratio = ratio * 0xd097f3bdfd2022b8845ad8f792aa5825 >> 128
    if abs_tick & 0x2000:  ratio = ratio * 0xa9f746462d870fdf8a65dc1f90e061e5 >> 128
    if abs_tick & 0x4000:  ratio = ratio * 0x70d869a156d2a1b890bb3df62baf32f7 >> 128
    if abs_tick & 0x8000:  ratio = ratio * 0x31be135f97d08fd981231505542fcfa6 >> 128
    if abs_tick & 0x10000: ratio = ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9 >> 128
    if abs_tick & 0x20000: ratio = ratio * 0x5d6af8dedb81196699c329225ee604 >> 128
    if abs_tick & 0x40000: ratio = ratio * 0x2216e584f5fa1ea926041bedfe98 >> 128
    if abs_tick & 0x80000: ratio = ratio * 0x48a170391f7dc42444e8fa2 >> 128

    if tick > 0:
        ratio = (2**256 - 1) // ratio

    remainder = ratio & ((1 << 32) - 1)
    return (ratio >> 32) + (1 if remainder else 0)


def tick_to_price(tick: int, token0_decimals: int, token1_decimals: int) -> Decimal:
    """Convert a tick index to a human-readable price."""
    decimal_diff = token0_decimals - token1_decimals
    return Decimal("1.0001") ** tick * Decimal(10 ** decimal_diff)


def price_to_tick(price: Decimal, token0_decimals: int, token1_decimals: int) -> int:
    """Convert a human-readable price to a tick index."""
    decimal_diff = token0_decimals - token1_decimals
    adjusted = float(price) / (10 ** decimal_diff)
    return int(math.log(adjusted) / math.log(1.0001))


def compute_required_ratio(
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> tuple[Decimal, Decimal]:
    """Return (r0, r1) — token amounts per unit of liquidity at the current price."""
    sqrt_a = float(_tick_to_sqrt_price_x96(tick_lower))
    sqrt_b = float(_tick_to_sqrt_price_x96(tick_upper))
    sqrt_p = float(sqrt_price_x96)

    if sqrt_p <= sqrt_a:
        r0 = (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b) * _Q96 if sqrt_a * sqrt_b > 0 else 0.0
        r1 = 0.0
    elif sqrt_p >= sqrt_b:
        r0 = 0.0
        r1 = (sqrt_b - sqrt_a) / _Q96
    else:
        r0 = (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b) * _Q96
        r1 = (sqrt_p - sqrt_a) / _Q96

    dec_adj = Decimal(10 ** token0_decimals) / Decimal(10 ** token1_decimals)
    return Decimal(str(r0)) / dec_adj, Decimal(str(r1))


@dataclass
class PositionState:
    """LP position state from on-chain."""

    token_id: int
    liquidity: int
    tick_lower: int
    tick_upper: int
    tokens_owed_0: int
    tokens_owed_1: int
    price_lower: Decimal
    price_upper: Decimal
    current_price: Decimal
    in_range: bool


@dataclass
class V4PoolReadConfig:
    """Minimal config for read-only V4 pool price fetching via StateView."""

    pool_manager: str
    state_view: str
    pool_address: str
    rpc_url: str
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    invert_price: bool = False
    chain_id_str: str = ""


def sqrt_price_x96_to_decimal(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """Convert a concentrated-liquidity sqrtPriceX96 to a human-readable price."""
    price = (Decimal(sqrt_price_x96) / Decimal(2**96)) ** 2
    decimal_diff = token0_decimals - token1_decimals
    price *= Decimal(10**decimal_diff)
    return price


MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI = [
    {
        "inputs": [{"components": [
            {"internalType": "address", "name": "target", "type": "address"},
            {"internalType": "bool", "name": "allowFailure", "type": "bool"},
            {"internalType": "bytes", "name": "callData", "type": "bytes"},
        ], "name": "calls", "type": "tuple[]"}],
        "name": "aggregate3",
        "outputs": [{"components": [
            {"internalType": "bool", "name": "success", "type": "bool"},
            {"internalType": "bytes", "name": "returnData", "type": "bytes"},
        ], "name": "returnData", "type": "tuple[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]


ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


_BALANCE_OF_SIG = bytes.fromhex("70a08231")


def _encode_balance_of(address: str) -> bytes:
    return _BALANCE_OF_SIG + bytes(12) + bytes.fromhex(address[2:])


def _decode_uint256(data: bytes) -> int:
    return int.from_bytes(data, "big") if len(data) == 32 else 0
