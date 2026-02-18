"""Shared base class for UniswapV3-style DEX adapters.

Also provides PoolPriceReader for read-only on-chain price fetching
(no private keys required).
"""

import asyncio
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
import math
import statistics
import structlog

from web3 import Web3
from web3.types import TxReceipt

from engine.api.schemas import Position, PriceQuote, LPPosition, TxResult, DexParams
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class PoolConfig:
    """Chain and contract addresses for a specific pool."""

    chain_id: int
    chain_name: str
    rpc_url: str
    pool_address: str
    nft_manager_address: str
    router_address: str
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    tick_spacing: int


@dataclass
class PoolReadConfig:
    """Minimal config for read-only pool price fetching (no keys needed).

    ``token0`` / ``token1`` must match the **actual on-chain ordering**
    (the pool always has token0 < token1 by address).

    Set ``invert_price=True`` when the pool's native price direction
    (token1-per-token0) is the opposite of what the consumer expects.
    For example, if the pool is USDT/cNGN but you want cNGN-per-USD,
    set ``invert_price=True`` so the reader returns ``1 / pool_price``.
    """

    rpc_url: str
    pool_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    invert_price: bool = False


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
    in_range: bool


# =============================================================================
# Shared price math
# =============================================================================


def sqrt_price_x96_to_decimal(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """Convert a UniswapV3/CL sqrtPriceX96 value to a human-readable price.

    Returns the price of token0 denominated in token1, adjusted for
    the decimal difference between the two tokens.
    """
    price = (Decimal(sqrt_price_x96) / Decimal(2**96)) ** 2
    decimal_diff = token0_decimals - token1_decimals
    price *= Decimal(10**decimal_diff)
    return price


# Function selector for slot0(): keccak256("slot0()")[:4] = 0x3850c7bd
# This is the same across all UniswapV3-style pools (Uniswap, Aerodrome,
# PancakeSwap, etc.) since the function signature is always `slot0()`.
SLOT0_SELECTOR = bytes.fromhex("3850c7bd")


# =============================================================================
# PoolPriceReader -- read-only, no private keys
# =============================================================================


class PoolPriceReader:
    """Read-only price reader for any UniswapV3-style concentrated liquidity pool.

    Only needs an RPC URL and pool address. Uses raw ``eth_call`` with the
    ``slot0()`` selector and parses the first 32-byte word (sqrtPriceX96)
    directly from the response bytes.

    This approach is **protocol-agnostic** -- different forks return different
    numbers of fields from slot0 (Aerodrome has 6, PancakeSwap has 7 with
    uint32 feeProtocol, standard UniV3 has 7 with uint8 feeProtocol), but the
    first word is always sqrtPriceX96 in all of them.

    No private keys, no NFT manager, no router -- just a single view call.
    """

    def __init__(self, config: PoolReadConfig, source_name: str):
        self.config = config
        self.source_name = source_name

        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        self._pool_address = Web3.to_checksum_address(config.pool_address)

    def get_price(self) -> Optional[PriceQuote]:
        """Read the current pool price via a single slot0() RPC call.

        Parses raw return bytes instead of using a typed ABI, making this
        compatible with all UniswapV3-style pool implementations.
        """
        try:
            result = self._w3.eth.call(
                {"to": self._pool_address, "data": SLOT0_SELECTOR}
            )

            if len(result) < 64:
                logger.error(
                    "slot0_response_too_short",
                    source=self.source_name,
                    length=len(result),
                )
                return None

            # First 32 bytes = sqrtPriceX96 (uint160, left-padded to 32 bytes)
            sqrt_price_x96 = int.from_bytes(result[:32], "big")

            mid = sqrt_price_x96_to_decimal(
                sqrt_price_x96,
                self.config.token0_decimals,
                self.config.token1_decimals,
            )

            if mid <= 0:
                return None

            # Invert if the pool's native direction is opposite to desired
            if self.config.invert_price:
                mid = Decimal(1) / mid

            return PriceQuote(
                source=f"{self.source_name}_pool",
                timestamp=int(_time.time() * 1000),
                bid=mid,
                ask=mid,
                mid=mid,
            )
        except Exception as e:
            logger.error(
                "pool_price_read_failed",
                source=self.source_name,
                pool=self.config.pool_address,
                error=str(e),
            )
            return None


# =============================================================================
# ERC20 ABI
# =============================================================================


# Minimal ERC20 ABI for approvals and balance checks
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
]


