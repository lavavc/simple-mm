"""
Exportable simulator to generate V3 profit curves for the frontend.
"""

from decimal import Decimal, getcontext
import time
from engine.config import settings
from engine.venues.dex.aerodrome import AERODROME_POOL_READ_CONFIG
from engine.venues.dex.pancakeswap import PANCAKESWAP_POOL_READ_CONFIG
from web3 import AsyncWeb3
import structlog

logger = structlog.get_logger()
getcontext().prec = 50

SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
Q96 = Decimal(2 ** 96)

# Hardcoded constants from config
PANCAKE_FEE = Decimal("0.0001")
AERO_FEE = Decimal("0.0005")

async def get_v3_pool_state(rpc_url: str, pool_address: str):
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    pool = w3.to_checksum_address(pool_address)
    
    # Try fetching, catch timeout
    try:
        slot0_raw = await w3.eth.call({"to": pool, "data": SLOT0_SELECTOR})
        liquidity_raw = await w3.eth.call({"to": pool, "data": LIQUIDITY_SELECTOR})
        
        sqrt_price_x96 = int.from_bytes(slot0_raw[:32], "big")
        liquidity = int.from_bytes(liquidity_raw[:32], "big")
        
        return Decimal(sqrt_price_x96), Decimal(liquidity)
    except Exception as e:
        logger.error("v3_pool_state_fetch_error", error=str(e), rpc=rpc_url)
        return None, None

def calc_pancake_buy_cngn(amount_usdt_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, apply_fee: bool = True) -> Decimal:
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)
    fee = PANCAKE_FEE if apply_fee else Decimal(0)
    amount_in_after_fee = amount_usdt_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**18)
    
    numerator = liquidity * sqrt_p
    denominator = (liquidity * Q96) + (amount_in_raw * sqrt_p)
    sqrt_p_new = (numerator * Q96) / denominator
    amount_out_raw = (liquidity * (sqrt_p - sqrt_p_new)) / Q96
    return amount_out_raw / Decimal(10**6)

def calc_aerodrome_buy_cngn(amount_usdc_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, apply_fee: bool = True) -> Decimal:
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)
    fee = AERO_FEE if apply_fee else Decimal(0)
    amount_in_after_fee = amount_usdc_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**6)
    
    sqrt_p_new = sqrt_p + (amount_in_raw * Q96) / liquidity
    amount_out_raw = (liquidity * Q96 * (sqrt_p_new - sqrt_p)) / (sqrt_p * sqrt_p_new)
    return amount_out_raw / Decimal(10**6)

def calc_aerodrome_sell_cngn(amount_cngn_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, apply_fee: bool = True) -> Decimal:
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)
    fee = AERO_FEE if apply_fee else Decimal(0)
    amount_in_after_fee = amount_cngn_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**6)
    
    numerator = liquidity * sqrt_p
    denominator = (liquidity * Q96) + (amount_in_raw * sqrt_p)
    sqrt_p_new = (numerator * Q96) / denominator
    amount_out_raw = (liquidity * (sqrt_p - sqrt_p_new)) / Q96
    return amount_out_raw / Decimal(10**6)

def calc_pancake_sell_cngn(amount_cngn_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, apply_fee: bool = True) -> Decimal:
    if liquidity == 0 or sqrt_p == 0: return Decimal(0)
    fee = PANCAKE_FEE if apply_fee else Decimal(0)
    amount_in_after_fee = amount_cngn_in * (Decimal("1") - fee)
    amount_in_raw = amount_in_after_fee * Decimal(10**6)
    
    sqrt_p_new = sqrt_p + (amount_in_raw * Q96) / liquidity
    amount_out_raw = (liquidity * Q96 * (sqrt_p_new - sqrt_p)) / (sqrt_p * sqrt_p_new)
    return amount_out_raw / Decimal(10**18)

