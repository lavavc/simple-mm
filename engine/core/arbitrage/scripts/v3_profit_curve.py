"""
Simulating V3 Price Impact to find exact profit per trade size.
Since the current spread is negative against fees, we artificially inflate the
Aerodrome price temporarily just to observe the exact profit curve across trade sizes.
"""

import asyncio
from decimal import Decimal, getcontext
import os
from web3 import AsyncWeb3
from dotenv import load_dotenv

load_dotenv()
getcontext().prec = 50

# Configure variables
BSC_RPC = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org/")
BASE_RPC = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
ASSETCHAIN_RPC = os.getenv("ASSETCHAIN_RPC_URL", "https://mainnet-rpc.assetchain.org")

PANCAKE_POOL = "0xb84e7c912a1034ad674bba8859fca84f1f614a29"
AERO_POOL = "0x0206B696a410277eF692024C2B64CcF4EaC78589"
ASSETCHAIN_POOL = "0xE2a45a102B00Fad6447d0AD859b43BAf8bF6DeF1"

SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
# fee() selector: keccak256("fee()")[:4] = 0xddca3f43
FEE_SELECTOR = "0xddca3f43"
Q96 = Decimal(2 ** 96)

async def get_v3_pool_state(rpc_url: str, pool_address: str):
    """Fetch pool state and fee tier from chain. Fee is cached per call."""
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    pool = w3.to_checksum_address(pool_address)

    slot0_raw, liquidity_raw, fee_raw = await asyncio.gather(
        w3.eth.call({"to": pool, "data": SLOT0_SELECTOR}),
        w3.eth.call({"to": pool, "data": LIQUIDITY_SELECTOR}),
        w3.eth.call({"to": pool, "data": FEE_SELECTOR}),
    )

    sqrt_price_x96 = int.from_bytes(slot0_raw[:32], "big")
    liquidity = int.from_bytes(liquidity_raw[:32], "big")
    # fee() returns uint24 in pool units (e.g. 10000 = 1%). Convert to Decimal ratio.
    fee_units = int.from_bytes(fee_raw[:32], "big")
    fee = Decimal(fee_units) / Decimal("1000000")  # e.g. 10000 -> 0.01

    return Decimal(sqrt_price_x96), Decimal(liquidity), fee

# ========== V3 MATH FUNCTIONS ==========

def calc_pancake_buy_cngn(amount_usdt_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal) -> Decimal:
    """
    PancakeSwap (BSC):
    token0 = USDT (18 decimals), token1 = cNGN (6 decimals)
    We are trading token0 for token1.
    Formula: sqrtP_new = (L * sqrtP) / (L + amount_in * sqrtP)
    """
    amount_in_after_fee = amount_usdt_in * (Decimal("1") - fee)
    # Convert incoming USDT to raw token0 units
    amount_in_raw = amount_in_after_fee * Decimal(10**18)
    
    numerator = liquidity * sqrt_p
    denominator = (liquidity * Q96) + (amount_in_raw * sqrt_p)
    sqrt_p_new = (numerator * Q96) / denominator
    
    amount_out_raw = (liquidity * (sqrt_p - sqrt_p_new)) / Q96
    
    # Return output as human-readable cNGN (token1 has 6 decimals)
    return amount_out_raw / Decimal(10**6)

def calc_aerodrome_sell_cngn(amount_cngn_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal) -> Decimal:
    """
    Aerodrome (Base):
    token0 = cNGN (6 decimals), token1 = USDC (6 decimals)
    We are trading token0 for token1.
    Formula: sqrtP_new = (L * sqrtP) / (L + amount_in * sqrtP)
    """
    amount_in_after_fee = amount_cngn_in * (Decimal("1") - fee)
    # Convert incoming cNGN to raw token0 units
    amount_in_raw = amount_in_after_fee * Decimal(10**6)
    
    numerator = liquidity * sqrt_p
    denominator = (liquidity * Q96) + (amount_in_raw * sqrt_p)
    sqrt_p_new = (numerator * Q96) / denominator
    
    amount_out_raw = (liquidity * (sqrt_p - sqrt_p_new)) / Q96
    
    # Return output as human-readable USDC (token1 has 6 decimals)
    return amount_out_raw / Decimal(10**6)

