"""
Live Arbitrage Execution Optimizer for cNGN.
Connects directly to DexScreener to pull live liquidity and prices for both chains,
and calculates the mathematically perfect trade size using constant product (x*y=k) math as a conservative slippage bounds.
"""

import httpx
import asyncio
from decimal import Decimal
import structlog

logger = structlog.get_logger()

# PancakeSwap V3 (BSC)
PANCAKE_POOL = "0xb84e7c912a1034ad674bba8859fca84f1f614a29"
PANCAKE_FEE = Decimal("0.0025") # 0.25%

# Aerodrome (Base)
AERO_POOL = "0x0206B696a410277eF692024C2B64CcF4EaC78589"
AERO_FEE = Decimal("0.0030") # 0.30%

async def fetch_pool_data(chain: str, pool_address: str) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pool_address}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        data = response.json()
        
    pairs = data.get("pairs", [])
    if not pairs:
        raise ValueError(f"Could not fetch data for {chain} {pool_address}")
        
    pair = pairs[0]
    return {
        "price_usd": Decimal(str(pair["priceUsd"])),
        "liquidity_usd": Decimal(str(pair["liquidity"]["usd"])),
        # DexScreener defines 'base' as the token, 'quote' as the stablecoin
        "reserve_cngn": Decimal(str(pair["liquidity"]["base"])),
        "reserve_usd": Decimal(str(pair["liquidity"]["quote"]))
    }

def calculate_profit(
    trade_size_usd: Decimal, 
    buy_reserve_usd: Decimal, buy_reserve_cngn: Decimal, buy_fee: Decimal,
    sell_reserve_usd: Decimal, sell_reserve_cngn: Decimal, sell_fee: Decimal
) -> Decimal:
    """Simulate exactly how much profit we make routing trade_size_usd through both pools."""
    if trade_size_usd <= 0:
        return Decimal("0")
        
    # --- LEG 1: Buy cNGN ---
    usd_in_after_fee = trade_size_usd * (Decimal("1") - buy_fee)
    # Output cNGN = (Reserve_cNGN * In) / (Reserve_USD + In)
    cngn_out = (buy_reserve_cngn * usd_in_after_fee) / (buy_reserve_usd + usd_in_after_fee)
    
    # --- LEG 2: Sell cNGN ---
    cngn_in_after_fee = cngn_out * (Decimal("1") - sell_fee)
    # Output USD = (Reserve_USD * In) / (Reserve_cNGN + In)
    usd_out = (sell_reserve_usd * cngn_in_after_fee) / (sell_reserve_cngn + cngn_in_after_fee)
    
    return usd_out - trade_size_usd

def find_optimal_size(
    buy_reserve_usd: Decimal, buy_reserve_cngn: Decimal, buy_fee: Decimal,
    sell_reserve_usd: Decimal, sell_reserve_cngn: Decimal, sell_fee: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """Finds the mathematically perfect peak of the profit curve."""
    
    best_size = Decimal("0")
    max_profit = Decimal("-999999")
    
    # We step up to $10,000 to find the coarse peak
    for i in range(10, 10000, 10):
        size = Decimal(str(i))
        profit = calculate_profit(
            size, 
            buy_reserve_usd, buy_reserve_cngn, buy_fee,
            sell_reserve_usd, sell_reserve_cngn, sell_fee
        )
        if profit > max_profit:
            max_profit = profit
            best_size = size
            
    # If the gross math is negative even at $10, it will be negative everywhere
    if best_size == Decimal("0") or max_profit <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")
            
    # Fine search around peak
    fine_start = max(Decimal("0"), best_size - Decimal("20"))
    fine_end = best_size + Decimal("20")
    
    current = fine_start
    while current <= fine_end:
        profit = calculate_profit(
            current, 
            buy_reserve_usd, buy_reserve_cngn, buy_fee,
            sell_reserve_usd, sell_reserve_cngn, sell_fee
        )
        if profit > max_profit:
            max_profit = profit
            best_size = current
        current += Decimal("0.10")
        
    # Return the exact cngn acquired at this perfect size for reporting
    usd_after_fee = best_size * (Decimal("1") - buy_fee)
    cngn_acquired = (buy_reserve_cngn * usd_after_fee) / (buy_reserve_usd + usd_after_fee)
    
    return best_size, max_profit, cngn_acquired

async def main():
    print("Fetching live on-chain Pool data...\n")
    pancake = await fetch_pool_data("bsc", PANCAKE_POOL)
    aerodrome = await fetch_pool_data("base", AERO_POOL)
    
    p_price = pancake["price_usd"]
    a_price = aerodrome["price_usd"]
    
    print("=================================================================")
    print("              LIVE STATISTICAL ARBITRAGE OPTIMIZER               ")
    print("=================================================================")
    print(f"PancakeSwap (BSC) Price:  ${p_price:.7f}")
    print(f"Aerodrome (Base) Price:   ${a_price:.7f}")
    
    # Determine the direction of the trade
    if p_price < a_price:
        buy_venue, buy_price, buy_fee, buy_data = "PancakeSwap", p_price, PANCAKE_FEE, pancake
        sell_venue, sell_price, sell_fee, sell_data = "Aerodrome", a_price, AERO_FEE, aerodrome
        spread_bps = ((a_price - p_price) / p_price) * 10000
    else:
        buy_venue, buy_price, buy_fee, buy_data = "Aerodrome", a_price, AERO_FEE, aerodrome
        sell_venue, sell_price, sell_fee, sell_data = "PancakeSwap", p_price, PANCAKE_FEE, pancake
        spread_bps = ((p_price - a_price) / a_price) * 10000
        
    print(f"Gross Spread:             {spread_bps:.2f} bps")
    print(f"Total Trade Fees:         {((buy_fee + sell_fee) * 10000):.2f} bps")
    print(f"Direction to Arbs:        Buy on {buy_venue}, Sell on {sell_venue}\n")
    
    # Calculate optimal size
    best_size, max_profit, cngn_transacted = find_optimal_size(
        buy_data["reserve_usd"], buy_data["reserve_cngn"], buy_fee,
        sell_data["reserve_usd"], sell_data["reserve_cngn"], sell_fee
    )
    
    if max_profit > 0 and spread_bps > ((buy_fee + sell_fee) * 10000):
        print(f"🎯 PERFECT TRADE SIZE: ${best_size:,.2f}")
        print(f"💰 MAXIMUM PROFIT:     ${max_profit:,.2f}\n")
        
        print("-" * 65)
        print(" SIMULATED EXECUTION TRAIL ".center(65))
        print("-" * 65)
        print(f"[ACTION 1] {buy_venue} Wallet: Spend ${best_size:,.2f} USD")
        print(f"                         Receive {cngn_transacted:,.2f} cNGN")
        print("")
        print(f"[ACTION 2] {sell_venue} Wallet: Sell {cngn_transacted:,.2f} cNGN")
        print(f"                         Receive ${(best_size + max_profit):,.2f} USDC/T")
        print("")
        print(f"[RESULT]   Global P&L:   +${max_profit:,.2f} pure profit")
        print("           Delta Impact: Neutral (Bought and Sold exact same cNGN amount)")
        print("=================================================================")
        
    else:
        print("❌ NO ACTION REQUIRED")
        print("Reason: Spread is smaller than the combined LP fees (0.55%), or slippage immediately kills the profit margin.")
        print("We must wait for price divergence to widen.")
        print("=================================================================")

if __name__ == "__main__":
    asyncio.run(main())
