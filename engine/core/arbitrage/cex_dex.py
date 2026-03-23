"""
CEX-DEX arbitrage simulation.

Provides two entry points:
- find_optimal_arb: fast path — runs ternary search across all directions,
  returns the best trade without generating a curve. Call on every depth update.
- compute_arb_curve: slow path — generates the full profit curve
  for UI display. Call on a slower poll cycle.

Both functions accept a cex_fee parameter so any CEX venue can be plugged in.
"""
import time
from decimal import Decimal
import structlog
from engine.api.schemas import OrderBookDepth
from engine.core.arbitrage.pool_state import get_cached_pool_state, swap_token0_for_token1, swap_token1_for_token0, Q96

logger = structlog.get_logger()

QUIDAX_FEE = Decimal("0.001")  # 0.1% taker fee

from engine.core import gas_oracle as _gas_oracle  # noqa: E402
_CEX_DEX_GAS_FN = {
    "QUIDAX_TO_UNI_BASE": _gas_oracle.gas_usd_base,
    "UNI_BASE_TO_QUIDAX": _gas_oracle.gas_usd_base,
    "QUIDAX_TO_UNI_BSC":  _gas_oracle.gas_usd_bsc,
    "UNI_BSC_TO_QUIDAX":  _gas_oracle.gas_usd_bsc,
}

# Short-circuit: evaluate at $5 before running the full ternary search.
# At $5 slippage on both legs is negligible, so profit ≈ raw spread.
# If the spread is already ≤ 0 at $5 it can only worsen with size (slippage is monotonic),
# so skipping the ~114-eval ternary search keeps the event loop unblocked on dead routes.
_SPREAD_CHECK_SIZE = Decimal("5")
_SPREAD_CHECK_MIN_PROFIT = Decimal("0")


def walk_orderbook_asks(asks: list, cngn_amount: Decimal, fee: Decimal) -> tuple[Decimal, list[dict]]:
    """
    Walk the ASK side of an order book: sell `cngn_amount` cNGN to receive USDT.
    Asks sorted lowest price first. Returns (usdt_received_after_fee, trace).
    """
    remaining = cngn_amount
    total_usdt = Decimal("0")
    traces = []
    for ask in sorted(asks, key=lambda x: x.price):
        if remaining <= 0:
            break
        max_cngn_at_level = ask.amount * ask.price
        if remaining >= max_cngn_at_level:
            total_usdt += ask.amount
            traces.append({"price": float(ask.price), "amount": float(ask.amount)})
            remaining -= max_cngn_at_level
        else:
            usdt_fraction = remaining / ask.price
            total_usdt += usdt_fraction
            traces.append({"price": float(ask.price), "amount": float(usdt_fraction)})
            remaining = Decimal("0")
    return total_usdt * (Decimal("1") - fee), traces


def walk_orderbook_bids(bids: list, usdt_amount: Decimal, fee: Decimal) -> tuple[Decimal, list[dict]]:
    """
    Walk the BID side of an order book: sell `usdt_amount` USDT to receive cNGN.
    Bids sorted highest price first. Returns (cngn_received_after_fee, trace).
    """
    remaining = usdt_amount
    total_cngn = Decimal("0")
    traces = []
    for bid in sorted(bids, key=lambda x: x.price, reverse=True):
        if remaining <= 0:
            break
        sell_amount = min(remaining, bid.amount)
        total_cngn += sell_amount * bid.price
        traces.append({"price": float(bid.price), "amount": float(sell_amount)})
        remaining -= sell_amount
    return total_cngn * (Decimal("1") - fee), traces


def _ternary_search(eval_func, low=Decimal("1"), high=Decimal("5000"), tol=Decimal("0.5")):
    """Find the profit-maximising size for a unimodal profit function."""
    while high - low > tol:
        m1 = low + (high - low) / Decimal("3")
        m2 = high - (high - low) / Decimal("3")
        f1_prof, f1_out, f1_cngn = eval_func(m1)
        f2_prof, f2_out, f2_cngn = eval_func(m2)
        if f1_prof < f2_prof:
            low = m1
        else:
            high = m2
    best_size = (low + high) / Decimal("2")
    best_prof, best_out, best_cngn = eval_func(best_size)
    return best_prof, best_size, best_cngn, best_out


