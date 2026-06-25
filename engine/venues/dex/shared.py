"""Shared DEX math, read configs, and low-level contract helpers."""

from dataclasses import dataclass
from decimal import Decimal

from engine.math.v3 import (
    _Q96,
    _tick_to_sqrt_price_x96,
    price_to_tick,
    sqrt_price_x96_to_decimal,
    tick_to_price,
)


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
