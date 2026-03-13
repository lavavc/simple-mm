import time
from decimal import Decimal
import structlog
from engine.api.schemas import OrderBookDepth
from engine.core.arbitrage.pool_state import get_cached_pool_state, v3_swap_token0_for_token1, v3_swap_token1_for_token0, Q96

logger = structlog.get_logger()


QUIDAX_FEE = Decimal("0.001")  # 0.1% taker fee


def walk_quidax_asks(asks: list, cngn_to_spend: Decimal) -> tuple[Decimal, list[dict]]:
    """
    Walk the ASK side of the Quidax order book to calculate how much USDT
    you receive for selling `cngn_to_spend` cNGN. Applies the 0.1% taker fee.
    Asks are sorted lowest price first (best ask first).
    """
    remaining_cngn = cngn_to_spend
    total_usdt = Decimal("0")
    traces = []
    for ask in sorted(asks, key=lambda x: x.price):
        if remaining_cngn <= 0:
            break
        max_cngn_at_level = ask.amount * ask.price
        if remaining_cngn >= max_cngn_at_level:
            total_usdt += ask.amount
            traces.append({"price": float(ask.price), "amount": float(ask.amount)})
            remaining_cngn -= max_cngn_at_level
        else:
            usdt_fraction = remaining_cngn / ask.price
            total_usdt += usdt_fraction
            traces.append({"price": float(ask.price), "amount": float(usdt_fraction)})
            remaining_cngn = Decimal("0")
    return total_usdt * (Decimal("1") - QUIDAX_FEE), traces


def compute_quidax_liquidation(quidax_depth, current_balances: list) -> dict | None:
    """
    Compute Quidax-only liquidation values (with and without fee) from the live
    order book and wallet balances. Does NOT require DEX pool state to be seeded.
    Returns a partial liquidation_valuation dict with just the Quidax fields.
    """
    if not quidax_depth or not quidax_depth.asks or not quidax_depth.bids or not current_balances:
        return None

    bids = sorted(quidax_depth.bids, key=lambda x: x.price, reverse=True)
    asks = sorted(quidax_depth.asks, key=lambda x: x.price)
    quidax_mid = Decimal("1") / ((bids[0].price + asks[0].price) / 2) if bids and asks else Decimal(0)

    quidax_bal = next((b for b in current_balances if b.role == "quidax-exchange"), None)
    if not quidax_bal:
        return None

    q_cngn = quidax_bal.token_balances.get("cNGN", Decimal(0))
    q_usdt = quidax_bal.token_balances.get("USDT", Decimal(0))

    liq_with_fee = Decimal(0)
    liq_no_fee = Decimal(0)
    if q_cngn > 0:
        liq_with_fee, _ = walk_quidax_asks(asks, q_cngn)
        # No-fee: remove the 0.1% taker deduction
        liq_no_fee = liq_with_fee / (Decimal("1") - QUIDAX_FEE)

    return {
        "quidax_cngn_usd": float(liq_with_fee),
        "quidax_cngn_usd_no_fee": float(liq_no_fee),
        "quidax_usdt": float(q_usdt),
        "quidax_mid": float(quidax_mid),
    }