def calc_aerodrome_buy_cngn(amount_usdc_in: Decimal, sqrt_p: Decimal, liquidity: Decimal, fee: Decimal) -> Decimal:
    """
    Aerodrome (Base):
    token0 = cNGN (6 decimals), token1 = USDC (6 decimals)
    We are trading token1 for token0.
    Formula: sqrtP_new = sqrtP + (amount_in * Q96) / L
    amount_out = L * (sqrtP_new - sqrtP) / (sqrtP * sqrtP_new)
    """
    amount_in_after_fee = amount_usdc_in * (Decimal("1") - fee)
    # Convert incoming USDC to raw token1 units
    amount_in_raw = amount_in_after_fee * Decimal(10**6)
    
    sqrt_p_new = sqrt_p + (amount_in_raw * Q96) / liquidity
    
    amount_out_raw = (liquidity * Q96 * (sqrt_p_new - sqrt_p)) / (sqrt_p * sqrt_p_new)
    
    # Return output as human-readable cNGN (token0 has 6 decimals)
    return amount_out_raw / Decimal(10**6)


async def main():
    print("Fetching live V3 data (slot0, liquidity, fee) from chain...")
    # Fetch all pools in parallel
    (bsc_sqrt, bsc_liq, pancake_fee), \
    (base_sqrt, base_liq, aero_fee), \
    (asset_sqrt, asset_liq, asset_fee) = await asyncio.gather(
        get_v3_pool_state(BSC_RPC, PANCAKE_POOL),
        get_v3_pool_state(BASE_RPC, AERO_POOL),
        get_v3_pool_state(ASSETCHAIN_RPC, ASSETCHAIN_POOL)
    )

    pancake_price = ((bsc_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    p_price_usd = Decimal(1) / pancake_price
    a_price_usd = ((base_sqrt / Q96) ** 2) * Decimal(10 ** (6 - 6))
    asset_price = ((asset_sqrt / Q96) ** 2) * Decimal(10 ** (18 - 6))
    asset_price_usd = Decimal(1) / asset_price

    print("=================================================================")
    print("        V3 PRICE IMPACT & PROFIT CURVE SIMULATOR                 ")
    print("=================================================================")
    print(f"PancakeSwap Price: ${p_price_usd:.7f} | Fee: {pancake_fee * 100:.4f}%")
    print(f"Aerodrome Price:   ${a_price_usd:.7f} | Fee: {aero_fee * 100:.4f}%")
    print(f"AssetChain Price:  ${asset_price_usd:.7f} | Fee: {asset_fee * 100:.4f}%")
    print("-----------------------------------------------------------------")
    print(f"{'Invest ($)':<10} | {'Got cNGN(Pancake)':<17} | {'Got cNGN(Asset)':<17} | {'Net Profit ($)':<15}")
    print("-" * 65)

    test_sizes = [1, 10, 50, 100, 500, 1000, 2500, 5000, 10000, 50000, 100000]

    for size in test_sizes:
        investment_usd = Decimal(str(size))

        # We test buying on Pancake and AssetChain
        # Pancake buys (token0=USDT -> token1=cNGN) -- same as AssetChain structure
        cngn_received_pancake = calc_pancake_buy_cngn(investment_usd, bsc_sqrt, bsc_liq, pancake_fee)
        cngn_received_asset = calc_pancake_buy_cngn(investment_usd, asset_sqrt, asset_liq, asset_fee)

        # Let's say we do a round-trip: sell cNGN bought on AssetChain over on Aerodrome
        usd_returned = calc_aerodrome_sell_cngn(cngn_received_asset, base_sqrt, base_liq, aero_fee)
        net_profit = usd_returned - investment_usd

        print(f"${size:<9,d} | {int(cngn_received_pancake):<17,d} | {int(cngn_received_asset):<17,d} | ${net_profit:<14,.2f}")

if __name__ == "__main__":
    asyncio.run(main())
