"""DEX-DEX arbitrage detection using V4 pool state."""

import time
from decimal import Decimal
from typing import Any, Callable

import structlog

from engine.market.pool_state import (
    get_cached_pool_state,
    swap_token0_for_token1,
    swap_token1_for_token0,
    Q96,
)

logger = structlog.get_logger()

_ABSOLUTE_MAX_USD = Decimal("15000")
_REVERSE_SEARCH_TOL_USD = Decimal("0.01")

from engine.market import gas_oracle as _gas_oracle  # noqa: E402


def _ternary_search(
    eval_func: Callable[[Decimal], tuple[Decimal, Decimal, Decimal, str]],
    low: Decimal = Decimal("1"),
    high: Decimal = Decimal("15000"),
    tol: Decimal = Decimal("0.5"),
) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
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


def _load_dex_dex_pool_state() -> dict[str, Decimal] | None:
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, _, uni_bsc_fee = \
        get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, _, uni_base_fee = \
        get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    if (
        uni_bsc_sqrt is None or uni_bsc_liq is None or uni_bsc_fee is None
        or uni_base_sqrt is None or uni_base_liq is None or uni_base_fee is None
    ):
        return None

    return {
        "uni_bsc_sqrt": uni_bsc_sqrt,
        "uni_bsc_liq": uni_bsc_liq,
        "uni_bsc_fee": uni_bsc_fee,
        "uni_base_sqrt": uni_base_sqrt,
        "uni_base_liq": uni_base_liq,
        "uni_base_fee": uni_base_fee,
    }


def _estimate_dex_dex_amounts(
    direction: str,
    investment_usd: Decimal,
    pool_state: dict[str, Decimal],
) -> tuple[Decimal, Decimal] | None:
    if direction == "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE":
        cngn = swap_token0_for_token1(
            investment_usd,
            pool_state["uni_bsc_sqrt"],
            pool_state["uni_bsc_liq"],
            pool_state["uni_bsc_fee"],
            18,
            6,
        )
        usd_out = swap_token0_for_token1(
            cngn,
            pool_state["uni_base_sqrt"],
            pool_state["uni_base_liq"],
            pool_state["uni_base_fee"],
            6,
            6,
        )
    elif direction == "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE":
        cngn = swap_token1_for_token0(
            investment_usd,
            pool_state["uni_base_sqrt"],
            pool_state["uni_base_liq"],
            pool_state["uni_base_fee"],
            6,
            6,
        )
        usd_out = swap_token1_for_token0(
            cngn,
            pool_state["uni_bsc_sqrt"],
            pool_state["uni_bsc_liq"],
            pool_state["uni_bsc_fee"],
            18,
            6,
        )
    else:
        return None
    return cngn, usd_out


def _build_dex_dex_trade_result(
    direction: str,
    investment_usd: Decimal,
    cngn: Decimal,
    usd_out: Decimal,
) -> dict[str, Any]:
    expected_profit = usd_out - investment_usd
    net_spread_bps = int((expected_profit / investment_usd) * 10000) if investment_usd > 0 else 0
    return {
        "direction": direction,
        "optimal_size_usd": float(investment_usd),
        "expected_profit_usd": float(expected_profit),
        "cngn_transferred": float(cngn),
        "expected_usd_out": float(usd_out),
        "net_spread_bps": net_spread_bps,
    }


def estimate_dex_dex_trade(direction: str, investment_usd: Decimal) -> dict[str, Any] | None:
    """Compute executable DEX-DEX amounts for a specific routed USD size."""
    if investment_usd <= 0:
        return None

    pool_state = _load_dex_dex_pool_state()
    if pool_state is None:
        return None

    amounts = _estimate_dex_dex_amounts(direction, investment_usd, pool_state)
    if amounts is None:
        return None
    cngn, usd_out = amounts
    return _build_dex_dex_trade_result(direction, investment_usd, cngn, usd_out)


def estimate_max_dex_buy_usd_for_cngn(direction: str, wallet_cngn: Decimal) -> dict[str, Any] | None:
    """Invert the DEX-DEX path so sell-side cNGN caps the buy-side USD size exactly."""
    if wallet_cngn <= 0:
        return None

    pool_state = _load_dex_dex_pool_state()
    if pool_state is None:
        return None

    def cngn_required(investment_usd: Decimal) -> Decimal | None:
        amounts = _estimate_dex_dex_amounts(direction, investment_usd, pool_state)
        if amounts is None:
            return None
        cngn, _ = amounts
        return cngn

    max_required = cngn_required(_ABSOLUTE_MAX_USD)
    if max_required is None:
        return None
    if max_required <= wallet_cngn:
        amounts = _estimate_dex_dex_amounts(direction, _ABSOLUTE_MAX_USD, pool_state)
        if amounts is None:
            return None
        cngn, usd_out = amounts
        return _build_dex_dex_trade_result(direction, _ABSOLUTE_MAX_USD, cngn, usd_out)

    low = Decimal("0")
    high = _ABSOLUTE_MAX_USD
    while high - low > _REVERSE_SEARCH_TOL_USD:
        mid = (low + high) / Decimal("2")
        required = cngn_required(mid)
        if required is None:
            return None
        if required <= wallet_cngn:
            low = mid
        else:
            high = mid

    amounts = _estimate_dex_dex_amounts(direction, low, pool_state)
    if amounts is None:
        return None
    cngn, usd_out = amounts
    return _build_dex_dex_trade_result(direction, low, cngn, usd_out)


def find_optimal_dex_arb() -> dict[str, Any] | None:
    """
    Fast path: ternary search across both DEX-DEX directions.
    Returns the optimal arb signal dict, or None if pool state is unavailable.
    Callers should schedule seed_pool_states() on None return.
    """
    gas_base = _gas_oracle.gas_usd_base()
    gas_bsc = _gas_oracle.gas_usd_bsc()
    if gas_base is None or gas_bsc is None:
        return None

    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, uni_bsc_ts, uni_bsc_fee = \
        get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, uni_base_ts, uni_base_fee = \
        get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    if (
        uni_bsc_sqrt is None or uni_bsc_liq is None or uni_bsc_fee is None
        or uni_base_sqrt is None or uni_base_liq is None or uni_base_fee is None
    ):
        logger.error("dex_arb_blocked_missing_fees")
        return None

    uni_bsc_raw = ((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    uni_bsc_price_usd = float(Decimal(1) / uni_bsc_raw)
    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    max_usd_v1 = _ABSOLUTE_MAX_USD
    max_usd_v2 = _ABSOLUTE_MAX_USD

    def eval_bsc_to_base(inv: Decimal) -> tuple[Decimal, Decimal, Decimal, str]:
        cngn = swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        out = swap_token0_for_token1(cngn, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        return out - inv, out, cngn, "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE"

    def eval_base_to_bsc(inv: Decimal) -> tuple[Decimal, Decimal, Decimal, str]:
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
        },
        "stats": {
            "uni_bsc_liquidity_cngn_raw": str(uni_bsc_liq),
            "uni_base_liquidity_cngn_raw": str(uni_base_liq),
            "uni_bsc_ts": float(uni_bsc_ts or 0),
            "uni_base_ts": float(uni_base_ts or 0),
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
            "gas_usd": float(gas_base + gas_bsc),
        },
    }


def generate_dex_profit_curve() -> dict[str, Any]:
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

    uni_bsc_sqrt, uni_bsc_liq, _, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, _, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)
    if (
        uni_bsc_sqrt is None or uni_bsc_liq is None or uni_bsc_fee is None
        or uni_base_sqrt is None or uni_base_liq is None or uni_base_fee is None
    ):
        return {}

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