def simulate_cex_dex_arbitrage(quidax_depth: OrderBookDepth, current_balances: list = None) -> dict | None:
    """
    Simulates CEX to DEX arbitrage over varying trade sizes to generate a profit curve.
    
    Quidax pair: usdtcngn (Base=USDT, Quote=cNGN).
    Price = cNGN per 1 USDT.
    Amount = USDT volume.
    """
    if not quidax_depth or not quidax_depth.asks or not quidax_depth.bids:
        return None

    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    uni_bsc_sqrt, uni_bsc_liq, _, _, _, uni_bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    uni_base_sqrt, uni_base_liq, _, _, _, uni_base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)
    
    if not uni_bsc_sqrt or not uni_base_sqrt or uni_bsc_fee is None or uni_base_fee is None:
        return None



    # Sort books
    bids = sorted(quidax_depth.bids, key=lambda x: x.price, reverse=True) # Highest first
    asks = sorted(quidax_depth.asks, key=lambda x: x.price) # Lowest first

    def calculate_cngn_from_quidax_sell_usdt(investment_usdt: Decimal) -> tuple[Decimal, list[dict]]:
        """Walk the BID book: We SELL our USDT to the Bids to get cNGN."""
        remaining_usdt = investment_usdt
        total_cngn = Decimal("0")
        traces = []
        for bid in bids:
            if remaining_usdt <= 0: break
            sell_amount = min(remaining_usdt, bid.amount)
            total_cngn += sell_amount * bid.price
            traces.append({"price": float(bid.price), "amount": float(sell_amount)})
            remaining_usdt -= sell_amount
        return total_cngn * (Decimal("1") - QUIDAX_FEE), traces


    # Calculate current Quidax mid price for reference in UI (cNGN/USD roughly via 1/mid)
    quidax_mid = Decimal("1") / ((bids[0].price + asks[0].price) / 2) if bids and asks else Decimal(0)
    
    uni_bsc_price_usd = float(Decimal(1) / (((uni_bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))))
    uni_base_price_usd = float(((uni_base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    test_sizes = list(range(10, 1500, 10))

    best_profit = Decimal("-999999")
    best_size = Decimal("0")
    best_dir = None
    best_cngn = Decimal("0")
    usd_out_expected = Decimal("0")
    
    all_arbs = []

    # Helper to check and append local best to global and all_arbs
    def process_local_best(dir_name, b_prof, b_size, b_cngn, b_out):
        nonlocal best_profit, best_size, best_dir, best_cngn, usd_out_expected
        if b_size > 0:
            if b_prof > best_profit:
                best_profit = b_prof
                best_size = b_size
                best_dir = dir_name
                best_cngn = b_cngn
                usd_out_expected = b_out
            all_arbs.append({
                "direction": dir_name,
                "optimal_size_usd": float(b_size),
                "expected_profit_usd": float(b_prof),
                "cngn_transferred": float(b_cngn),
                "expected_usd_out": float(b_out),
                "net_spread_bps": int(((b_out - b_size) / b_size) * 10000)
            })
    def ternary_search(eval_func, low=Decimal("1"), high=Decimal("5000"), tol=Decimal("0.5")):
        # Evaluate up front to check if it's even profitable
        if eval_func(Decimal("5"))[0] <= Decimal("-1"):
            return Decimal("-999999"), Decimal("0"), Decimal("0"), Decimal("0")
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

    # 1. Quidax -> UNI BSC (Sell USDT on Quidax, Sell cNGN on BSC)
    def eval_quidax_to_bsc(inv: Decimal):
        cngn, _ = calculate_cngn_from_quidax_sell_usdt(inv)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out = v3_swap_token1_for_token0(cngn, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        return out - inv, out, cngn
    process_local_best("QUIDAX_TO_UNI_BSC", *ternary_search(eval_quidax_to_bsc))

    # 2. UNI BSC -> Quidax (Sell USDT on BSC, Buy USDT on Quidax)
    def eval_bsc_to_quidax(inv: Decimal):
        cngn = v3_swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out, _ = walk_quidax_asks(asks, cngn)
        return out - inv, out, cngn
    process_local_best("UNI_BSC_TO_QUIDAX", *ternary_search(eval_bsc_to_quidax))

    # 3. Quidax -> UNI Base
    def eval_quidax_to_base(inv: Decimal):
        cngn, _ = calculate_cngn_from_quidax_sell_usdt(inv)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out = v3_swap_token0_for_token1(cngn, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        return out - inv, out, cngn
    process_local_best("QUIDAX_TO_UNI_BASE", *ternary_search(eval_quidax_to_base))

    # 4. UNI Base -> Quidax
    def eval_base_to_quidax(inv: Decimal):
        cngn = v3_swap_token1_for_token0(inv, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        if cngn == 0: return Decimal("-999999"), Decimal("0"), Decimal("0")
        out, _ = walk_quidax_asks(asks, cngn)
        return out - inv, out, cngn
    process_local_best("UNI_BASE_TO_QUIDAX", *ternary_search(eval_base_to_quidax))

    best_spread_bps = 0
    if best_size > 0:
        best_spread_bps = int(((usd_out_expected - best_size) / best_size) * 10000)

    curve_cex_to_dex = []
    curve_dex_to_cex = []

    # Create granular curves for hover details, evaluating exactly $1 to $1000
    plot_sizes = list(range(1, 1001))
    slippage_multiplier = Decimal("1") - Decimal("0.0010")
    
    for size in plot_sizes:
        inv = Decimal(size)

        # --- Direction 1: CEX -> DEX (Sell USDT on CEX, Buy USDT on DEX) ---
        cngn_q, cx_tr_q = calculate_cngn_from_quidax_sell_usdt(inv)
        
        out_bsc_q = v3_swap_token1_for_token0(cngn_q, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        out_bsc_no_fee_q = v3_swap_token1_for_token0(cngn_q, uni_bsc_sqrt, uni_bsc_liq, Decimal(0), 18, 6)

        out_base_q = v3_swap_token0_for_token1(cngn_q, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        out_base_no_fee_q = v3_swap_token0_for_token1(cngn_q, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)

        curve_cex_to_dex.append({
            "size": float(size),
            "quidax_levels": cx_tr_q,
            "bsc": {
                "cngn_acquired": float(cngn_q),
                "profit": float(out_bsc_q - inv),
                "profit_no_fee": float(out_bsc_no_fee_q - inv), 
                "min_acceptable_usd": float(out_bsc_q * slippage_multiplier),
                "usdt_out": float(out_bsc_q)
            },
            "base": {
                "cngn_acquired": float(cngn_q),
                "profit": float(out_base_q - inv),
                "profit_no_fee": float(out_base_no_fee_q - inv), 
                "min_acceptable_usd": float(out_base_q * slippage_multiplier),
                "usdt_out": float(out_base_q)
            }
        })

        # --- Direction 2: DEX -> CEX (Sell USDT on DEX, Buy USDT on CEX) ---
        cngn_bsc_d = v3_swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
        cngn_bsc_no_fee_d = v3_swap_token0_for_token1(inv, uni_bsc_sqrt, uni_bsc_liq, Decimal(0), 18, 6)

        out_bsc_d, cx_tr_bsc_d = walk_quidax_asks(asks, cngn_bsc_d)
        out_bsc_no_fee_d, _ = walk_quidax_asks(asks, cngn_bsc_no_fee_d)

        cngn_base_d = v3_swap_token1_for_token0(inv, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
        cngn_base_no_fee_d = v3_swap_token1_for_token0(inv, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)

        out_base_d, cx_tr_base_d = walk_quidax_asks(asks, cngn_base_d)
        out_base_no_fee_d, _ = walk_quidax_asks(asks, cngn_base_no_fee_d)

        curve_dex_to_cex.append({
            "size": float(size),
            "quidax_levels": cx_tr_bsc_d if cx_tr_bsc_d else cx_tr_base_d,
            "bsc": {
                "cngn_acquired": float(cngn_bsc_d),
                "profit": float(out_bsc_d - inv),
                "profit_no_fee": float(out_bsc_no_fee_d - inv), 
                "min_acceptable_usd": float(out_bsc_d * slippage_multiplier),
                "usdt_out": float(out_bsc_d)
            },
            "base": {
                "cngn_acquired": float(cngn_base_d),
                "profit": float(out_base_d - inv),
                "profit_no_fee": float(out_base_no_fee_d - inv), 
                "min_acceptable_usd": float(out_base_d * slippage_multiplier),
                "usdt_out": float(out_base_d)
            }
        })

    liquidation_valuation = {
        "quidax_cngn_usd": 0.0,
        "quidax_cngn_usd_no_fee": 0.0,
        "quidax_usdt": 0.0,
        "uni_bsc_cngn_usd": 0.0,
        "uni_bsc_cngn_usd_no_fee": 0.0,
        "uni_bsc_usdt": 0.0,
        "uni_base_cngn_usd": 0.0,
        "uni_base_cngn_usd_no_fee": 0.0,
        "uni_base_usdc": 0.0,
    }
    
    if current_balances:
        # Quidax CEX: delegate to compute_quidax_liquidation which uses
        # the real orderbook walk for both with-fee and no-fee values.
        qx_liq = compute_quidax_liquidation(quidax_depth, current_balances)
        if qx_liq:
            liquidation_valuation["quidax_cngn_usd"]        = qx_liq["quidax_cngn_usd"]
            liquidation_valuation["quidax_cngn_usd_no_fee"] = qx_liq["quidax_cngn_usd_no_fee"]
            liquidation_valuation["quidax_usdt"]            = qx_liq["quidax_usdt"]

        # Evaluate UNI BSC
        bsc_bal = next((b for b in current_balances if b.role in ("uni-bsc-trade", "trade_uni_bsc")), None)
        if bsc_bal:
            bsc_cngn = bsc_bal.token_balances.get("cNGN", Decimal(0))
            bsc_usdt = bsc_bal.token_balances.get("USDT", Decimal(0))
            liquidation_valuation["uni_bsc_usdt"] = float(bsc_usdt)
            if bsc_cngn > 0:
                # With fee: uses actual AMM fee rate
                liq_value = v3_swap_token1_for_token0(bsc_cngn, uni_bsc_sqrt, uni_bsc_liq, uni_bsc_fee, 18, 6)
                liquidation_valuation["uni_bsc_cngn_usd"] = float(liq_value)
                # No fee: same swap with fee=0
                liq_no_fee = v3_swap_token1_for_token0(bsc_cngn, uni_bsc_sqrt, uni_bsc_liq, Decimal(0), 18, 6)
                liquidation_valuation["uni_bsc_cngn_usd_no_fee"] = float(liq_no_fee)

        # Evaluate UNI Base
        base_bal = next((b for b in current_balances if b.role in ("uni-base-trade", "trade_uni_base")), None)
        if base_bal:
            base_cngn = base_bal.token_balances.get("cNGN", Decimal(0))
            base_usdc = base_bal.token_balances.get("USDC", Decimal(0))
            liquidation_valuation["uni_base_usdc"] = float(base_usdc)
            if base_cngn > 0:
                # With fee: uses actual AMM fee rate
                liq_value = v3_swap_token0_for_token1(base_cngn, uni_base_sqrt, uni_base_liq, uni_base_fee, 6, 6)
                liquidation_valuation["uni_base_cngn_usd"] = float(liq_value)
                # No fee: same swap with fee=0
                liq_no_fee = v3_swap_token0_for_token1(base_cngn, uni_base_sqrt, uni_base_liq, Decimal(0), 6, 6)
                liquidation_valuation["uni_base_cngn_usd_no_fee"] = float(liq_no_fee)


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
            "assetchain_fee_bps": 30,
            "estimated_gas_usd": 0.07
        },
        "liquidation_valuation": liquidation_valuation
    }
