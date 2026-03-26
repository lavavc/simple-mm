"""
Pool state cache: fetch and store on-chain state for the arb engine.
Supports V3-compatible pools (AssetChain) and V4 pools (Uniswap Base/BSC).
"""

from decimal import Decimal, getcontext
import asyncio
import math
import time
from engine.config import settings
from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
from engine.venues.dex.pool_reader_v3 import PoolReadConfig
from engine.venues.dex.shared import V4PoolReadConfig
from web3 import AsyncWeb3
import structlog

logger = structlog.get_logger()
getcontext().prec = 50

_FEE_MAX_ATTEMPTS = 3  # Total attempts for fee() RPC call
_FEE_BACKOFF_BASE = 1  # seconds — doubles each retry (1s, 2s)


async def _fetch_fee_with_retry(w3: AsyncWeb3, pool: str, pool_address: str) -> Decimal | None:
    """Attempt to fetch pool fee() on-chain, retrying with exponential backoff.

    Returns the fee as a fraction (e.g. 0.0001 for 0.01%) or None if all
    attempts fail. Using None lets callers treat a missing fee as a hard
    block rather than silently falling back to an assumed value.
    """
    last_err: Exception | None = None
    for attempt in range(_FEE_MAX_ATTEMPTS):
        try:
            fee_raw = await w3.eth.call({"to": pool, "data": FEE_SELECTOR})
            return Decimal(int.from_bytes(fee_raw[:32], "big")) / Decimal(1000000)
        except Exception as e:
            last_err = e
            if attempt < _FEE_MAX_ATTEMPTS - 1:
                wait = _FEE_BACKOFF_BASE * (2 ** attempt)  # 1s, 2s
                logger.warning(
                    "pool_fee_fetch_retry",
                    pool=pool_address,
                    attempt=attempt + 1,
                    retry_in_seconds=wait,
                    error=str(e),
                )
                await asyncio.sleep(wait)

    logger.error(
        "pool_fee_fetch_failed",
        pool=pool_address,
        attempts=_FEE_MAX_ATTEMPTS,
        error=str(last_err),
        note="fee stored as None — arb execution will be blocked until resolved",
    )
    return None

SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
FEE_SELECTOR = "0xddca3f43"
Q96 = Decimal(2 ** 96)

STATE_VIEW_ABI = [
    {"inputs": [{"name": "poolId", "type": "bytes32"}],
     "name": "getSlot0", "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
         {"name": "protocolFee", "type": "uint24"}, {"name": "lpFee", "type": "uint24"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}],
     "name": "getLiquidity", "outputs": [{"name": "liquidity", "type": "uint128"}],
     "stateMutability": "view", "type": "function"},
]

# Cache to prevent making duplicate RPC calls for liquidity if the tick hasn't changed.
# Structure: { pool_address: {"tick": int, "liquidity": Decimal, "sqrt_p": Decimal, "timestamp": float} }
_POOL_CACHE: dict[str, dict] = {}

def get_cached_pool_state(pool_address: str) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None, float | None, Decimal | None]:
    """Retrieve the latest known state from memory without network calls."""
    data = _POOL_CACHE.get(pool_address)
    if data:
        return data["sqrt_p"], data["liquidity"], data.get("balance0"), data.get("balance1"), data.get("timestamp"), data.get("fee")
    return None, None, None, None, None, None

def set_pool_tick_range(pool_id: str, tick_lower: int, tick_upper: int) -> None:
    """Write position tick range into pool cache for balance computation.
    Called by LP adapter with actual or strategy-estimated ticks."""
    cached = _POOL_CACHE.get(pool_id)
    if cached is not None:
        cached["tick_lower"] = tick_lower
        cached["tick_upper"] = tick_upper


