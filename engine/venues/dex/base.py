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
from web3.middleware import geth_poa_middleware
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
    pool_fee: Optional[int] = None  # Fee tier for protocols that use fee != tick_spacing (e.g. PancakeSwap)
    invert_price: bool = False  # True when native pool price must be inverted (e.g. PancakeSwap: USDT/cNGN → cNGN/USD)


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
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    invert_price: bool = False


@dataclass
class V4PoolReadConfig:
    """Minimal config for read-only V4 pool price fetching via StateView.

    ``pool_address`` stores the bytes32 pool ID — keeps all downstream cache
    lookups (``_POOL_CACHE[config.pool_address]``) working unchanged.
    """

    pool_manager: str    # PoolManager singleton address
    state_view: str      # StateView contract address
    pool_address: str    # bytes32 pool ID — named pool_address for cache-key compatibility
    rpc_url: str
    token0_address: str
    token1_address: str
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
    current_price: Decimal
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
SLOT0_SELECTOR = bytes.fromhex("3850c7bd")

# Function selector for liquidity(): keccak256("liquidity()")[:4] = 0x1a686502
LIQUIDITY_SELECTOR = bytes.fromhex("1a686502")

# Function selector for balanceOf(address): keccak256("balanceOf(address)")[:4] = 0x70a08231
_BALANCE_OF_SIG = bytes.fromhex("70a08231")

# Uniswap V3 Swap event topic: keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)")
_SWAP_EVENT_TOPIC = bytes.fromhex("c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67")

# Function selector for fee(): keccak256("fee()")[:4] = 0xddca3f43
# Returns uint24 fee tier (e.g. 10000 = 1%, 3000 = 0.3%, 500 = 0.05%)
FEE_SELECTOR = bytes.fromhex("ddca3f43")

# DexScreener chain identifiers
DEXSCREENER_CHAIN_MAP = {8453: "base", 56: "bsc"}

# Multicall3 — deployed at the same address on Ethereum, Base, and BSC
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


def _encode_balance_of(address: str) -> bytes:
    """Encode a balanceOf(address) call for use in Multicall3."""
    return _BALANCE_OF_SIG + bytes(12) + bytes.fromhex(address[2:])