class BaseDexAdapter(VenueAdapter, ABC):
    """
    Shared implementation for UniswapV3-style DEXes.

    Subclasses only need to provide ABIs and any protocol-specific quirks.
    """

    def __init__(
        self,
        pool_config: PoolConfig,
        lp_private_key: str,
        trade_private_key: str,
        strategy_params: DexParams,
    ):
        self.config = pool_config
        self.params = strategy_params

        # Web3 setup
        self.w3 = Web3(Web3.HTTPProvider(pool_config.rpc_url))
        self.lp_account = self.w3.eth.account.from_key(lp_private_key)
        self.trade_account = self.w3.eth.account.from_key(trade_private_key)

        # Contracts - subclasses provide ABIs
        self.pool_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_config.pool_address),
            abi=self.get_pool_abi(),
        )
        self.nft_manager = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_config.nft_manager_address),
            abi=self.get_nft_manager_abi(),
        )
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_config.router_address),
            abi=self.get_router_abi(),
        )

        # Per-account nonce locks to prevent concurrent transaction collisions
        self._nonce_locks: dict[str, asyncio.Lock] = {}

        # Token contracts for balance queries
        self.token0 = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_config.token0_address),
            abi=ERC20_ABI,
        )
        self.token1 = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_config.token1_address),
            abi=ERC20_ABI,
        )

    # === Abstract methods for protocol-specific ABIs ===

    @abstractmethod
    def get_pool_abi(self) -> list:
        """Return the pool contract ABI."""
        pass

    @abstractmethod
    def get_nft_manager_abi(self) -> list:
        """Return the NFT position manager ABI."""
        pass

    @abstractmethod
    def get_router_abi(self) -> list:
        """Return the swap router ABI."""
        pass

    # === VenueAdapter implementation ===

    async def get_position(self) -> Position:
        """Get current position including LP and wallet balances."""
        # Get wallet balances
        token0_bal = self.token0.functions.balanceOf(self.lp_account.address).call()
        token1_bal = self.token1.functions.balanceOf(self.lp_account.address).call()

        # Normalize to decimals
        cngn_bal = Decimal(token0_bal) / Decimal(10**self.config.token0_decimals)
        stable_bal = Decimal(token1_bal) / Decimal(10**self.config.token1_decimals)

        # Get LP position if exists
        lp_position = None
        token_ids = self.get_owned_positions()
        if token_ids:
            pos_state = self.get_position_state(token_ids[0])
            if pos_state:
                lp_position = LPPosition(
                    token_id=str(pos_state.token_id),
                    liquidity=str(pos_state.liquidity),
                    range_min=pos_state.price_lower,
                    range_max=pos_state.price_upper,
                    in_range=pos_state.in_range,
                )

        # Determine which is USDT vs USDC based on symbol
        balances = {"cngn": cngn_bal, "usdt": Decimal(0), "usdc": Decimal(0)}
        if self.config.token1_symbol.upper() == "USDC":
            balances["usdc"] = stable_bal
        else:
            balances["usdt"] = stable_bal

        import time

        return Position(
            venue=self.name,
            pair=f"{self.config.token0_symbol}/{self.config.token1_symbol}",
            timestamp=int(time.time() * 1000),
            balances=balances,
            lp_position=lp_position,
        )

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Get current pool price."""
        state = self.get_current_state()
        import time

        return PriceQuote(
            source=f"{self.name}_pool",
            timestamp=int(time.time() * 1000),
            bid=state["price"],
            ask=state["price"],
            mid=state["price"],
        )

    # === Pool state queries ===

    def get_current_state(self) -> dict:
        """Get current pool state (tick, sqrtPriceX96, etc.)."""
        slot0 = self.pool_contract.functions.slot0().call()
        return {
            "sqrt_price_x96": slot0[0],
            "tick": slot0[1],
            "price": self._sqrt_price_to_decimal(slot0[0]),
        }

    def get_position_state(self, token_id: int) -> Optional[PositionState]:
        """Get position details from NFT manager."""
        try:
            pos = self.nft_manager.functions.positions(token_id).call()
            tick_lower, tick_upper = pos[5], pos[6]
            liquidity = pos[7]

            if liquidity == 0:
                return None

            current_tick = self.get_current_state()["tick"]

            return PositionState(
                token_id=token_id,
                liquidity=liquidity,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                tokens_owed_0=pos[8],
                tokens_owed_1=pos[9],
                price_lower=self._tick_to_price(tick_lower),
                price_upper=self._tick_to_price(tick_upper),
                in_range=tick_lower <= current_tick <= tick_upper,
            )
        except Exception as e:
            logger.warning("get_position_failed", token_id=token_id, error=str(e))
            return None

    def get_owned_positions(self) -> list[int]:
        """Get all position token IDs owned by LP wallet."""
        try:
            balance = self.nft_manager.functions.balanceOf(self.lp_account.address).call()

            token_ids = []
            for i in range(balance):
                token_id = self.nft_manager.functions.tokenOfOwnerByIndex(
                    self.lp_account.address, i
                ).call()
                token_ids.append(token_id)

            return token_ids
        except Exception as e:
            logger.warning("get_owned_positions_failed", error=str(e))
            return []

    # === Capital allocation ===

    def calculate_mint_amounts(
        self,
        reference_price_usd: Optional[Decimal] = None,
    ) -> tuple[int, int]:
        """
        Calculate how much of each token to use for minting, respecting reserves and limits.

        This prevents "all-in LP" by applying:
        1. max_utilization_percent - caps % of balance to deploy
        2. min_reserve_token0/token1 - keeps minimum in wallet
        3. max_position_usd - hard cap on total position value

        Args:
            reference_price_usd: Optional USD price of token0 for max_position_usd calc

        Returns:
            (amount0, amount1) in raw token units (not decimal-adjusted)
        """
        # Get current balances (raw units)
        balance0_raw = self.token0.functions.balanceOf(self.lp_account.address).call()
        balance1_raw = self.token1.functions.balanceOf(self.lp_account.address).call()

        # Convert to decimal for calculations
        balance0 = Decimal(balance0_raw) / Decimal(10**self.config.token0_decimals)
        balance1 = Decimal(balance1_raw) / Decimal(10**self.config.token1_decimals)

        # 1. Apply max utilization percent
        max_util = self.params.max_utilization_percent / Decimal("100")
        available0 = balance0 * max_util
        available1 = balance1 * max_util

        # 2. Subtract minimum reserves
        available0 = max(Decimal("0"), available0 - self.params.min_reserve_token0)
        available1 = max(Decimal("0"), available1 - self.params.min_reserve_token1)

        # Also ensure we don't go below reserves even after utilization calc
        max_from_reserve0 = max(Decimal("0"), balance0 - self.params.min_reserve_token0)
        max_from_reserve1 = max(Decimal("0"), balance1 - self.params.min_reserve_token1)
        available0 = min(available0, max_from_reserve0)
        available1 = min(available1, max_from_reserve1)

        # 3. Apply max position USD cap if set
        if self.params.max_position_usd and reference_price_usd:
            # Estimate total position value
            # token0 value + token1 value (token1 assumed to be stablecoin ≈ $1)
            token0_usd_value = available0 * reference_price_usd
            token1_usd_value = available1  # Stablecoin

            total_usd = token0_usd_value + token1_usd_value

            if total_usd > self.params.max_position_usd:
                # Scale down proportionally
                scale_factor = self.params.max_position_usd / total_usd
                available0 = available0 * scale_factor
                available1 = available1 * scale_factor

        # Convert back to raw units
        amount0 = int(available0 * Decimal(10**self.config.token0_decimals))
        amount1 = int(available1 * Decimal(10**self.config.token1_decimals))

        logger.info(
            "calculated_mint_amounts",
            venue=self.name,
            balance0=float(balance0),
            balance1=float(balance1),
            available0=float(available0),
            available1=float(available1),
            max_utilization_percent=float(self.params.max_utilization_percent),
            min_reserve_token0=float(self.params.min_reserve_token0),
            min_reserve_token1=float(self.params.min_reserve_token1),
        )

        return amount0, amount1

    def get_deployable_balances(self) -> dict[str, Decimal]:
        """
        Get balances available for deployment (after reserves).

        Returns:
            Dict with 'token0' and 'token1' available amounts
        """
        amount0, amount1 = self.calculate_mint_amounts()
        return {
            "token0": Decimal(amount0) / Decimal(10**self.config.token0_decimals),
            "token1": Decimal(amount1) / Decimal(10**self.config.token1_decimals),
        }

    # === Strategy logic ===

    def calculate_tick_range(self, prices: list[Decimal]) -> tuple[int, int]:
        """
        Calculate optimal tick range using SD-based strategy.

        Args:
            prices: Historical prices for volatility calculation

        Returns:
            (tick_lower, tick_upper) tuple
        """
        if self.params.lookback_points:
            prices = prices[-self.params.lookback_points :]

        if len(prices) < 2:
            raise ValueError("Insufficient price history for SD calculation")

        # Calculate mean and standard deviation
        float_prices = [float(p) for p in prices]
        mean_price = statistics.mean(float_prices)
        std_dev = statistics.stdev(float_prices)

        # Range = mean +/- (SD * multiplier)
        multiplier = float(self.params.sd_multiplier)
        lower_price = mean_price - (std_dev * multiplier)
        upper_price = mean_price + (std_dev * multiplier)

        # Ensure positive
        lower_price = max(lower_price, 0.0001)

        # Convert to ticks
        tick_lower = self._price_to_tick(Decimal(str(lower_price)))
        tick_upper = self._price_to_tick(Decimal(str(upper_price)))

        # Align to tick spacing
        tick_lower = (tick_lower // self.config.tick_spacing) * self.config.tick_spacing
        tick_upper = (
            (tick_upper // self.config.tick_spacing) + 1
        ) * self.config.tick_spacing

        # Apply min/max width constraints
        tick_width = tick_upper - tick_lower
        if tick_width < self.params.min_tick_width:
            mid = (tick_lower + tick_upper) // 2
            tick_lower = mid - self.params.min_tick_width // 2
            tick_upper = mid + self.params.min_tick_width // 2
        elif tick_width > self.params.max_tick_width:
            mid = (tick_lower + tick_upper) // 2
            tick_lower = mid - self.params.max_tick_width // 2
            tick_upper = mid + self.params.max_tick_width // 2

        # Re-align to tick spacing after width constraints (width adjustment can un-align)
        tick_lower = (tick_lower // self.config.tick_spacing) * self.config.tick_spacing
        tick_upper = (
            (tick_upper // self.config.tick_spacing) + 1
        ) * self.config.tick_spacing

        logger.info(
            "calculated_tick_range",
            venue=self.name,
            mean_price=mean_price,
            std_dev=std_dev,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
        )

        return tick_lower, tick_upper

    # === Position management ===

    async def mint_position(
        self,
        amount0: int,
        amount1: int,
        tick_lower: int,
        tick_upper: int,
    ) -> TxResult:
        """Create new LP position."""
        # Approve tokens
        await self._approve_if_needed(
            self.config.token0_address,
            self.config.nft_manager_address,
            amount0,
        )
        await self._approve_if_needed(
            self.config.token1_address,
            self.config.nft_manager_address,
            amount1,
        )

        # Build mint transaction
        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300

        slippage = self.params.max_slippage_percent / Decimal("100")
        amount0_min = int(amount0 * (1 - slippage))
        amount1_min = int(amount1 * (1 - slippage))

        mint_params = (
            Web3.to_checksum_address(self.config.token0_address),
            Web3.to_checksum_address(self.config.token1_address),
            self.config.tick_spacing,
            tick_lower,
            tick_upper,
            amount0,
            amount1,
            amount0_min,
            amount1_min,
            self.lp_account.address,
            deadline,
        )

        tx = self.nft_manager.functions.mint(mint_params).build_transaction(
            self._get_tx_params(self.lp_account)
        )

        return await self._send_transaction(tx, self.lp_account)

    async def remove_position(self, token_id: int) -> TxResult:
        """Remove liquidity, collect fees, burn NFT."""
        position = self.get_position_state(token_id)
        if not position:
            return TxResult(hash="", status="failed", error="Position not found")

        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300

        # Decrease liquidity to 0
        decrease_params = (
            token_id,
            position.liquidity,
            0,  # amount0Min
            0,  # amount1Min
            deadline,
        )

        tx1 = self.nft_manager.functions.decreaseLiquidity(
            decrease_params
        ).build_transaction(self._get_tx_params(self.lp_account))

        result = await self._send_transaction(tx1, self.lp_account)
        if result.status != "confirmed":
            return result

        # Collect tokens
        max_uint128 = 2**128 - 1
        collect_params = (
            token_id,
            self.lp_account.address,
            max_uint128,
            max_uint128,
        )

        tx2 = self.nft_manager.functions.collect(collect_params).build_transaction(
            self._get_tx_params(self.lp_account)
        )

        result = await self._send_transaction(tx2, self.lp_account)
        if result.status != "confirmed":
            return result

        # Burn NFT
        tx3 = self.nft_manager.functions.burn(token_id).build_transaction(
            self._get_tx_params(self.lp_account)
        )

        return await self._send_transaction(tx3, self.lp_account)

    async def swap(
        self,
        token_in: str,
        amount_in: int,
        min_amount_out: int,
    ) -> TxResult:
        """Execute swap using trade wallet (not LP wallet)."""
        await self._approve_if_needed(
            token_in,
            self.config.router_address,
            amount_in,
            account=self.trade_account,
        )

        token_out = (
            self.config.token1_address
            if token_in.lower() == self.config.token0_address.lower()
            else self.config.token0_address
        )

        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300

        swap_params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            self.config.tick_spacing,
            self.trade_account.address,
            deadline,
            amount_in,
            min_amount_out,
            0,  # sqrtPriceLimitX96
        )

        tx = self.router.functions.exactInputSingle(swap_params).build_transaction(
            self._get_tx_params(self.trade_account)
        )

        return await self._send_transaction(tx, self.trade_account)

    # === Price/tick math ===

    def _sqrt_price_to_decimal(self, sqrt_price_x96: int) -> Decimal:
        """Convert sqrtPriceX96 to decimal price (delegates to shared utility)."""
        return sqrt_price_x96_to_decimal(
            sqrt_price_x96,
            self.config.token0_decimals,
            self.config.token1_decimals,
        )

    def _tick_to_price(self, tick: int) -> Decimal:
        """Convert tick to price."""
        price = Decimal("1.0001") ** tick
        decimal_diff = self.config.token0_decimals - self.config.token1_decimals
        price *= Decimal(10**decimal_diff)
        return price

    def _price_to_tick(self, price: Decimal) -> int:
        """Convert price to tick."""
        decimal_diff = self.config.token0_decimals - self.config.token1_decimals
        adjusted = float(price) / (10**decimal_diff)
        return int(math.log(adjusted) / math.log(1.0001))

    # === Transaction helpers ===

    def _get_tx_params(self, account) -> dict:
        """Build base transaction parameters with EIP-1559 gas."""
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", self.w3.eth.gas_price)

        return {
            "from": account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "maxFeePerGas": int(base_fee * 1.5),
            "maxPriorityFeePerGas": self.w3.to_wei(0.1, "gwei"),
            "chainId": self.config.chain_id,
        }

    async def _send_transaction(self, tx: dict, account) -> TxResult:
        """Sign, send, and wait for transaction. Serializes per-account to prevent nonce collisions."""
        try:
            # Estimate gas (nonce doesn't affect gas, so do this outside the lock)
            tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)

            # Acquire per-account lock, set nonce, sign, and broadcast atomically
            if account.address not in self._nonce_locks:
                self._nonce_locks[account.address] = asyncio.Lock()

            async with self._nonce_locks[account.address]:
                tx["nonce"] = self.w3.eth.get_transaction_count(account.address, "pending")
                signed = account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

            # Wait for receipt
            receipt: TxReceipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=120
            )

            status = "confirmed" if receipt["status"] == 1 else "failed"

            logger.info(
                "transaction_sent",
                venue=self.name,
                hash=tx_hash.hex(),
                status=status,
                gas_used=receipt["gasUsed"],
            )

            return TxResult(
                hash=tx_hash.hex(),
                status=status,
                gas_used=receipt["gasUsed"],
            )
        except Exception as e:
            logger.error("transaction_failed", venue=self.name, error=str(e))
            return TxResult(hash="", status="failed", error=str(e))

    async def _approve_if_needed(
        self,
        token: str,
        spender: str,
        amount: int,
        account=None,
    ):
        """Approve token spending if allowance insufficient."""
        if account is None:
            account = self.lp_account

        token_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(token),
            abi=ERC20_ABI,
        )

        allowance = token_contract.functions.allowance(
            account.address, Web3.to_checksum_address(spender)
        ).call()

        if allowance < amount:
            logger.info(
                "approving_token",
                token=token,
                spender=spender,
                current_allowance=allowance,
            )

            tx = token_contract.functions.approve(
                Web3.to_checksum_address(spender), 2**256 - 1
            ).build_transaction(self._get_tx_params(account))

            await self._send_transaction(tx, account)