def find_optimal_arb(quidax_depth: OrderBookDepth, cex_fee: Decimal = QUIDAX_FEE) -> dict | None:
    """
    Fast path: find the optimal CEX-DEX trade across all four directions.
    No curve generation. Returns optimal_arb, all_arbs, and prices.
    """
    if not quidax_depth or not quidax_depth.asks or not quidax_depth.bids:
        return None

    if _gas_oracle.gas_usd_base() is None or _gas_oracle.gas_usd_bsc() is None:
        return None

    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, _, _, _, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, _, _, _, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    if not uni_bsc_sqrt or not uni_base_sqrt or uni_bsc_fee is None or uni_base_fee is None:
        return None

    bids = sorted(quidax_depth.bids, key=lambda x: x.price, reverse=True)
    asks = sorted(quidax_depth.asks, key=lambda x: x.price)

    def cex_buy(inv):
        return walk_orderbook_bids(bids, inv, cex_fee)

    def cex_sell(amount):
        return walk_orderbook_asks(asks, amount, cex_fee)

    def eval_quidax_to_bsc(inv):
        cngn, _ = cex_buy(inv)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out = swap_token1_for_token0(cngn, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        return out - inv, out, cngn

    def eval_bsc_to_quidax(inv):
        cngn = swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out, _ = cex_sell(cngn)
        return out - inv, out, cngn

    def eval_quidax_to_base(inv):
        cngn, _ = cex_buy(inv)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out = swap_token0_for_token1(cngn, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        return out - inv, out, cngn

    def eval_base_to_quidax(inv):
        cngn = swap_token1_for_token0(inv, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out, _ = cex_sell(cngn)
        return out - inv, out, cngn

    directions = [
        ("QUIDAX_TO_UNI_BSC", eval_quidax_to_bsc),
        ("UNI_BSC_TO_QUIDAX", eval_bsc_to_quidax),
        ("QUIDAX_TO_UNI_BASE", eval_quidax_to_base),
        ("UNI_BASE_TO_QUIDAX", eval_base_to_quidax),
    ]

    best_profit = Decimal("-999999")
    best_size = Decimal("0")
    best_dir = None
    best_cngn = Decimal("0")
    usd_out_expected = Decimal("0")
    all_arbs = []

    for dir_name, eval_func in directions:
        if eval_func(_SPREAD_CHECK_SIZE)[0] <= _SPREAD_CHECK_MIN_PROFIT:
            continue
        b_prof, b_size, b_cngn, b_out = _ternary_search(eval_func)
        if b_size > 0:
            if b_prof > best_profit:
                best_profit = b_prof
                best_size = b_size
                best_dir = dir_name
                best_cngn = b_cngn
                usd_out_expected = b_out
            gas_fn = _CEX_DEX_GAS_FN.get(dir_name, _gas_oracle.gas_usd_bsc)
            gas_usd = gas_fn()
            all_arbs.append({
                "direction": dir_name,
                "optimal_size_usd": float(b_size),
                "expected_profit_usd": float(b_prof),
                "cngn_transferred": float(b_cngn),
                "expected_usd_out": float(b_out),
                "net_spread_bps": int(((b_out - b_size) / b_size) * 10000),
                "gas_usd": float(gas_usd) if gas_usd is not None else 0.0,
            })

    best_spread_bps = int(((usd_out_expected - best_size) / best_size) * 10000) if best_size > 0 else 0
    quidax_mid = Decimal("1") / ((bids[0].price + asks[0].price) / 2) if bids and asks else Decimal(0)
    uni_bsc_price_usd = float(Decimal(1) / (((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))))
    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    return {
        "timestamp": int(time.time() * 1000),
        "prices": {
            "quidax": float(quidax_mid),
            "uni-bsc": uni_bsc_price_usd,
            "uni-base": uni_base_price_usd,
        },
        "all_arbs": all_arbs,
        "optimal_arb": {
            "direction": best_dir or "NONE",
            "optimal_size_usd": float(best_size),
            "expected_profit_usd": float(best_profit),
            "cngn_transferred": float(best_cngn),
            "expected_usd_out": float(usd_out_expected),
            "net_spread_bps": best_spread_bps,
            "slippage_tolerance_bps": 10,
            "uni_bsc_fee_bps": int(uni_bsc_fee * 10000) if uni_bsc_fee else 0,
            "uni_base_fee_bps": int(uni_base_fee * 10000) if uni_base_fee else 0,
            "gas_usd": float(_CEX_DEX_GAS_FN.get(best_dir, _gas_oracle.gas_usd_bsc)()),
        },
    }


def compute_arb_curve(quidax_depth: OrderBookDepth, cex_fee: Decimal = QUIDAX_FEE) -> dict | None:
    """
    Slow path: generate the full profit curve for UI display.
    Call on a slower poll cycle; do not block the fast arb path on this.
    """
    if not quidax_depth or not quidax_depth.asks or not quidax_depth.bids:
        return None

    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, _, _, _, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, _, _, _, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    if not uni_bsc_sqrt or not uni_base_sqrt or uni_bsc_fee is None or uni_base_fee is None:
        return None

    bids = sorted(quidax_depth.bids, key=lambda x: x.price, reverse=True)
    asks = sorted(quidax_depth.asks, key=lambda x: x.price)
    slippage_multiplier = Decimal("1") - Decimal("0.0010")

    curve_cex_to_dex = []
    curve_dex_to_cex = []

    for size in range(1, 5001):
        inv = Decimal(size)

        # CEX -> DEX: buy cNGN on CEX, sell on DEX
        cngn_q, cx_tr_q = walk_orderbook_bids(bids, inv, cex_fee)
        out_bsc_q = swap_token1_for_token0(cngn_q, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        out_base_q = swap_token0_for_token1(cngn_q, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)

        curve_cex_to_dex.append({
            "size": float(size),
            "quidax_levels": cx_tr_q,
            "bsc": {
                "cngn_acquired": float(cngn_q),
                "profit": float(out_bsc_q - inv),
                "min_acceptable_usd": float(out_bsc_q * slippage_multiplier),
                "usdt_out": float(out_bsc_q),
            },
            "base": {
                "cngn_acquired": float(cngn_q),
                "profit": float(out_base_q - inv),
                "min_acceptable_usd": float(out_base_q * slippage_multiplier),
                "usdt_out": float(out_base_q),
            },
        })

        # DEX -> CEX: buy cNGN on DEX, sell on CEX
        cngn_bsc_d = swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        out_bsc_d, cx_tr_bsc_d = walk_orderbook_asks(asks, cngn_bsc_d, cex_fee)

        cngn_base_d = swap_token1_for_token0(inv, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        out_base_d, cx_tr_base_d = walk_orderbook_asks(asks, cngn_base_d, cex_fee)

        curve_dex_to_cex.append({
            "size": float(size),
            "quidax_levels": cx_tr_bsc_d if cx_tr_bsc_d else cx_tr_base_d,
            "bsc": {
                "cngn_acquired": float(cngn_bsc_d),
                "profit": float(out_bsc_d - inv),
                "min_acceptable_usd": float(out_bsc_d * slippage_multiplier),
                "usdt_out": float(out_bsc_d),
            },
            "base": {
                "cngn_acquired": float(cngn_base_d),
                "profit": float(out_base_d - inv),
                "min_acceptable_usd": float(out_base_d * slippage_multiplier),
                "usdt_out": float(out_base_d),
            },
        })

    quidax_mid = Decimal("1") / ((bids[0].price + asks[0].price) / 2) if bids and asks else Decimal(0)
    uni_bsc_price_usd = float(Decimal(1) / (((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))))
    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    return {
        "timestamp": int(time.time() * 1000),
        "prices": {
            "quidax": float(quidax_mid),
            "uni-bsc": uni_bsc_price_usd,
            "uni-base": uni_base_price_usd,
            "assetchain": 0.0,
        },
        "stats": {
            "uni_bsc_liquidity_cngn_raw": str(uni_bsc_liq),
            "uni_base_liquidity_cngn_raw": str(uni_base_liq),
            "assetchain_liquidity_cngn_raw": "0",
        },
        "curve_cex_to_dex": curve_cex_to_dex,
        "curve_dex_to_cex": curve_dex_to_cex,
    }