def _decode_uint256(data: bytes) -> int:
    return int.from_bytes(data, "big") if len(data) == 32 else 0


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
        
        # Add POA middleware for chains like BSC/AssetChain that use non-standard block extraData
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
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

        # Cached pool fee in bps — fetched once from the chain at init time
        self._pool_fee_bps: Optional[int] = None

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

    async def get_pool_metrics(
        self, our_liquidity: int = 0
    ) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        """Fetch pool TVL and 24h volume from DexScreener; compute our share on-chain.

        Results are cached for 60s — callers within the same cycle share one HTTP request.
        Returns (pool_tvl_usd, volume_24h_usd, our_share_pct).
        """
        import httpx

        now = _time.time()
        cache_stale = not hasattr(self, "_metrics_cache_time") or now - self._metrics_cache_time > 60

        if cache_stale:
            pool_tvl_usd: Optional[Decimal] = None
            volume_24h_usd: Optional[Decimal] = None
            pool_total_liquidity: int = 0

            try:
                chain = DEXSCREENER_CHAIN_MAP.get(self.config.chain_id)
                if chain:
                    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{self.config.pool_address}"
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(url)
                        data = resp.json()
                    pairs = data.get("pairs") or []
                    if pairs:
                        pair = pairs[0]
                        tvl = pair.get("liquidity", {}).get("usd")
                        vol = pair.get("volume", {}).get("h24")
                        pool_tvl_usd = Decimal(str(tvl)) if tvl is not None else None
                        volume_24h_usd = Decimal(str(vol)) if vol is not None else None
            except Exception as e:
                logger.warning("dexscreener_fetch_failed", venue=self.name, error=str(e))

            try:
                result = self.w3.eth.call(
                    {"to": self.pool_contract.address, "data": LIQUIDITY_SELECTOR}
                )
                if len(result) >= 32:
                    pool_total_liquidity = int.from_bytes(result[:32], "big")
            except Exception as e:
                logger.warning("pool_liquidity_fetch_failed", venue=self.name, error=str(e))

            self._metrics_tvl = pool_tvl_usd
            self._metrics_vol = volume_24h_usd
            self._metrics_total_liq = pool_total_liquidity
            self._metrics_cache_time = now

        our_share_pct: Optional[Decimal] = None
        if our_liquidity > 0 and self._metrics_total_liq > 0:
            our_share_pct = Decimal(our_liquidity) / Decimal(self._metrics_total_liq) * Decimal(100)

        return self._metrics_tvl, self._metrics_vol, our_share_pct

    async def get_position(self) -> Position:
        """Get current position including LP and wallet balances (both LP and trade accounts)."""
        # Get wallet balances — batch all 4 balanceOf calls via Multicall3 (single RPC)
        multicall = self.w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI,
        )
        mc_results = multicall.functions.aggregate3([
            (Web3.to_checksum_address(self.config.token0_address), True, _encode_balance_of(self.lp_account.address)),
            (Web3.to_checksum_address(self.config.token0_address), True, _encode_balance_of(self.trade_account.address)),
            (Web3.to_checksum_address(self.config.token1_address), True, _encode_balance_of(self.lp_account.address)),
            (Web3.to_checksum_address(self.config.token1_address), True, _encode_balance_of(self.trade_account.address)),
        ]).call()
        t0_amount = Decimal(
            (_decode_uint256(mc_results[0][1]) if mc_results[0][0] else 0)
            + (_decode_uint256(mc_results[1][1]) if mc_results[1][0] else 0)
        ) / Decimal(10**self.config.token0_decimals)
        t1_amount = Decimal(
            (_decode_uint256(mc_results[2][1]) if mc_results[2][0] else 0)
            + (_decode_uint256(mc_results[3][1]) if mc_results[3][0] else 0)
        ) / Decimal(10**self.config.token1_decimals)

        # Fetch pool metrics (always — TVL/volume shown regardless of LP activity)
        lp_position = None
        token_ids = self.get_owned_positions()
        our_liquidity = 0
        pos_state = None
        if token_ids:
            pos_state = self.get_position_state(token_ids[0])
            if pos_state:
                our_liquidity = pos_state.liquidity

        pool_tvl_usd, volume_24h_usd, our_share_pct = await self.get_pool_metrics(our_liquidity)

        if pos_state:
            lp_position = LPPosition(
                token_id=str(pos_state.token_id),
                liquidity=str(pos_state.liquidity),
                range_min=pos_state.price_lower,
                range_max=pos_state.price_upper,
                in_range=pos_state.in_range,
                our_share_pct=our_share_pct,
            )

        balances: dict[str, Decimal] = {"cngn": Decimal(0), "usdt": Decimal(0), "usdc": Decimal(0)}
        for sym, amount in [
            (self.config.token0_symbol.lower(), t0_amount),
            (self.config.token1_symbol.lower(), t1_amount),
        ]:
            if sym in balances:
                balances[sym] = amount
            else:
                balances["usdt"] = amount  # fallback for unexpected stable symbols

        import time

        return Position(
            venue=self.name,
            pair=f"{self.config.token0_symbol}/{self.config.token1_symbol}",
            timestamp=int(time.time() * 1000),
            balances=balances,
            lp_position=lp_position,
            pool_tvl_usd=pool_tvl_usd,
            volume_24h_usd=volume_24h_usd,
        )

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Get current pool price."""
        state = self.get_current_state()
        import time

        price = state["price"]
        if self.config.invert_price and price > 0:
            price = Decimal("1") / price

        return PriceQuote(
            source=f"{self.name}_pool",
            timestamp=int(time.time() * 1000),
            bid=price,
            ask=price,
            mid=price,
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

    def get_fee_bps(self, fallback: int = 30) -> int:
        """Pool swap fee in basis points, fetched once from chain and cached.

        Converts from Uniswap fee units (10000 = 1%) to bps (100 = 1%).
        Falls back to ``fallback`` if the RPC call fails.
        """
        if not hasattr(self, "_fee_bps"):
            try:
                fee_raw = self.pool_contract.functions.fee().call()
                self._fee_bps: int | None = fee_raw // 100
            except Exception as e:
                logger.warning("fee_fetch_failed", venue=self.name, error=str(e))
                self._fee_bps = None
        return self._fee_bps if self._fee_bps is not None else fallback

    @property
    def cngn_decimals(self) -> int:
        return self.config.token1_decimals if self.config.invert_price else self.config.token0_decimals

    @property
    def stable_decimals(self) -> int:
        return self.config.token0_decimals if self.config.invert_price else self.config.token1_decimals

    @property
    def stable_address(self) -> str:
        return self.config.token0_address if self.config.invert_price else self.config.token1_address

    @property
    def cngn_address(self) -> str:
        return self.config.token1_address if self.config.invert_price else self.config.token0_address

    @property
    def stable_token(self):
        return self.token0 if self.config.invert_price else self.token1

    @property
    def fee_param(self) -> int:
        """Raw fee tier for router calls. Fetched from chain once and cached."""
        return self.get_fee_bps() * 100

    def get_virtual_reserves(self) -> tuple[Decimal, Decimal] | None:
        """Return (cngn_virtual, stable_virtual) in human-readable units for the active tick."""
        try:
            sqrt_price_x96 = self.get_current_state()["sqrt_price_x96"]
            result = self.w3.eth.call({"to": self.pool_contract.address, "data": LIQUIDITY_SELECTOR})
            if len(result) < 32:
                return None
            L = Decimal(int.from_bytes(result[:32], "big"))
            sqrtP = Decimal(sqrt_price_x96) / Decimal(2**96)
            if sqrtP == 0:
                return None
            x = L / sqrtP / Decimal(10 ** self.config.token0_decimals)
            y = L * sqrtP / Decimal(10 ** self.config.token1_decimals)
            if self.config.invert_price:   # token0=stable, token1=cNGN
                return y, x
            return x, y                    # token0=cNGN, token1=stable
        except Exception as e:
            logger.warning("get_virtual_reserves_failed", venue=self.name, error=str(e))
            return None

    def get_position_state(self, token_id: int) -> Optional[PositionState]:
        """Get position details from NFT manager."""
        try:
            pos = self.nft_manager.functions.positions(token_id).call()
            tick_lower, tick_upper = pos[5], pos[6]
            liquidity = pos[7]

            if liquidity == 0:
                return None

            state = self.get_current_state()
            current_tick = state["tick"]

            return PositionState(
                token_id=token_id,
                liquidity=liquidity,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                tokens_owed_0=pos[8],
                tokens_owed_1=pos[9],
                price_lower=self._tick_to_price(tick_lower),
                price_upper=self._tick_to_price(tick_upper),
                current_price=state["price"],
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

    def calculate_mint_amounts(self) -> tuple[int, int]:
        """
        Return raw token amounts to deploy for minting.

        Uses the explicitly configured deploy_token0/deploy_token1 amounts,
        capped by the actual wallet balance so it never overdrafts.

        Returns:
            (amount0, amount1) in raw token units (not decimal-adjusted)
        """
        balance0_raw = self.token0.functions.balanceOf(self.lp_account.address).call()
        balance1_raw = self.token1.functions.balanceOf(self.lp_account.address).call()

        balance0 = Decimal(balance0_raw) / Decimal(10**self.config.token0_decimals)
        balance1 = Decimal(balance1_raw) / Decimal(10**self.config.token1_decimals)

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
            int(amount0 * Decimal(10**self.config.token0_decimals)),
            int(amount1 * Decimal(10**self.config.token1_decimals)),
        )

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

        float_prices = [float(p) for p in prices]

        # EWMA mean and variance on raw prices — online, no pre-seed (matches backtester)
        lam = float(self.params.ewma_lambda)
        mean = float_prices[0]
        var = 0.0
        for x in float_prices[1:]:
            delta = x - mean
            mean = lam * mean + (1 - lam) * x
            var = lam * var + (1 - lam) * delta * delta
        std_dev = math.sqrt(var)

        # Asymmetric range: skew controls fraction of total width allocated below mean
        multiplier = float(self.params.sd_multiplier)
        skew = float(self.params.downside_skew)
        total = std_dev * multiplier * 2
        lower_price = mean_price - total * skew
        upper_price = mean_price + total * (1 - skew)

        # Ensure positive
        lower_price = max(lower_price, 0.0001)

        # Convert to ticks
        tick_lower = self._price_to_tick(Decimal(str(lower_price)))
        tick_upper = self._price_to_tick(Decimal(str(upper_price)))

        # Align to tick spacing: floor for lower, ceil for upper (only moves when misaligned)
        spacing = self.config.tick_spacing
        tick_lower = math.floor(tick_lower / spacing) * spacing
        tick_upper = math.ceil(tick_upper / spacing) * spacing

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

        # Re-align after width constraints
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

        fee_param = self.fee_param
        mint_params = (
            Web3.to_checksum_address(self.config.token0_address),
            Web3.to_checksum_address(self.config.token1_address),
            fee_param,
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

        fee_param = self.fee_param
        swap_params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee_param,
            self.trade_account.address,
            deadline,
            amount_in,
            min_amount_out,
            0,  # sqrtPriceLimitX96
        )

        tx = self.router.functions.exactInputSingle(swap_params).build_transaction(
            self._get_tx_params(self.trade_account)
        )

        return await self._send_transaction(tx, self.trade_account, capture_swap_output=True)

    async def ensure_trade_approvals(self):
        """Pre-approve router to spend both tokens from the trade account (max uint256).

        Called once at startup so swap() never needs an approval tx during execution.
        Safe to call repeatedly — _approve_if_needed skips if allowance is already sufficient.
        """
        for token_address in [self.config.token0_address, self.config.token1_address]:
            await self._approve_if_needed(
                token_address, self.config.router_address, 1,
                account=self.trade_account,
            )

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
        priority_fee = self.w3.to_wei(0.1, "gwei")

        return {
            "from": account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "maxFeePerGas": (2 * int(base_fee)) + priority_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": self.config.chain_id,
        }

    def _parse_swap_output(self, receipt) -> Optional[int]:
        """Parse the cNGN-side amount from the pool's Swap event in a tx receipt.

        Returns abs(amount0) for non-inverted pools (token0=cNGN) or abs(amount1)
        for inverted pools (token1=cNGN).  For a buy this equals cNGN received;
        for a sell this equals cNGN sent.
        """
        pool_addr = self.config.pool_address.lower()
        for log in receipt.get("logs", []):
            if (
                log["address"].lower() == pool_addr
                and len(log["topics"]) >= 1
                and log["topics"][0] == _SWAP_EVENT_TOPIC
            ):
                data = log["data"]
                if len(data) >= 64:
                    amount0 = int.from_bytes(data[:32], "big", signed=True)
                    amount1 = int.from_bytes(data[32:64], "big", signed=True)
                    return abs(amount1 if self.config.invert_price else amount0)
        return None

    async def _send_transaction(self, tx: dict, account, capture_swap_output: bool = False) -> TxResult:
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
                tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)

            # Wait for receipt
            receipt: TxReceipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=120
            )

            status = "confirmed" if receipt["status"] == 1 else "failed"
            output_raw = self._parse_swap_output(receipt) if capture_swap_output and status == "confirmed" else None

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
                output_raw=output_raw,
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
