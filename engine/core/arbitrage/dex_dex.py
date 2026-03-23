"""DEX-DEX arbitrage detection using V4 pool state."""

import time
from decimal import Decimal

import structlog

from engine.core.arbitrage.pool_state import (
    get_cached_pool_state,
    swap_token0_for_token1,
    swap_token1_for_token0,
    Q96,
)

logger = structlog.get_logger()

_MIN_POOL_STABLE_USD = Decimal("500")
_ABSOLUTE_MAX_USD = Decimal("15000")

from engine.core import gas_oracle as _gas_oracle  # noqa: E402


def _ternary_search(eval_func, low=Decimal("1"), high=Decimal("15000"), tol=Decimal("0.5")):
    """Find the profit-maximising size for a unimodal profit function."""
    while high - low > tol:
        m1 = low + (high - low) / Decimal("3")
        m2 = high - (high - low) / Decimal("3")
        f1_prof, f1_out, f1_cngn, _ = eval_func(m1)
        f2_prof, f2_out, f2_cngn, _ = eval_func(m2)
        if f1_prof < f2_prof:
            low = m1
        else:
            high = m2
    mid = (low + high) / Decimal("2")
    best_prof, best_out, best_cngn, best_dir = eval_func(mid)
    return best_prof, mid, best_cngn, best_out, best_dir


def find_optimal_dex_arb() -> dict | None:
    """
    Fast path: ternary search across both DEX-DEX directions.
    Returns the optimal arb signal dict, or None if pool state is unavailable.
    Callers should schedule seed_pool_states() on None return.
    """
    if _gas_oracle.gas_usd_base() is None or _gas_oracle.gas_usd_bsc() is None:
        return None

    from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, uni_bsc_b0, uni_bsc_b1, uni_bsc_ts, uni_bsc_fee = \
        get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, uni_base_b0, uni_base_b1, uni_base_ts, uni_base_fee = \
        get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)
    asset_sqrt, asset_liq, asset_b0, asset_b1, asset_ts, _ = \
        get_cached_pool_state(ASSETCHAIN_POOL_READ_CONFIG.pool_address)

    if uni_bsc_fee is None or uni_base_fee is None:
        logger.error("dex_arb_blocked_missing_fees")
        return None

    if not uni_bsc_sqrt or not uni_base_sqrt:
        logger.warning("dex_arb_cache_miss")
        return None

    uni_bsc_stable, uni_bsc_cngn = uni_bsc_b0, uni_bsc_b1  # token0=USDT, token1=cNGN
    uni_base_cngn, uni_base_stable = uni_base_b0, uni_base_b1  # token0=cNGN, token1=USDC

    if (uni_bsc_stable is None or uni_bsc_stable < _MIN_POOL_STABLE_USD or
            uni_base_stable is None or uni_base_stable < _MIN_POOL_STABLE_USD):
        logger.warning("dex_arb_blocked_thin_pools")
        return None

    uni_bsc_raw = ((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    uni_bsc_price_usd = float(Decimal(1) / uni_bsc_raw)
    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))
    asset_price_usd = (
        float(Decimal(1) / (((asset_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))))
        if asset_sqrt else None
    )

    max_usd_v1 = min(uni_bsc_cngn * Decimal(str(uni_bsc_price_usd)), uni_base_stable, _ABSOLUTE_MAX_USD)
    max_usd_v2 = min(uni_base_cngn * Decimal(str(uni_base_price_usd)), uni_bsc_stable, _ABSOLUTE_MAX_USD)

    def eval_bsc_to_base(inv: Decimal):
        cngn = swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        out = swap_token0_for_token1(cngn, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        return out - inv, out, cngn, "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE"

    def eval_base_to_bsc(inv: Decimal):
        cngn = swap_token1_for_token0(inv, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        out = swap_token1_for_token0(cngn, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        return out - inv, out, cngn, "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE"

    best_v1 = _ternary_search(eval_bsc_to_base, high=max_usd_v1)
    best_v2 = _ternary_search(eval_base_to_bsc, high=max_usd_v2)
    best_profit, best_size, best_cngn, usd_out, best_dir = (
        best_v1 if best_v1[0] >= best_v2[0] else best_v2
    )

    best_spread_bps = int(((usd_out - best_size) / best_size) * 10000) if best_size > 0 else 0

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
            "assetchain_stable": float(asset_b0 or 0),
            "assetchain_cngn": float(asset_b1 or 0),
            "uni_bsc_ts": float(uni_bsc_ts or 0),
            "uni_base_ts": float(uni_base_ts or 0),
            "assetchain_ts": float(asset_ts or 0),
        },
        "optimal_arb": {
            "direction": best_dir,
            "optimal_size_usd": float(best_size),
            "expected_profit_usd": float(best_profit),
            "cngn_transferred": float(best_cngn),
            "expected_usd_out": float(usd_out),
            "net_spread_bps": best_spread_bps,
            "slippage_tolerance_bps": 10,
            "uni_bsc_fee_bps": int(uni_bsc_fee * 10000),
            "uni_base_fee_bps": int(uni_base_fee * 10000),
            "assetchain_fee_bps": 30,
            "estimated_gas_usd": float(_gas_oracle.gas_usd_base() + _gas_oracle.gas_usd_bsc()),
        },
    }


def generate_dex_profit_curve() -> dict:
    """
    Slow path: full DEX-DEX profit curve for UI display.
    Calls find_optimal_dex_arb() for pool validation and to determine dynamic curve range
    (always includes the profit peak with 50% headroom).
    Intended to run in run_in_executor — pure CPU, no async calls.
    """
    fast = find_optimal_dex_arb()
    if not fast:
        return {}

    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, _, _, _, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, _, _, _, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    optimal_size = fast["optimal_arb"]["optimal_size_usd"]
    curve_max = max(5000, int(optimal_size * 1.5))

    slippage_multiplier = Decimal("1") - Decimal("0.0010")
    curve = []
    for size in range(1, curve_max + 1):
        investment_usd = Decimal(str(size))

        cngn_acquired_base = swap_token1_for_token0(investment_usd, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        usd_returned_bsc = swap_token1_for_token0(cngn_acquired_base, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)

        cngn_acquired_bsc = swap_token0_for_token1(investment_usd, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        usd_returned_base = swap_token0_for_token1(cngn_acquired_bsc, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)

        curve.append({
            "size": size,
            "base_to_bsc": {
                "cngn_acquired": float(cngn_acquired_base),
                "profit": float(usd_returned_bsc - investment_usd),
                "profit_after_slippage": float(usd_returned_bsc * slippage_multiplier - investment_usd),
                "min_acceptable_usd": float(usd_returned_bsc * slippage_multiplier),
            },
            "bsc_to_base": {
                "cngn_acquired": float(cngn_acquired_bsc),
                "profit": float(usd_returned_base - investment_usd),
                "profit_after_slippage": float(usd_returned_base * slippage_multiplier - investment_usd),
                "min_acceptable_usd": float(usd_returned_base * slippage_multiplier),
            },
        })

    return {**fast, "curve": curve}