async def generate_v3_profit_curve() -> dict:
    """Generates the side-by-side exact V3 curve data over a set of investment sizes. Returns dict suitable for JSON broadcast."""
    bsc_sqrt, bsc_liq = await get_v3_pool_state(settings.bsc_rpc_url, PANCAKESWAP_POOL_READ_CONFIG.pool_address)
    base_sqrt, base_liq = await get_v3_pool_state(settings.base_rpc_url, AERODROME_POOL_READ_CONFIG.pool_address)
    
    if not bsc_sqrt or not base_sqrt:
        return {}

    pancake_price = ((bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    p_price_usd = float(Decimal(1) / pancake_price)
    
    a_price_usd = float(((base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6)))

    test_sizes = [1, 10, 50, 100, 500, 1000, 2500, 5000, 10000, 50000, 100000]
    
    curve = []
    for size in test_sizes:
        investment_usd = Decimal(str(size))
        
        # 1. With Fees (Actual Cash Return)
        cngn_pancake = calc_pancake_buy_cngn(investment_usd, bsc_sqrt, bsc_liq, apply_fee=True)
        cngn_aero = calc_aerodrome_buy_cngn(investment_usd, base_sqrt, base_liq, apply_fee=True)
        usd_returned = calc_aerodrome_sell_cngn(cngn_pancake, base_sqrt, base_liq, apply_fee=True)
        
        # 2. Without Fees (Theoretical Return - Just Price Impact)
        cngn_pancake_no_fee = calc_pancake_buy_cngn(investment_usd, bsc_sqrt, bsc_liq, apply_fee=False)
        cngn_aero_no_fee = calc_aerodrome_buy_cngn(investment_usd, base_sqrt, base_liq, apply_fee=False)
        usd_returned_no_fee = calc_aerodrome_sell_cngn(cngn_pancake_no_fee, base_sqrt, base_liq, apply_fee=False)

        # 3. Slippage Tolerance Check (0.10% = subtract 10 basis points from the expected payout)
        slippage_tolerance = Decimal("0.0010") # 0.10%
        min_usd_acceptable = usd_returned * (Decimal("1") - slippage_tolerance)

        curve.append({
            "size": size,
            "cngn_pancake": float(cngn_pancake),
            "cngn_aero": float(cngn_aero),
            "profit": float(usd_returned - investment_usd),
            "profit_no_fee": float(usd_returned_no_fee - investment_usd),
            "cngn_pancake_no_fee": float(cngn_pancake_no_fee),
            "cngn_aero_no_fee": float(cngn_aero_no_fee),
            "min_acceptable_usd": float(min_usd_acceptable)
        })

    # Find optimal trade within $1000 inventory
    max_usd = 1000
    best_profit = Decimal("-999999")
    best_size = Decimal("0")
    best_dir = None
    best_cngn = Decimal("0")
    usd_out_expected = Decimal("0")
    best_spread_bps = 0

    step = 10
    # DELTA BALANCING VECTOR 1: Buy on PancakeSwap, Sell identical cNGN amount from Base inventory
    for size in range(10, max_usd + step, step):
        usd_in_bsc = Decimal(size)
        cngn_acquired_bsc = calc_pancake_buy_cngn(usd_in_bsc, bsc_sqrt, bsc_liq)
        
        # We don't bridge. We immediately sell the identical amount of cNGN we just bought
        # out of our pre-existing inventory on the Base chain.
        usd_out_base = calc_aerodrome_sell_cngn(cngn_acquired_bsc, base_sqrt, base_liq)
        
        if usd_out_base - usd_in_bsc > best_profit:
            best_profit = usd_out_base - usd_in_bsc
            best_size = usd_in_bsc
            best_dir = "PANCAKE_TO_AERO_DELTA_BALANCE"
            best_cngn = cngn_acquired_bsc
            usd_out_expected = usd_out_base

    # DELTA BALANCING VECTOR 2: Buy on Aerodrome, Sell identical cNGN amount from BSC inventory
    for size in range(10, max_usd + step, step):
        usd_in_base = Decimal(size)
        cngn_acquired_base = calc_aerodrome_buy_cngn(usd_in_base, base_sqrt, base_liq)
        
        # Immediate sell from PancakeSwap inventory
        usd_out_bsc = calc_pancake_sell_cngn(cngn_acquired_base, bsc_sqrt, bsc_liq)
        
        if usd_out_bsc - usd_in_base > best_profit:
            best_profit = usd_out_bsc - usd_in_base
            best_size = usd_in_base
            best_dir = "AERO_TO_PANCAKE_DELTA_BALANCE"
            best_cngn = cngn_acquired_base
            usd_out_expected = usd_out_bsc

    if best_size > 0:
        best_spread_bps = int(((usd_out_expected - best_size) / best_size) * 10000)

    return {
        "timestamp": int(time.time() * 1000),
        "prices": {
            "pancakeswap": p_price_usd,
            "aerodrome": a_price_usd
        },
        "stats": {
            "pancake_liquidity_cngn_raw": str(bsc_liq),
            "aerodrome_liquidity_cngn_raw": str(base_liq)
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
            "pancake_fee_bps": 1,
            "aerodrome_fee_bps": 5,
            "estimated_gas_usd": 0.07
        }
    }