async def update_single_pool_state(config: PoolReadConfig, rpc_url_override: str = None) -> bool:
    """Fetches the state for a single V3-compatible pool and updates the cache. Returns True if successful."""
    rpc_url = rpc_url_override or config.rpc_url
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    pool = w3.to_checksum_address(config.pool_address)

    try:
        slot0_raw = await w3.eth.call({"to": pool, "data": SLOT0_SELECTOR})
        sqrt_price_x96 = Decimal(int.from_bytes(slot0_raw[:32], "big"))

        tick_bytes = slot0_raw[32:64][-3:]
        tick = int.from_bytes(tick_bytes, "big", signed=True)

        cached_data = _POOL_CACHE.get(config.pool_address)

        # Always fetch balanceOf for accurate depth, but cache liquidity if tick is identical
        t0_call = "0x70a08231" + pool[2:].zfill(64)
        t1_call = "0x70a08231" + pool[2:].zfill(64)

        balance0_raw = await w3.eth.call({"to": w3.to_checksum_address(config.token0_address), "data": t0_call})
        balance1_raw = await w3.eth.call({"to": w3.to_checksum_address(config.token1_address), "data": t1_call})

        balance0 = Decimal(int.from_bytes(balance0_raw[:32], "big")) / Decimal(10**config.token0_decimals)
        balance1 = Decimal(int.from_bytes(balance1_raw[:32], "big")) / Decimal(10**config.token1_decimals)

        if cached_data and cached_data["tick"] == tick:
            liquidity = cached_data["liquidity"]
            cached_fee = cached_data.get("fee")
            if cached_fee is not None:
                # Tick unchanged and fee is known — use the cache as-is.
                fee = cached_fee
                logger.debug("pool_cache_hit_liquidity", pool=config.pool_address, tick=tick)
            else:
                # Fee was None from a previous failed fetch. Retry now so a
                # transient RPC error doesn't leave us blocked indefinitely
                # just because the tick hasn't changed.
                logger.info(
                    "pool_fee_cache_null_retrying",
                    pool=config.pool_address,
                    tick=tick,
                )
                fee = await _fetch_fee_with_retry(w3, pool, config.pool_address)
        else:
            liquidity_raw = await w3.eth.call({"to": pool, "data": LIQUIDITY_SELECTOR})
            liquidity = Decimal(int.from_bytes(liquidity_raw[:32], "big"))
            fee = await _fetch_fee_with_retry(w3, pool, config.pool_address)
            logger.debug("pool_cache_miss_fetching_liquidity", pool=config.pool_address, tick=tick)

        _POOL_CACHE[config.pool_address] = {
            "tick": tick,
            "liquidity": liquidity,
            "fee": fee,
            "sqrt_p": sqrt_price_x96,
            "balance0": balance0,
            "balance1": balance1,
            "timestamp": time.time()
        }

        if fee is None:
            logger.warning(
                "pool_state_incomplete",
                pool=config.pool_address,
                reason="fee fetch failed — state cached but marked incomplete",
            )
            return False

        return True
    except Exception as e:
        logger.error("pool_state_fetch_error", error=str(e), rpc=rpc_url, pool=config.pool_address)
        return False


async def update_single_v4_pool_state(config: V4PoolReadConfig) -> bool:
    """Fetches the state for a single V4 pool via StateView and updates the cache."""
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(config.rpc_url))
    pool_id_bytes = bytes.fromhex(config.pool_address[2:])

    try:
        state_view = w3.eth.contract(
            address=w3.to_checksum_address(config.state_view),
            abi=STATE_VIEW_ABI,
        )

        # StateView stores poolManager as immutable — only poolId is passed
        slot0 = await state_view.functions.getSlot0(pool_id_bytes).call()
        sqrt_price_x96 = Decimal(slot0[0])
        tick = slot0[1]
        fee = Decimal(slot0[3]) / Decimal(1000000)  # lpFee uint24 → fraction

        cached_data = _POOL_CACHE.get(config.pool_address)

        if cached_data and cached_data["tick"] == tick:
            liquidity = cached_data["liquidity"]
            logger.debug("v4_pool_cache_hit_liquidity", pool=config.pool_address, tick=tick)
        else:
            liquidity_raw = await state_view.functions.getLiquidity(pool_id_bytes).call()
            liquidity = Decimal(liquidity_raw)
            logger.debug("v4_pool_cache_miss_fetching_liquidity", pool=config.pool_address, tick=tick)  # noqa: keep v4_ prefix for V4-specific path

        # Compute token amounts from tick math using the stored tick range.
        # tick_lower/tick_upper are written by the LP adapter (actual or estimated from strategy params).
        tick_lower = (cached_data or {}).get("tick_lower")
        tick_upper = (cached_data or {}).get("tick_upper")

        balance0, balance1 = None, None
        if tick_lower is not None and tick_upper is not None and liquidity > 0:
            sqrt_lower = Decimal(str(math.exp(tick_lower * math.log(1.0001) / 2))) * Q96
            sqrt_upper = Decimal(str(math.exp(tick_upper * math.log(1.0001) / 2))) * Q96
            sqrt_p_c = max(sqrt_lower, min(sqrt_upper, sqrt_price_x96))
            balance0 = liquidity * Q96 * (sqrt_upper - sqrt_p_c) / (sqrt_p_c * sqrt_upper) / Decimal(10 ** config.token0_decimals)
            balance1 = liquidity * (sqrt_p_c - sqrt_lower) / Q96 / Decimal(10 ** config.token1_decimals)

        _POOL_CACHE[config.pool_address] = {
            "tick": tick,
            "liquidity": liquidity,
            "fee": fee,
            "sqrt_p": sqrt_price_x96,
            "balance0": balance0,
            "balance1": balance1,
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "timestamp": time.time(),
        }
        return True
    except Exception as e:
        logger.error("v4_pool_state_fetch_error", error=str(e), rpc=config.rpc_url, pool=config.pool_address)
        return False


