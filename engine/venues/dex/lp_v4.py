"""Uniswap V4 LP adapter — extends BaseV4DexAdapter with position management."""

import math
import time as _time
from decimal import Decimal
from typing import Optional

import structlog
from eth_abi import encode
from web3 import Web3

from engine.api.schemas import DexParams, LPPosition, Position, TxResult
from .shared import ERC20_ABI, MULTICALL3_ABI, MULTICALL3_ADDRESS, PositionState, _decode_uint256, _encode_balance_of
from .v4 import BaseV4DexAdapter, V4ExecutionConfig

logger = structlog.get_logger()

# V4 PositionManager action codes
_V4_LP_MINT_POSITION      = 0
_V4_LP_DECREASE_LIQUIDITY = 2
_V4_LP_BURN_POSITION      = 3
_V4_LP_SETTLE_PAIR        = 17  # 0x11
_V4_LP_TAKE_PAIR          = 18  # 0x12

_DEFAULT_LP_GAS = 500_000

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

            from .shared import sqrt_price_x96_to_decimal
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
        multicall = self.w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI,
        )
        results = multicall.functions.aggregate3([
            (Web3.to_checksum_address(self.config.token0_address), True, _encode_balance_of(self.lp_account.address)),
            (Web3.to_checksum_address(self.config.token0_address), True, _encode_balance_of(self.trade_account.address)),
            (Web3.to_checksum_address(self.config.token1_address), True, _encode_balance_of(self.lp_account.address)),
            (Web3.to_checksum_address(self.config.token1_address), True, _encode_balance_of(self.trade_account.address)),
        ]).call()

        t0_amount = Decimal(
            (_decode_uint256(results[0][1]) if results[0][0] else 0)
            + (_decode_uint256(results[1][1]) if results[1][0] else 0)
        ) / Decimal(10 ** self.config.token0_decimals)
        t1_amount = Decimal(
            (_decode_uint256(results[2][1]) if results[2][0] else 0)
            + (_decode_uint256(results[3][1]) if results[3][0] else 0)
        ) / Decimal(10 ** self.config.token1_decimals)

        lp_position = None
        token_ids = self.get_owned_positions()
        pos_state = None
        if token_ids:
            pos_state = self.get_position_state(token_ids[0])

        position_value_usd, volume_24h_usd, our_share_pct = self.get_pool_metrics(pos_state)

        if pos_state:
            lp_position = LPPosition(
                token_id=str(pos_state.token_id),
                liquidity=str(pos_state.liquidity),
                range_min=pos_state.price_lower,
                range_max=pos_state.price_upper,
                in_range=pos_state.in_range,
                our_share_pct=our_share_pct,
            )

        balances = {"cngn": Decimal(0), "usdt": Decimal(0), "usdc": Decimal(0)}
        for sym, amount in [
            (self.config.token0_symbol.lower(), t0_amount),
            (self.config.token1_symbol.lower(), t1_amount),
        ]:
            if sym in balances:
                balances[sym] = amount
            else:
                balances["usdt"] = amount

        return Position(
            venue=self.name,
            pair=f"{self.config.token0_symbol}/{self.config.token1_symbol}",
            timestamp=int(_time.time() * 1000),
            balances=balances,
            lp_position=lp_position,
            position_value_usd=position_value_usd,
            volume_24h_usd=volume_24h_usd,
        )

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
        tx_params["gas"] = _DEFAULT_LP_GAS
        tx = self.position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)

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

    async def remove_position(self, token_id: int) -> TxResult:
        """Remove V4 LP position via modifyLiquidities (decrease + burn + take)."""
        if not self.position_manager_contract:
            return TxResult(hash="", status="failed", error="no position_manager configured")

        pos = self.get_position_state(token_id)
        if not pos:
            return TxResult(hash="", status="failed", error="position not found")

        currency0, currency1, _, _, _ = self._resolve_pool_key()

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
                [currency0, currency1, self.lp_account.address],
            ),
        ]
        unlock_data = encode(["bytes", "bytes[]"], [actions, params])

        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300
        tx_params = self._get_tx_params(self.lp_account)
        tx_params["value"] = 0
        tx_params["gas"] = _DEFAULT_LP_GAS
        tx = self.position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)

        logger.info("v4_remove_position", venue=self.name, token_id=token_id, liquidity=pos.liquidity)
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

        from engine.core.arbitrage.pool_state import get_cached_pool_state, Q96

        sqrt_p, pool_liquidity, _, _ = get_cached_pool_state(self.config.pool_id)
        if not sqrt_p or not pool_liquidity:
            return None, None, None

        Q96_INT = int(Q96)
        L = pos_state.liquidity
        sqrt_lower = int(math.exp(pos_state.tick_lower * math.log(1.0001) / 2) * Q96_INT)
        sqrt_upper = int(math.exp(pos_state.tick_upper * math.log(1.0001) / 2) * Q96_INT)
        sp = int(sqrt_p)

        t0_scale = Decimal(10 ** self.config.token0_decimals)
        t1_scale = Decimal(10 ** self.config.token1_decimals)

        if sp <= sqrt_lower:
            amount0 = Decimal(L * Q96_INT * (sqrt_upper - sqrt_lower) // (sqrt_lower * sqrt_upper)) / t0_scale
            amount1 = Decimal(0)
        elif sp >= sqrt_upper:
            amount0 = Decimal(0)
            amount1 = Decimal(L * (sqrt_upper - sqrt_lower) // Q96_INT) / t1_scale
        else:
            amount0 = Decimal(L * Q96_INT * (sqrt_upper - sp) // (sp * sqrt_upper)) / t0_scale
            amount1 = Decimal(L * (sp - sqrt_lower) // Q96_INT) / t1_scale

        if self.config.token0_symbol.upper() == "CNGN":
            # Base: token0=cNGN (6 dec), token1=USDC (6 dec)
            cngn_price_usd = (sqrt_p / Q96) ** 2
            position_value_usd = amount0 * cngn_price_usd + amount1
        else:
            # BSC: token0=USDT (18 dec), token1=cNGN (6 dec)
            dec_adj = Decimal(10 ** (self.config.token0_decimals - self.config.token1_decimals))
            cngn_price_usd = Decimal(1) / ((sqrt_p / Q96) ** 2 * dec_adj)
            position_value_usd = amount0 + amount1 * cngn_price_usd

        our_share_pct = Decimal(L) / pool_liquidity * Decimal(100) if pos_state.in_range else Decimal(0)

        return position_value_usd, None, our_share_pct

    # === Capital allocation ===

    def calculate_mint_amounts(self) -> tuple[int, int]:
        """Return raw token amounts to deploy, capped by LP wallet balance."""
        balance0_raw = self.token0.functions.balanceOf(self.lp_account.address).call()
        balance1_raw = self.token1.functions.balanceOf(self.lp_account.address).call()

        balance0 = Decimal(balance0_raw) / Decimal(10 ** self.config.token0_decimals)
        balance1 = Decimal(balance1_raw) / Decimal(10 ** self.config.token1_decimals)

        amount0 = min(self.params.deploy_token0, balance0)
        amount1 = min(self.params.deploy_token1, balance1)

        logger.info(
            "calculated_mint_amounts",
            venue=self.name,
            balance0=float(balance0),
            balance1=float(balance1),
            deploy_token0=float(self.params.deploy_token0),
            deploy_token1=float(self.params.deploy_token1),
            amount0=float(amount0),
            amount1=float(amount1),
        )

        return (
            int(amount0 * Decimal(10 ** self.config.token0_decimals)),
            int(amount1 * Decimal(10 ** self.config.token1_decimals)),
        )

    def get_trade_token_balances(self) -> tuple[Decimal, Decimal]:
        """Return (token0, token1) balances of the trade account in decimal units."""
        raw0 = self.token0.functions.balanceOf(self.trade_account.address).call()
        raw1 = self.token1.functions.balanceOf(self.trade_account.address).call()
        return (
            Decimal(raw0) / Decimal(10 ** self.config.token0_decimals),
            Decimal(raw1) / Decimal(10 ** self.config.token1_decimals),
        )

    async def transfer_from_trade_to_lp(self, token_index: int, amount: Decimal) -> TxResult:
        """Transfer token0 (index 0) or token1 (index 1) from trade account to LP account."""
        token = self.token0 if token_index == 0 else self.token1
        decimals = self.config.token0_decimals if token_index == 0 else self.config.token1_decimals
        amount_raw = int(amount * Decimal(10 ** decimals))
        tx = token.functions.transfer(
            self.lp_account.address, amount_raw
        ).build_transaction(self._get_tx_params(self.trade_account))
        tx["gas"] = 100_000
        return await self._send_transaction(tx, self.trade_account)

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
        Q96 = 2 ** 96
        sqrt_a = int(math.exp(tick_lower * math.log(1.0001) / 2) * Q96)
        sqrt_b = int(math.exp(tick_upper * math.log(1.0001) / 2) * Q96)
        sqrt_p = sqrt_price_x96

        if sqrt_p <= sqrt_a:
            if sqrt_b == sqrt_a:
                return 0
            return amount0 * sqrt_a * sqrt_b // ((sqrt_b - sqrt_a) * Q96)
        elif sqrt_p >= sqrt_b:
            if sqrt_b == sqrt_a:
                return 0
            return amount1 * Q96 // (sqrt_b - sqrt_a)
        else:
            L0 = amount0 * sqrt_p * sqrt_b // ((sqrt_b - sqrt_p) * Q96) if sqrt_b > sqrt_p else 0
            L1 = amount1 * Q96 // (sqrt_p - sqrt_a) if sqrt_p > sqrt_a else 0
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
            if allowance >= 1:
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
