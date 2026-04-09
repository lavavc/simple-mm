"""LP position dataclasses and V4 action code constants."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from engine.types import TxResult

# V4 PositionManager action codes from Uniswap v4-periphery Actions.sol
_V4_LP_INCREASE_LIQUIDITY = 0   # 0x00
_V4_LP_DECREASE_LIQUIDITY = 1   # 0x01
_V4_LP_MINT_POSITION      = 2   # 0x02
_V4_LP_BURN_POSITION      = 3   # 0x03
_V4_LP_SETTLE_PAIR        = 13  # 0x0d
_V4_LP_TAKE_PAIR          = 17  # 0x11


@dataclass(slots=True)
class LPBalanceSwapResult:
    direction: str
    token_in: str
    token_out: str
    amount_in_raw: int
    min_out_raw: int
    tx_result: "TxResult"


@dataclass(slots=True)
class LPPositionSnapshot:
    token_id: int | None
    liquidity: int | None
    token0_amount: Decimal | None
    token1_amount: Decimal | None
    token0_symbol: str
    token1_symbol: str
    range_min: Decimal | None
    range_max: Decimal | None
    in_range: bool | None
    position_value_usd: Decimal | None
    our_share_pct: Decimal | None
    snapshot_status: Literal["live", "degraded"] = "live"
    snapshot_message: str | None = None


@dataclass(slots=True)
class LPStaticPositionMetadata:
    token_id: int
    liquidity: int
    tick_lower: int
    tick_upper: int
    range_min: Decimal
    range_max: Decimal


@dataclass(slots=True)
class LPMarketSnapshot:
    sqrt_price_x96: Decimal
    current_price: Decimal
    pool_liquidity: Decimal | None
    current_tick: int
