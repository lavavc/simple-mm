"""Uniswap V4 LP adapter — extends BaseV4DexAdapter with position management."""

import math
import time as _time
from decimal import Decimal
from typing import Optional

import structlog
from eth_abi import encode
from web3 import Web3

from engine.api.schemas import DexParams, LPPosition, Position, TxResult
from .shared import ERC20_ABI, MULTICALL3_ABI, MULTICALL3_ADDRESS, PositionState, _decode_uint256, _encode_balance_of, sqrt_price_x96_to_decimal
from .v4 import BaseV4DexAdapter, V4ExecutionConfig

logger = structlog.get_logger()

_Q96 = 2 ** 96  # shared constant — used in float, int, and Decimal contexts below


def _tick_to_sqrt_x96(tick: int) -> float:
    """Return sqrtPriceX96 as a float for a given tick."""
    return math.exp(tick * math.log(1.0001) / 2) * _Q96


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
                price_lower=self._tick_to_price(tick_lower),
                price_upper=self._tick_to_price(tick_upper),
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

        from engine.core.arbitrage.pool_state import get_cached_pool_state

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

    def _compute_required_ratio(
        self, tick_lower: int, tick_upper: int, sqrt_price_x96: int
    ) -> tuple[Decimal, Decimal]:
        """Return (r0, r1) — token amounts per unit of liquidity at the current price.

        Pure math, no RPC. Uses pool state only; downside_skew is not consulted.
        r0 = (sqrt_b − sqrt_p) / (sqrt_p × sqrt_b)   [token0 per unit L when in range]
        r1 = (sqrt_p − sqrt_a)                         [token1 per unit L when in range]
        Adjusts for decimal differences between token0 and token1.
        """
        sqrt_a = _tick_to_sqrt_x96(tick_lower)
        sqrt_b = _tick_to_sqrt_x96(tick_upper)
        sqrt_p = float(sqrt_price_x96)

        # Formulas mirror get_pool_metrics: amount0 = L*Q96*(sqrt_b-sp)/(sp*sqrt_b),
        # amount1 = L*(sp-sqrt_a)/Q96.  Per-unit-L, with Q96 absorbed:
        if sqrt_p <= sqrt_a:
            # Below range: position is entirely in token0
            r0 = (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b) * _Q96 if sqrt_a * sqrt_b > 0 else 0.0
            r1 = 0.0
        elif sqrt_p >= sqrt_b:
            # Above range: position is entirely in token1
            r0 = 0.0
            r1 = (sqrt_b - sqrt_a) / _Q96
        else:
            r0 = (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b) * _Q96
            r1 = (sqrt_p - sqrt_a) / _Q96

        # Adjust for decimal scaling: raw amounts are in different units
        dec_adj = Decimal(10 ** self.config.token0_decimals) / Decimal(10 ** self.config.token1_decimals)
        r0_dec = Decimal(str(r0)) / dec_adj
        r1_dec = Decimal(str(r1))
        return r0_dec, r1_dec

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

        r0, r1 = self._compute_required_ratio(tick_lower, tick_upper, sqrt_price_x96)

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

    # === Strategy math ===

    def compute_ewma_stats(self, prices: list[Decimal]) -> tuple[float, float]:
        """Return (ewma_mean, std_dev) from price history using configured lambda."""
        if self.params.lookback_points:
            prices = prices[-self.params.lookback_points:]
        float_prices = [float(p) for p in prices]
        lam = float(self.params.ewma_lambda)
        mean = float_prices[0]
        var = 0.0
        for x in float_prices[1:]:
            delta = x - mean
            mean = lam * mean + (1 - lam) * x
            var = lam * var + (1 - lam) * delta * delta
        return mean, math.sqrt(var)

    def calculate_tick_range(self, prices: list[Decimal], recovery_price: float | None = None) -> tuple[int, int]:
        """Calculate optimal tick range using SD-based strategy."""
        if self.params.lookback_points:
            prices = prices[-self.params.lookback_points:]

        if len(prices) < 2:
            raise ValueError("Insufficient price history for SD calculation")

        mean, std_dev = self.compute_ewma_stats(prices)

        multiplier = float(self.params.sd_multiplier)
        skew = float(self.params.downside_skew)
        if recovery_price is not None and std_dev > 0:
            deviation = (recovery_price - mean) / (std_dev * multiplier)
            skew = max(0.2, min(0.8, skew + deviation * 0.15))
        total = std_dev * multiplier * 2
        lower_price = max(mean - total * skew, 0.0001)
        upper_price = mean + total * (1 - skew)

        tick_lower = self._price_to_tick(Decimal(str(lower_price)))
        tick_upper = self._price_to_tick(Decimal(str(upper_price)))

        spacing = self.config.tick_spacing
        tick_lower = math.floor(tick_lower / spacing) * spacing
        tick_upper = math.ceil(tick_upper / spacing) * spacing

        tick_width = tick_upper - tick_lower
        if tick_width < self.params.min_tick_width:
            mid = (tick_lower + tick_upper) // 2
            tick_lower = mid - self.params.min_tick_width // 2
            tick_upper = mid + self.params.min_tick_width // 2
        elif tick_width > self.params.max_tick_width:
            mid = (tick_lower + tick_upper) // 2
            tick_lower = mid - self.params.max_tick_width // 2
            tick_upper = mid + self.params.max_tick_width // 2

        tick_lower = math.floor(tick_lower / spacing) * spacing
        tick_upper = math.ceil(tick_upper / spacing) * spacing

        logger.info(
            "calculated_tick_range",
            venue=self.name,
            mean_price=mean,
            std_dev=std_dev,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
        )

        return tick_lower, tick_upper

    def _tick_to_price(self, tick: int) -> Decimal:
        decimal_diff = self.config.token0_decimals - self.config.token1_decimals
        price = Decimal("1.0001") ** tick
        price *= Decimal(10 ** decimal_diff)
        return price

    def _price_to_tick(self, price: Decimal) -> int:
        decimal_diff = self.config.token0_decimals - self.config.token1_decimals
        adjusted = float(price) / (10 ** decimal_diff)
        return int(math.log(adjusted) / math.log(1.0001))

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
