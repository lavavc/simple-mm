"""Shared DEX math, read configs, and low-level contract helpers."""

from dataclasses import dataclass
from decimal import Decimal


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
    avg_block_seconds: float = 2.0  # used to estimate 24h block range without binary search


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
