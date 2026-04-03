"""Uniswap V4 LP adapter — extends BaseV4DexAdapter with position management."""

import time as _time
from decimal import Decimal
from typing import Optional

import structlog
from eth_abi import encode
from web3 import Web3

from engine.api.schemas import LPPosition, Position, TxResult
from engine.config import DexParams
from .shared import (
    ERC20_ABI, MULTICALL3_ABI, MULTICALL3_ADDRESS, PositionState,
    _decode_uint256, _encode_balance_of, sqrt_price_x96_to_decimal,
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

    def get_position_state(self, token_id: int) -> Optional[PositionState]:
        """Get position details via PositionManager.getPositionInfo + StateView."""
        if not self.position_manager_contract:
            return None
        try:
            result = self.position_manager_contract.functions.getPositionInfo(token_id).call()
            # result = (poolKey tuple, info bytes32)
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

            slot0 = self.state_view.functions.getSlot0(pool_id_bytes).call()
            sqrt_price_x96 = int(slot0[0])
            current_tick = int(slot0[1])

            current_price = sqrt_price_x96_to_decimal(
                sqrt_price_x96,
                self.config.token0_decimals,
                self.config.token1_decimals,
            )
            if self.config.invert_price and current_price > 0:
                current_price = Decimal(1) / current_price

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

    # === Position override ===

    async def get_position(self) -> Position:
        """Get current position including real LP data."""
        pos = await super().get_position()
        token_ids = self.get_owned_positions()
        pos_state = self.get_position_state(token_ids[0]) if token_ids else None
        position_value_usd, _, our_share_pct = self.get_pool_metrics(pos_state)
        lp_position = None
        if pos_state:
            lp_position = LPPosition(
                token_id=str(pos_state.token_id),
                liquidity=str(pos_state.liquidity),
                range_min=pos_state.price_lower,
                range_max=pos_state.price_upper,
                in_range=pos_state.in_range,
                our_share_pct=our_share_pct,
            )
        return pos.model_copy(update={"lp_position": lp_position, "position_value_usd": position_value_usd})

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
        tx_params["value"] = 0
        tx_params["gas"] = 2_000_000  # placeholder; replaced by estimate below
        tx = self.position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)
        estimated = self.w3.eth.estimate_gas({"from": tx["from"], "to": tx["to"], "data": tx["data"], "value": 0})
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
        tx_params["value"] = 0
        tx_params["gas"] = 2_000_000  # placeholder; replaced by estimate below
        tx = self.position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)
        estimated = self.w3.eth.estimate_gas({"from": tx["from"], "to": tx["to"], "data": tx["data"], "value": 0})
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
        if not sqrt_p or not pool_liquidity:
            return None, None, None

        L = pos_state.liquidity
        sqrt_lower = int(_tick_to_sqrt_x96(pos_state.tick_lower))
        sqrt_upper = int(_tick_to_sqrt_x96(pos_state.tick_upper))
        sp = int(sqrt_p)

        t0_scale = Decimal(10 ** self.config.token0_decimals)
        t1_scale = Decimal(10 ** self.config.token1_decimals)

        if sp <= sqrt_lower:
            amount0 = Decimal(L * _Q96 * (sqrt_upper - sqrt_lower) // (sqrt_lower * sqrt_upper)) / t0_scale
            amount1 = Decimal(0)
        elif sp >= sqrt_upper:
            amount0 = Decimal(0)
            amount1 = Decimal(L * (sqrt_upper - sqrt_lower) // _Q96) / t1_scale
        else:
            amount0 = Decimal(L * _Q96 * (sqrt_upper - sp) // (sp * sqrt_upper)) / t0_scale
            amount1 = Decimal(L * (sp - sqrt_lower) // _Q96) / t1_scale

        if self.config.cngn_is_token0:
            # Base: token0=cNGN (6 dec), token1=USDC (6 dec)
            cngn_price_usd = (sqrt_p / _Q96) ** 2
            position_value_usd = amount0 * cngn_price_usd + amount1
        else:
            # BSC: token0=USDT (18 dec), token1=cNGN (6 dec)
            dec_adj = Decimal(10 ** (self.config.token0_decimals - self.config.token1_decimals))
            cngn_price_usd = Decimal(1) / ((sqrt_p / _Q96) ** 2 * dec_adj)
            position_value_usd = amount0 + amount1 * cngn_price_usd

        our_share_pct = Decimal(L) / pool_liquidity * Decimal(100) if pos_state.in_range else Decimal(0)

        return position_value_usd, None, our_share_pct

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
        estimated = self.w3.eth.estimate_gas({"from": tx["from"], "to": tx["to"], "data": tx["data"], "value": 0})
        tx["gas"] = int(estimated * 1.2)
        token_out = (
            self.config.token1_address
            if token_in.lower() == self.config.token0_address.lower()
            else self.config.token0_address
        )
        return await self._send_transaction(tx, self.lp_account, output_token=token_out)


    async def prepare_lp_balance(self, tick_lower: int, tick_upper: int) -> bool | None:
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
            return

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_price_x96, self.config.token0_decimals, self.config.token1_decimals)

        # Price of token0 in token1 units (for value normalisation)
        price = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2 * Decimal(
            10 ** self.config.token0_decimals
        ) / Decimal(10 ** self.config.token1_decimals)

        # Total value in token1 units
        total_value = balance0 * price + balance1
        if total_value == 0:
            return

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
            return

        if balance0 > target0:
            surplus = balance0 - target0
            surplus_raw = int(surplus * Decimal(10 ** self.config.token0_decimals))
            min_out = int(surplus * price * Decimal("0.99") * Decimal(10 ** self.config.token1_decimals))
            logger.info("lp_swap_to_ratio", venue=self.name, direction="token0→token1",
                        surplus=float(surplus), min_out=min_out)
            result = await self._swap_from_lp(self.config.token0_address, surplus_raw, min_out)
        else:
            surplus = balance1 - target1
            surplus_raw = int(surplus * Decimal(10 ** self.config.token1_decimals))
            min_out_dec = surplus / price * Decimal("0.99")
            min_out = int(min_out_dec * Decimal(10 ** self.config.token0_decimals))
            logger.info("lp_swap_to_ratio", venue=self.name, direction="token1→token0",
                        surplus=float(surplus), min_out=min_out)
            result = await self._swap_from_lp(self.config.token1_address, surplus_raw, min_out)

        if result.status != "confirmed":
            logger.warning("lp_ratio_swap_failed_skipping_mint", venue=self.name, error=result.error)
            return False

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

    async def _approve_lp_tokens_if_needed(self):
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
            tx_params["value"] = 0
            tx_params["gas"] = 100_000
            tx = token.functions.approve(
                Web3.to_checksum_address(pm_addr), 2 ** 256 - 1
            ).build_transaction(tx_params)
            result = await self._send_transaction(tx, self.lp_account)
            if result.status == "confirmed":
                self._lp_approvals_done.add(cache_key)
            else:
                logger.error("lp_token_approval_failed", venue=self.name, token=token_addr, error=result.error)
