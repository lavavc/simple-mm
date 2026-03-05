"""
Exportable simulator to generate V3 profit curves for the frontend.
"""

from decimal import Decimal, getcontext
import asyncio
import time
from engine.config import settings
from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
from engine.venues.dex.base import PoolReadConfig, V4PoolReadConfig
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
                    "v3_pool_fee_fetch_retry",
                    pool=pool_address,
                    attempt=attempt + 1,
                    retry_in_seconds=wait,
                    error=str(e),
                )
                await asyncio.sleep(wait)

    logger.error(
        "v3_pool_fee_fetch_failed",
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

async def update_single_pool_state(config: PoolReadConfig, rpc_url_override: str = None) -> bool:
    """Fetches the state for a single V3 pool and updates the cache. Returns True if successful."""
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
                logger.debug("v3_pool_cache_hit_liquidity", pool=config.pool_address, tick=tick)
            else:
                # Fee was None from a previous failed fetch. Retry now so a
                # transient RPC error doesn't leave us blocked indefinitely
                # just because the tick hasn't changed.
                logger.info(
                    "v3_pool_fee_cache_null_retrying",
                    pool=config.pool_address,
                    tick=tick,
                )
                fee = await _fetch_fee_with_retry(w3, pool, config.pool_address)
        else:
            liquidity_raw = await w3.eth.call({"to": pool, "data": LIQUIDITY_SELECTOR})
            liquidity = Decimal(int.from_bytes(liquidity_raw[:32], "big"))
            fee = await _fetch_fee_with_retry(w3, pool, config.pool_address)
            logger.debug("v3_pool_cache_miss_fetching_liquidity", pool=config.pool_address, tick=tick)

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
                "v3_pool_state_incomplete",
                pool=config.pool_address,
                reason="fee fetch failed — state cached but marked incomplete",
            )
            return False

        return True
    except Exception as e:
        logger.error("v3_pool_state_fetch_error", error=str(e), rpc=rpc_url, pool=config.pool_address)
        return False


async def update_single_v4_pool_state(config: V4PoolReadConfig) -> bool:
    """Fetches the state for a single V4 pool via StateView and updates the cache."""
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(config.rpc_url))
    pool_manager = w3.to_checksum_address(config.pool_manager)
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
            logger.debug("v4_pool_cache_miss_fetching_liquidity", pool=config.pool_address, tick=tick)

        # Balances held by the PoolManager (V4 holds all tokens centrally)
        t0_call = "0x70a08231" + pool_manager[2:].zfill(64)
        t1_call = "0x70a08231" + pool_manager[2:].zfill(64)
        balance0_raw = await w3.eth.call({"to": w3.to_checksum_address(config.token0_address), "data": t0_call})
        balance1_raw = await w3.eth.call({"to": w3.to_checksum_address(config.token1_address), "data": t1_call})
        balance0 = Decimal(int.from_bytes(balance0_raw[:32], "big")) / Decimal(10**config.token0_decimals)
        balance1 = Decimal(int.from_bytes(balance1_raw[:32], "big")) / Decimal(10**config.token1_decimals)

        _POOL_CACHE[config.pool_address] = {
            "tick": tick,
            "liquidity": liquidity,
            "fee": fee,
            "sqrt_p": sqrt_price_x96,
            "balance0": balance0,
            "balance1": balance1,
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
        "timestamp": time.time(),
    }


async def seed_pool_states():
    """Initializes the memory state manager by fetching all pools once."""
    logger.info("seeding_initial_pool_states")
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
    await update_single_v4_pool_state(UNISWAP_BSC_POOL_READ_CONFIG)
    await update_single_v4_pool_state(UNISWAP_BASE_POOL_READ_CONFIG)
    await update_single_pool_state(ASSETCHAIN_POOL_READ_CONFIG, settings.assetchain_rpc_url)

# ========== V3 EXACT MATH FUNCTIONS ==========