def update_pool_state_from_event(pool_id: str, sqrt_p: int, liquidity: int, tick: int, fee: int):
    """Update cache from a V4 Swap event — zero RPC calls."""
    cached = _POOL_CACHE.get(pool_id, {})
    _POOL_CACHE[pool_id] = {
        "tick": tick,
        "liquidity": Decimal(liquidity),
        "fee": Decimal(fee) / Decimal(1000000),
        "sqrt_p": Decimal(sqrt_p),
        "balance0": cached.get("balance0"),
        "balance1": cached.get("balance1"),
        "tick_lower": cached.get("tick_lower"),
        "tick_upper": cached.get("tick_upper"),
        "timestamp": time.time(),
    }


async def seed_pool_states():
    """Initializes the memory state manager by fetching all pools once."""
    logger.info("seeding_initial_pool_states")
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
    await asyncio.gather(
        update_single_v4_pool_state(UNISWAP_BSC_POOL_READ_CONFIG),
        update_single_v4_pool_state(UNISWAP_BASE_POOL_READ_CONFIG),
        update_single_pool_state(ASSETCHAIN_POOL_READ_CONFIG, settings.assetchain_rpc_url),
    )

# ========== CONCENTRATED LIQUIDITY SWAP MATH ==========
# Identical formula for both V3 and V4 pools (same CFMM invariant).
# Single-tick approximation: uses cached sqrtPrice and liquidity as constants.

def swap_token0_for_token1(amount_token0_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal, t0_decimals: int, t1_decimals: int) -> Decimal:
    """
    Single-tick swap: Token0 -> Token1.
    Formula: sqrtP_new = (L * sqrtP) / (L + amount_in * sqrtP)
    """
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)

    amount_in_after_fee = amount_token0_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**t0_decimals)

    numerator = liquidity * sqrt_p
    denominator = (liquidity * Q96) + (amount_in_raw * sqrt_p)
    sqrt_p_new = (numerator * Q96) / denominator
    amount_out_raw = (liquidity * (sqrt_p - sqrt_p_new)) / Q96

    return amount_out_raw / Decimal(10**t1_decimals)

def swap_token1_for_token0(amount_token1_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal, t0_decimals: int, t1_decimals: int) -> Decimal:
    """
    Single-tick swap: Token1 -> Token0.
    Formula: sqrtP_new = sqrtP + (amount_in * Q96) / L
    """
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)

    amount_in_after_fee = amount_token1_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**t1_decimals)

    sqrt_p_new = sqrt_p + (amount_in_raw * Q96) / liquidity
    amount_out_raw = (liquidity * Q96 * (sqrt_p_new - sqrt_p)) / (sqrt_p * sqrt_p_new)

    return amount_out_raw / Decimal(10**t0_decimals)
