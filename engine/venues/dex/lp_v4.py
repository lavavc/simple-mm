"""Uniswap V4 LP adapter — extends BaseV4DexAdapter with position management."""

from dataclasses import dataclass
import time as _time
from decimal import Decimal
from typing import Any, Optional

import structlog
from eth_abi import encode  # type: ignore[attr-defined]
from web3 import Web3
from web3.types import TxParams, Wei

from engine.api.schemas import LPPosition, Position, TxResult
from engine.config import DexParams
from .shared import (
    ERC20_ABI, PositionState, sqrt_price_x96_to_decimal,
    _Q96, _tick_to_sqrt_x96, tick_to_price, compute_required_ratio,
)
from .v4 import BaseV4DexAdapter, V4ExecutionConfig

logger = structlog.get_logger()


# V4 PositionManager action codes
_V4_LP_MINT_POSITION      = 0
_V4_LP_DECREASE_LIQUIDITY = 2
_V4_LP_BURN_POSITION      = 3
_V4_LP_SETTLE_PAIR        = 17  # 0x11
_V4_LP_TAKE_PAIR          = 18  # 0x12

POSITION_MANAGER_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getPositionInfo",
        "outputs": [
            {
                "components": [
                    {"name": "currency0", "type": "address"},
                    {"name": "currency1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "hooks", "type": "address"},
                ],
                "name": "poolKey",
                "type": "tuple",
            },
            {"name": "info", "type": "bytes32"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "unlockData", "type": "bytes"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "modifyLiquidities",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]


@dataclass(slots=True)
class LPBalanceSwapResult:
    direction: str
    token_in: str
    token_out: str
    amount_in_raw: int
    min_out_raw: int
    tx_result: TxResult


@dataclass(slots=True)
class LPPositionSnapshot:
    token_id: int | None
    token_ids: tuple[int, ...]
    position_count: int
    liquidity: int
    token0_amount: Decimal
    token1_amount: Decimal
    token0_symbol: str
    token1_symbol: str
    range_min: Decimal
    range_max: Decimal
    in_range: bool
    position_value_usd: Decimal
    our_share_pct: Decimal | None


def _sign_extend_24(v: int) -> int:
    """Sign-extend a 24-bit integer."""
    if v & 0x800000:
        return v - 0x1000000
    return v


def _decode_position_info(info_bytes32: bytes) -> tuple[int, int]:
    """Decode tickLower and tickUpper from PositionInfo bytes32.

    Layout: bits 8-31 = tickLower (24 bits), bits 32-55 = tickUpper (24 bits).
    """
    raw = int.from_bytes(info_bytes32, "big")
    tick_lower = _sign_extend_24((raw >> 8) & 0xFFFFFF)
    tick_upper = _sign_extend_24((raw >> 32) & 0xFFFFFF)
    return tick_lower, tick_upper


class V4LPAdapter(BaseV4DexAdapter):
    """Uniswap V4 execution adapter with LP position management.

    Extends BaseV4DexAdapter (swap-only) with V4-native LP operations:
    mint and remove via PositionManager.modifyLiquidities.
    """

    def __init__(
        self,
        config: V4ExecutionConfig,
        lp_private_key: str,
        trade_private_key: str,
        strategy_params: DexParams,
    ):
        super().__init__(config, lp_private_key, trade_private_key, strategy_params)

        self.position_manager_contract: Any | None
        if config.position_manager:
            self.position_manager_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(config.position_manager),
                abi=POSITION_MANAGER_ABI,
            )
        else:
            self.position_manager_contract = None

        self._lp_approvals_done: set[str] = set()

    # === Position queries ===

    def get_owned_positions(self) -> list[int]:
        """Get all position token IDs owned by LP wallet via ERC721 on PositionManager."""
        if not self.position_manager_contract:
            return []
        try:
            balance = self.position_manager_contract.functions.balanceOf(
                self.lp_account.address
            ).call()
            return [
                self.position_manager_contract.functions.tokenOfOwnerByIndex(
                    self.lp_account.address, i
                ).call()
                for i in range(balance)
            ]
        except Exception as e:
            logger.warning("get_owned_positions_failed", venue=self.name, error=str(e))
            return []

    def _get_live_pool_snapshot(self) -> tuple[Decimal, int, Decimal, Decimal | None] | None:
        """Return the latest live market state needed to evaluate LP positions."""
        try:
            pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
            slot0 = self.state_view.functions.getSlot0(pool_id_bytes).call()
            sqrt_price_x96 = Decimal(int(slot0[0]))
            current_tick = int(slot0[1])
            current_price = sqrt_price_x96_to_decimal(
                int(sqrt_price_x96),
                self.config.token0_decimals,
                self.config.token1_decimals,
            )
            if self.config.invert_price and current_price > 0:
                current_price = Decimal(1) / current_price

            pool_liquidity = Decimal(self.state_view.functions.getLiquidity(pool_id_bytes).call())
            return sqrt_price_x96, current_tick, current_price, pool_liquidity
        except Exception as e:
            logger.warning("get_v4_live_pool_snapshot_failed", venue=self.name, error=str(e))
            return None

    def _get_position_state_from_market(
        self,
        token_id: int,
        *,
        current_tick: int,
        current_price: Decimal,
    ) -> Optional[PositionState]:
        """Get position details using a shared live market snapshot."""
        if not self.position_manager_contract:
            return None
        try:
            result = self.position_manager_contract.functions.getPositionInfo(token_id).call()
            info_bytes32: bytes = result[1]
            tick_lower, tick_upper = _decode_position_info(info_bytes32)

            pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
            liquidity = self.state_view.functions.getPositionLiquidity(
                pool_id_bytes,
                self.lp_account.address,
                tick_lower,
                tick_upper,
                b"\x00" * 32,
            ).call()

            if liquidity == 0:
                return None

            return PositionState(
                token_id=token_id,
                liquidity=liquidity,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                tokens_owed_0=0,
                tokens_owed_1=0,
                price_lower=tick_to_price(tick_lower, self.config.token0_decimals, self.config.token1_decimals),
                price_upper=tick_to_price(tick_upper, self.config.token0_decimals, self.config.token1_decimals),
                current_price=current_price,
                in_range=tick_lower <= current_tick <= tick_upper,
            )
        except Exception as e:
            logger.warning("get_v4_position_state_failed", venue=self.name, token_id=token_id, error=str(e))
            return None

    def get_position_state(self, token_id: int) -> Optional[PositionState]:
        """Get position details via PositionManager.getPositionInfo + StateView."""
        market = self._get_live_pool_snapshot()
        if market is None:
            return None
        _, current_tick, current_price, _ = market
        return self._get_position_state_from_market(
            token_id,
            current_tick=current_tick,
            current_price=current_price,
        )

    def _empty_position_balances(self) -> dict[str, Decimal]:
        return {"cngn": Decimal(0), "usdt": Decimal(0), "usdc": Decimal(0)}

    def _add_symbol_balance(
        self,
        balances: dict[str, Decimal],
        symbol: str,
        amount: Decimal,
    ) -> None:
        key = symbol.lower()
        if key in balances:
            balances[key] += amount
        else:
            balances["usdt"] += amount

    # === Position override ===

    async def get_position(self) -> Position:
        """Get the currently deployed LP position(s) on this venue only."""
        snapshot = self.get_active_lp_position_snapshot()
        balances = self._empty_position_balances()
        lp_position = None
        position_value_usd = None

        if snapshot is not None:
            self._add_symbol_balance(balances, snapshot.token0_symbol, snapshot.token0_amount)
            self._add_symbol_balance(balances, snapshot.token1_symbol, snapshot.token1_amount)
            lp_position = LPPosition(
                token_id=str(snapshot.token_id) if snapshot.token_id is not None else None,
                token_ids=[str(token_id) for token_id in snapshot.token_ids],
                position_count=snapshot.position_count,
                liquidity=str(snapshot.liquidity),
                range_min=snapshot.range_min,
                range_max=snapshot.range_max,
                in_range=snapshot.in_range,
                our_share_pct=snapshot.our_share_pct,
            )
            position_value_usd = snapshot.position_value_usd

        return Position(
            venue=self.name,
            pair=f"{self.config.token0_symbol}/{self.config.token1_symbol}",
            timestamp=int(_time.time() * 1000),
            balances=balances,
            lp_position=lp_position,
            position_value_usd=position_value_usd,
            volume_24h_usd=None,
        )

    def _compute_lp_token_amounts(
        self,
        pos_state: PositionState,
        sqrt_price_x96: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Compute the exact token amounts held inside the LP NFT at the current price."""
        liquidity = pos_state.liquidity
        sqrt_lower = int(_tick_to_sqrt_x96(pos_state.tick_lower))
        sqrt_upper = int(_tick_to_sqrt_x96(pos_state.tick_upper))
        sqrt_price = int(sqrt_price_x96)

        t0_scale = Decimal(10 ** self.config.token0_decimals)
        t1_scale = Decimal(10 ** self.config.token1_decimals)

        if sqrt_price <= sqrt_lower:
            amount0 = Decimal(
                liquidity * _Q96 * (sqrt_upper - sqrt_lower) // (sqrt_lower * sqrt_upper)
            ) / t0_scale
            amount1 = Decimal(0)
        elif sqrt_price >= sqrt_upper:
            amount0 = Decimal(0)
            amount1 = Decimal(liquidity * (sqrt_upper - sqrt_lower) // _Q96) / t1_scale
        else:
            amount0 = Decimal(
                liquidity * _Q96 * (sqrt_upper - sqrt_price) // (sqrt_price * sqrt_upper)
            ) / t0_scale
            amount1 = Decimal(liquidity * (sqrt_price - sqrt_lower) // _Q96) / t1_scale

        return amount0, amount1

    def _build_lp_position_snapshot(
        self,
        pos_state: PositionState,
        *,
        sqrt_price_x96: Decimal,
        pool_liquidity: Decimal | None,
    ) -> LPPositionSnapshot:
        """Build a stable LP snapshot from one active NFT and live pool state."""
        amount0, amount1 = self._compute_lp_token_amounts(pos_state, sqrt_price_x96)

        if self.config.cngn_is_token0:
            cngn_price_usd = (sqrt_price_x96 / _Q96) ** 2
            position_value_usd = amount0 * cngn_price_usd + amount1
        else:
            dec_adj = Decimal(10 ** (self.config.token0_decimals - self.config.token1_decimals))
            cngn_price_usd = Decimal(1) / ((sqrt_price_x96 / _Q96) ** 2 * dec_adj)
            position_value_usd = amount0 + amount1 * cngn_price_usd

        our_share_pct: Decimal | None = None
        if pool_liquidity is not None and pool_liquidity > 0:
            our_share_pct = (
                Decimal(pos_state.liquidity) / pool_liquidity * Decimal(100)
                if pos_state.in_range
                else Decimal(0)
            )

        return LPPositionSnapshot(
            token_id=pos_state.token_id,
            token_ids=(pos_state.token_id,),
            position_count=1,
            liquidity=pos_state.liquidity,
            token0_amount=amount0,
            token1_amount=amount1,
            token0_symbol=self.config.token0_symbol,
            token1_symbol=self.config.token1_symbol,
            range_min=pos_state.price_lower,
            range_max=pos_state.price_upper,
            in_range=pos_state.in_range,
            position_value_usd=position_value_usd,
            our_share_pct=our_share_pct,
        )

    def _aggregate_lp_position_snapshots(
        self,
        snapshots: list[LPPositionSnapshot],
    ) -> LPPositionSnapshot | None:
        """Combine one or more LP NFT snapshots into a single venue-local position view."""
        if not snapshots:
            return None

        token_ids = tuple(snapshot.token_ids[0] for snapshot in snapshots)
        multi_position = len(token_ids) > 1
        if multi_position:
            logger.warning("multiple_lp_positions_detected", venue=self.name, token_ids=list(token_ids))

        have_share = all(snapshot.our_share_pct is not None for snapshot in snapshots)
        return LPPositionSnapshot(
            token_id=token_ids[0] if len(token_ids) == 1 else None,
            token_ids=token_ids,
            position_count=len(token_ids),
            liquidity=sum(snapshot.liquidity for snapshot in snapshots),
            token0_amount=sum(snapshot.token0_amount for snapshot in snapshots),
            token1_amount=sum(snapshot.token1_amount for snapshot in snapshots),
            token0_symbol=self.config.token0_symbol,
            token1_symbol=self.config.token1_symbol,
            range_min=min(snapshot.range_min for snapshot in snapshots),
            range_max=max(snapshot.range_max for snapshot in snapshots),
            in_range=any(snapshot.in_range for snapshot in snapshots),
            position_value_usd=sum(snapshot.position_value_usd for snapshot in snapshots),
            our_share_pct=(
                sum(snapshot.our_share_pct or Decimal(0) for snapshot in snapshots)
                if have_share
                else None
            ),
        )

    def get_active_lp_position_snapshot(self) -> LPPositionSnapshot | None:
        """Return the aggregated deployed LP composition from live pool state."""
        token_ids = self.get_owned_positions()
        if not token_ids:
            return None

        market = self._get_live_pool_snapshot()
        if market is None:
            return None
        sqrt_price_x96, current_tick, current_price, pool_liquidity = market

        snapshots: list[LPPositionSnapshot] = []
        for token_id in token_ids:
            pos_state = self._get_position_state_from_market(
                token_id,
                current_tick=current_tick,
                current_price=current_price,
            )
            if pos_state is None:
                continue
            snapshots.append(
                self._build_lp_position_snapshot(
                    pos_state,
                    sqrt_price_x96=sqrt_price_x96,
                    pool_liquidity=pool_liquidity,
                )
            )

        return self._aggregate_lp_position_snapshots(snapshots)

    # === LP operations ===

    async def mint_position(
        self,
        amount0: int,
        amount1: int,
        tick_lower: int,
        tick_upper: int,
    ) -> TxResult:
        """Create new V4 LP position via PositionManager.modifyLiquidities."""
        if not self.position_manager_contract:
            return TxResult(hash="", status="failed", error="no position_manager configured")

        await self._approve_lp_tokens_if_needed()

        pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
        slot0 = self.state_view.functions.getSlot0(pool_id_bytes).call()
        sqrt_price_x96 = int(slot0[0])

        liquidity = self._compute_liquidity_from_amounts(
            sqrt_price_x96, tick_lower, tick_upper, amount0, amount1
        )
        if liquidity == 0:
            return TxResult(hash="", status="failed", error="computed zero liquidity")

        slippage = float(self.params.max_slippage_percent) / 100
        amount0_max = int(amount0 * (1 + slippage))
        amount1_max = int(amount1 * (1 + slippage))

        currency0, currency1, fee, tick_spacing, hooks = self._resolve_pool_key()
        pool_key = (currency0, currency1, fee, tick_spacing, hooks)

        actions = bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR])
        params = [
            encode(
                ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
                [pool_key, tick_lower, tick_upper, liquidity, amount0_max, amount1_max, self.lp_account.address, b""],
            ),
            encode(["address", "address"], [currency0, currency1]),
        ]
        unlock_data = encode(["bytes", "bytes[]"], [actions, params])

        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300
        tx_params = self._get_tx_params(self.lp_account)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = 2_000_000  # placeholder; replaced by estimate below
        tx = self.position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)
        estimate_params: TxParams = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": Wei(0),
        }
        estimated = self.w3.eth.estimate_gas(estimate_params)
        tx["gas"] = int(estimated * 1.2)

        logger.info(
            "v4_mint_position",
            venue=self.name,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
            amount0=amount0,
            amount1=amount1,
        )
        return await self._send_transaction(tx, self.lp_account)

    async def remove_position(self, token_id: int, recipient: str | None = None) -> TxResult:
        """Remove V4 LP position via modifyLiquidities (decrease + burn + take).

        recipient: address to send withdrawn tokens to. Defaults to the LP account.
                   Rebalance path passes no recipient (reminting immediately);
                   manual withdraw path passes the caller's destination address.
        """
        if not self.position_manager_contract:
            return TxResult(hash="", status="failed", error="no position_manager configured")

        pos = self.get_position_state(token_id)
        if not pos:
            return TxResult(hash="", status="failed", error="position not found")

        currency0, currency1, _, _, _ = self._resolve_pool_key()
        to_addr = Web3.to_checksum_address(recipient or self.lp_account.address)

        actions = bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_BURN_POSITION, _V4_LP_TAKE_PAIR])
        params = [
            encode(
                ["uint256", "uint256", "uint128", "uint128", "bytes"],
                [token_id, pos.liquidity, 0, 0, b""],
            ),
            encode(
                ["uint256", "uint128", "uint128", "bytes"],
                [token_id, 0, 0, b""],
            ),
            encode(
                ["address", "address", "address"],
                [currency0, currency1, to_addr],
            ),
        ]
        unlock_data = encode(["bytes", "bytes[]"], [actions, params])

        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300
        tx_params = self._get_tx_params(self.lp_account)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = 2_000_000  # placeholder; replaced by estimate below
        tx = self.position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)
        estimate_params: TxParams = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": Wei(0),
        }
        estimated = self.w3.eth.estimate_gas(estimate_params)
        tx["gas"] = int(estimated * 1.2)

        logger.info("v4_remove_position", venue=self.name, token_id=token_id, liquidity=pos.liquidity, recipient=to_addr)
        return await self._send_transaction(tx, self.lp_account)

    # === Pool metrics (used by get_position) ===

    def get_pool_metrics(
        self, pos_state: Optional[PositionState] = None
    ) -> tuple[Optional[Decimal], None, Optional[Decimal]]:
        """Compute position value and our share of active pool liquidity.

        Position value uses exact tick math: given the position's tick range and the
        current sqrtPriceX96 from the pool state cache, computes the precise token
        amounts held in the position and converts to USD. No external calls.

        Our share is our position liquidity divided by the pool's total active
        in-range liquidity (from the same cache), which determines our proportion
        of swap fees earned on every trade through the pool.

        Volume is not available on-chain without event indexing; always returns None.
        """
        if pos_state is None:
            return None, None, None

        from engine.market.pool_state import get_cached_pool_state

        sqrt_p, pool_liquidity, _, _ = get_cached_pool_state(self.config.pool_id)
        if sqrt_p is None or pool_liquidity is None or pool_liquidity <= 0:
            return None, None, None

        snapshot = self._build_lp_position_snapshot(
            pos_state,
            sqrt_price_x96=sqrt_p,
            pool_liquidity=pool_liquidity,
        )

        return snapshot.position_value_usd, None, snapshot.our_share_pct

    # === Capital allocation ===

    def calculate_mint_amounts(self) -> tuple[int, int]:
        """Return full LP wallet balances as raw token amounts to deploy."""
        balance0 = self.token0.functions.balanceOf(self.lp_account.address).call()
        balance1 = self.token1.functions.balanceOf(self.lp_account.address).call()
        logger.info(
            "calculated_mint_amounts",
            venue=self.name,
            balance0=balance0 / 10 ** self.config.token0_decimals,
            balance1=balance1 / 10 ** self.config.token1_decimals,
        )
        return balance0, balance1

    async def _ensure_lp_swap_approvals(self, token_in: str) -> None:
        """Ensure LP account has Permit2 + UniversalRouter approvals for a preparatory swap."""
        await self._approve_token_to_permit2_if_needed(token_in, account=self.lp_account)
        await self._approve_permit2_to_router_if_needed(token_in, account=self.lp_account)

    async def _swap_from_lp(self, token_in: str, amount_in: int, min_out: int) -> TxResult:
        """Swap from the LP account using the same V4 pool (preparatory ratio correction)."""
        await self._ensure_lp_swap_approvals(token_in)
        tx, _ = self._build_swap_tx(token_in, amount_in, min_out, account=self.lp_account)
        estimate_params: TxParams = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": Wei(0),
        }
        estimated = self.w3.eth.estimate_gas(estimate_params)
        tx["gas"] = int(estimated * 1.2)
        token_out = (
            self.config.token1_address
            if token_in.lower() == self.config.token0_address.lower()
            else self.config.token0_address
        )
        return await self._send_transaction(tx, self.lp_account, output_token=token_out)


    async def prepare_lp_balance(self, tick_lower: int, tick_upper: int) -> LPBalanceSwapResult | None:
        """Swap LP wallet tokens to the ratio required by the pool at the current price.

        Reads the current sqrtPriceX96 from pool Slot0, computes the target token0/token1
        split using pure tick math, and swaps the surplus token if the imbalance exceeds 1%
        of total portfolio value. downside_skew is NOT used here — ratio is pool-state only.
        """
        pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
        slot0 = self.state_view.functions.getSlot0(pool_id_bytes).call()
        sqrt_price_x96 = int(slot0[0])

        balance0_raw = self.token0.functions.balanceOf(self.lp_account.address).call()
        balance1_raw = self.token1.functions.balanceOf(self.lp_account.address).call()
        balance0 = Decimal(balance0_raw) / Decimal(10 ** self.config.token0_decimals)
        balance1 = Decimal(balance1_raw) / Decimal(10 ** self.config.token1_decimals)

        if balance0 == 0 and balance1 == 0:
            return None

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_price_x96, self.config.token0_decimals, self.config.token1_decimals)

        # Price of token0 in token1 units (for value normalisation)
        price = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2 * Decimal(
            10 ** self.config.token0_decimals
        ) / Decimal(10 ** self.config.token1_decimals)

        # Total value in token1 units
        total_value = balance0 * price + balance1
        if total_value == 0:
            return None

        # Target allocations
        denom = r0 * price + r1 if (r0 * price + r1) > 0 else Decimal(1)
        target0 = total_value * r0 / denom
        target1 = max(Decimal(0), total_value - target0 * price)

        imbalance = abs(balance0 - target0) * price
        threshold = total_value * Decimal("0.01")

        if imbalance <= threshold:
            logger.info("lp_balance_already_correct", venue=self.name,
                        balance0=float(balance0), balance1=float(balance1),
                        target0=float(target0), target1=float(target1))
            return None

        if balance0 > target0:
            surplus = balance0 - target0
            surplus_raw = int(surplus * Decimal(10 ** self.config.token0_decimals))
            min_out = int(surplus * price * Decimal("0.99") * Decimal(10 ** self.config.token1_decimals))
            token_in = self.config.token0_address
            token_out = self.config.token1_address
            direction = "token0_to_token1"
            logger.info("lp_swap_to_ratio", venue=self.name, direction="token0→token1",
                        surplus=float(surplus), min_out=min_out)
            result = await self._swap_from_lp(token_in, surplus_raw, min_out)
        else:
            surplus = balance1 - target1
            surplus_raw = int(surplus * Decimal(10 ** self.config.token1_decimals))
            min_out_dec = surplus / price * Decimal("0.99")
            min_out = int(min_out_dec * Decimal(10 ** self.config.token0_decimals))
            token_in = self.config.token1_address
            token_out = self.config.token0_address
            direction = "token1_to_token0"
            logger.info("lp_swap_to_ratio", venue=self.name, direction="token1→token0",
                        surplus=float(surplus), min_out=min_out)
            result = await self._swap_from_lp(token_in, surplus_raw, min_out)

        if result.status != "confirmed":
            logger.warning("lp_ratio_swap_failed_skipping_mint", venue=self.name, error=result.error)
        return LPBalanceSwapResult(
            direction=direction,
            token_in=token_in,
            token_out=token_out,
            amount_in_raw=surplus_raw,
            min_out_raw=min_out,
            tx_result=result,
        )

    # === Helpers ===

    def _compute_liquidity_from_amounts(
        self,
        sqrt_price_x96: int,
        tick_lower: int,
        tick_upper: int,
        amount0: int,
        amount1: int,
    ) -> int:
        """Convert token amounts + tick range to V4 liquidity units."""
        sqrt_a = int(_tick_to_sqrt_x96(tick_lower))
        sqrt_b = int(_tick_to_sqrt_x96(tick_upper))
        sqrt_p = sqrt_price_x96

        if sqrt_p <= sqrt_a:
            if sqrt_b == sqrt_a:
                return 0
            return amount0 * sqrt_a * sqrt_b // ((sqrt_b - sqrt_a) * _Q96)
        elif sqrt_p >= sqrt_b:
            if sqrt_b == sqrt_a:
                return 0
            return amount1 * _Q96 // (sqrt_b - sqrt_a)
        else:
            L0 = amount0 * sqrt_p * sqrt_b // ((sqrt_b - sqrt_p) * _Q96) if sqrt_b > sqrt_p else 0
            L1 = amount1 * _Q96 // (sqrt_p - sqrt_a) if sqrt_p > sqrt_a else 0
            if L0 > 0 and L1 > 0:
                return min(L0, L1)
            return max(L0, L1)

    async def _approve_lp_tokens_if_needed(self) -> None:
        """Approve ERC20 tokens to PositionManager from LP account."""
        if not self.position_manager_contract:
            return
        pm_addr = self.config.position_manager
        for token_addr in [self.config.token0_address, self.config.token1_address]:
            cache_key = f"lp_pm_{token_addr.lower()}"
            if cache_key in self._lp_approvals_done:
                continue
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
            )
            allowance = token.functions.allowance(
                self.lp_account.address,
                Web3.to_checksum_address(pm_addr),
            ).call()
            if allowance >= 2 ** 128:
                self._lp_approvals_done.add(cache_key)
                continue
            tx_params = self._get_tx_params(self.lp_account)
            tx_params["value"] = Wei(0)
            tx_params["gas"] = 100_000
            tx = token.functions.approve(
                Web3.to_checksum_address(pm_addr), 2 ** 256 - 1
            ).build_transaction(tx_params)
            result = await self._send_transaction(tx, self.lp_account)
            if result.status == "confirmed":
                self._lp_approvals_done.add(cache_key)
            else:
                logger.error("lp_token_approval_failed", venue=self.name, token=token_addr, error=result.error)