def v3_swap_token0_for_token1(amount_token0_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal, t0_decimals: int, t1_decimals: int) -> Decimal:
    """
    Exact V3 single-tick swap math for Token0 -> Token1.
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

def v3_swap_token1_for_token0(amount_token1_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal, t0_decimals: int, t1_decimals: int) -> Decimal:
    """
    Exact V3 single-tick swap math for Token1 -> Token0.
    Formula: sqrtP_new = sqrtP + (amount_in * Q96) / L
    amount_out = L * (sqrtP_new - sqrtP) / (sqrtP * sqrtP_new)
    """
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)

    amount_in_after_fee = amount_token1_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**t1_decimals)

    sqrt_p_new = sqrt_p + (amount_in_raw * Q96) / liquidity
    amount_out_raw = (liquidity * Q96 * (sqrt_p_new - sqrt_p)) / (sqrt_p * sqrt_p_new)

    return amount_out_raw / Decimal(10**t0_decimals)

async def generate_v3_profit_curve() -> dict:
    """Generates the side-by-side exact V3 curve data over a set of investment sizes from CACHED memory."""
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, uni_bsc_b0, uni_bsc_b1, uni_bsc_ts, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, uni_base_b0, uni_base_b1, uni_base_ts, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)
    asset_sqrt, asset_liq, asset_b0, asset_b1, asset_ts, asset_fee = get_cached_pool_state(ASSETCHAIN_POOL_READ_CONFIG.pool_address)

    # Hard-block on execution venue fees (uni-bsc + uni-base).
    missing_execution_fees = [
        name for name, fee in [("uni-bsc", uni_bsc_fee), ("uni-base", uni_base_fee)]
        if fee is None
    ]
    if missing_execution_fees:
        logger.error(
            "v3_profit_curve_blocked_missing_fees",
            pools=missing_execution_fees,
            note="arb curve generation aborted — re-seed will be attempted",
        )
        import asyncio
        asyncio.create_task(seed_pool_states())
        return {}

    if not uni_bsc_sqrt or not uni_base_sqrt:
        logger.warning("v3_profit_curve_cache_miss_aborting_calc")
        import asyncio
        asyncio.create_task(seed_pool_states())
        return {}

    # Prices (USD per cNGN)
    uni_bsc_raw = ((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    uni_bsc_price_usd = float(Decimal(1) / uni_bsc_raw)

    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    asset_price_usd = float(Decimal(1) / (((asset_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6)))) if asset_sqrt else None

    uni_bsc_stable, uni_bsc_cngn = uni_bsc_b0, uni_bsc_b1  # token0=USDT, token1=cNGN
    uni_base_cngn, uni_base_stable = uni_base_b0, uni_base_b1  # token0=cNGN, token1=USDC
    asset_stable, asset_cngn = asset_b0, asset_b1

    test_sizes = [1, 10, 50, 100, 500, 1000, 2500, 5000, 10000, 50000, 100000]

    curve = []
    for size in test_sizes:
        investment_usd = Decimal(str(size))

        cngn_uni_bsc = v3_swap_token0_for_token1(investment_usd, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        cngn_uni_base = v3_swap_token1_for_token0(investment_usd, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        cngn_assetchain = (
            v3_swap_token0_for_token1(investment_usd, asset_sqrt, asset_liq, asset_fee, 18, 6)
            if asset_fee is not None else None
        )
        cngn_assetchain_no_fee = (
            v3_swap_token0_for_token1(investment_usd, asset_sqrt, asset_liq, Decimal(0), 18, 6)
            if asset_fee is not None else None
        )
        usd_returned = v3_swap_token0_for_token1(cngn_uni_bsc, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)

        cngn_uni_bsc_no_fee = v3_swap_token0_for_token1(investment_usd, uni_bsc_sqrt, uni_bsc_liq, Decimal(0), 18, 6)
        cngn_uni_base_no_fee = v3_swap_token1_for_token0(investment_usd, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)
        usd_returned_no_fee = v3_swap_token0_for_token1(cngn_uni_bsc_no_fee, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)

        slippage_tolerance = Decimal("0.0010")
        min_usd_acceptable = usd_returned * (Decimal("1") - slippage_tolerance)

        curve.append({
            "size": size,
            "cngn_uni_bsc": float(cngn_uni_bsc),
            "cngn_uni_base": float(cngn_uni_base),
            "cngn_assetchain": float(cngn_assetchain) if cngn_assetchain is not None else None,
            "profit": float(usd_returned - investment_usd),
            "profit_no_fee": float(usd_returned_no_fee - investment_usd),
            "cngn_uni_bsc_no_fee": float(cngn_uni_bsc_no_fee),
            "cngn_uni_base_no_fee": float(cngn_uni_base_no_fee),
            "cngn_assetchain_no_fee": float(cngn_assetchain_no_fee) if cngn_assetchain_no_fee is not None else None,
            "min_acceptable_usd": float(min_usd_acceptable)
        })

    max_usd = 15000
    best_profit = Decimal("-999999")
    best_size = Decimal("0")
    best_dir = None
    best_cngn = Decimal("0")
    usd_out_expected = Decimal("0")
    best_spread_bps = 0

    step = 10
    # DELTA BALANCING VECTOR 1: Buy on uni-bsc, sell identical cNGN from uni-base inventory
    for size in range(10, max_usd + step, step):
        usd_in_bsc = Decimal(size)
        cngn_acquired_bsc = v3_swap_token0_for_token1(usd_in_bsc, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        usd_out_base = v3_swap_token0_for_token1(cngn_acquired_bsc, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        if usd_out_base - usd_in_bsc > best_profit:
            best_profit = usd_out_base - usd_in_bsc
            best_size = usd_in_bsc
            best_dir = "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE"
            best_cngn = cngn_acquired_bsc
            usd_out_expected = usd_out_base

    # DELTA BALANCING VECTOR 2: Buy on uni-base, sell identical cNGN from uni-bsc inventory
    for size in range(10, max_usd + step, step):
        usd_in_base = Decimal(size)
        cngn_acquired_base = v3_swap_token1_for_token0(usd_in_base, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        usd_out_bsc = v3_swap_token1_for_token0(cngn_acquired_base, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        if usd_out_bsc - usd_in_base > best_profit:
            best_profit = usd_out_bsc - usd_in_base
            best_size = usd_in_base
            best_dir = "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE"
            best_cngn = cngn_acquired_base
            usd_out_expected = usd_out_bsc

    if best_size > 0:
        best_spread_bps = int(((usd_out_expected - best_size) / best_size) * 10000)

    uni_bsc_fee_bps = int(uni_bsc_fee * 10000) if uni_bsc_fee else 0
    uni_base_fee_bps = int(uni_base_fee * 10000) if uni_base_fee else 0

    return {
        "timestamp": int(time.time() * 1000),
        "prices": {
            "uni-bsc": uni_bsc_price_usd,
            "uni-base": uni_base_price_usd,
            "assetchain": asset_price_usd,
        },
        "stats": {
            "uni_bsc_liquidity_cngn_raw": str(uni_bsc_liq),
            "uni_base_liquidity_cngn_raw": str(uni_base_liq),
            "assetchain_liquidity_cngn_raw": str(asset_liq),
            "uni_bsc_stable": float(uni_bsc_stable or 0),
            "uni_bsc_cngn": float(uni_bsc_cngn or 0),
            "uni_base_stable": float(uni_base_stable or 0),
            "uni_base_cngn": float(uni_base_cngn or 0),
            "assetchain_stable": float(asset_stable or 0),
            "assetchain_cngn": float(asset_cngn or 0),
            "uni_bsc_ts": float(uni_bsc_ts or 0),
            "uni_base_ts": float(uni_base_ts or 0),
            "assetchain_ts": float(asset_ts or 0),
        },
        "curve": curve,
        "optimal_arb": {
            "direction": best_dir,
            "optimal_size_usd": float(best_size),
            "expected_profit_usd": float(best_profit),
            "cngn_transferred": float(best_cngn),
            "expected_usd_out": float(usd_out_expected),
            "net_spread_bps": best_spread_bps,
            "slippage_tolerance_bps": 10,
            "uni_bsc_fee_bps": uni_bsc_fee_bps,
            "uni_base_fee_bps": uni_base_fee_bps,
            "assetchain_fee_bps": 30,
            "estimated_gas_usd": 0.07
        }
    }
