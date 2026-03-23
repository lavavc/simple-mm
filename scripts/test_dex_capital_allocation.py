#!/usr/bin/env python3
"""
Test script demonstrating DEX capital allocation settings.

Shows how max_utilization_percent, min_reserve, and max_position_usd
affect the amounts deployed to LP.
"""

from decimal import Decimal
from engine.api.schemas import DexParams


def simulate_capital_allocation(
    balance_token0: Decimal,
    balance_token1: Decimal,
    params: DexParams,
    reference_price_usd: Decimal | None = None,
    token0_decimals: int = 18,
    token1_decimals: int = 6,
) -> dict:
    """
    Simulate the capital allocation logic from V4LPAdapter.calculate_mint_amounts()

    Returns dict with calculation breakdown.
    """
    # 1. Apply max utilization percent
    max_util = params.max_utilization_percent / Decimal("100")
    available0 = balance_token0 * max_util
    available1 = balance_token1 * max_util

    step1 = {"available0": available0, "available1": available1}

    # 2. Subtract minimum reserves
    available0 = max(Decimal("0"), available0 - params.min_reserve_token0)
    available1 = max(Decimal("0"), available1 - params.min_reserve_token1)

    # Also ensure we don't go below reserves even after utilization calc
    max_from_reserve0 = max(Decimal("0"), balance_token0 - params.min_reserve_token0)
    max_from_reserve1 = max(Decimal("0"), balance_token1 - params.min_reserve_token1)
    available0 = min(available0, max_from_reserve0)
    available1 = min(available1, max_from_reserve1)

    step2 = {"available0": available0, "available1": available1}

    # 3. Apply max position USD cap if set
    step3_applied = False
    if params.max_position_usd and reference_price_usd:
        token0_usd_value = available0 * reference_price_usd
        token1_usd_value = available1  # Stablecoin ≈ $1

        total_usd = token0_usd_value + token1_usd_value

        if total_usd > params.max_position_usd:
            scale_factor = params.max_position_usd / total_usd
            available0 = available0 * scale_factor
            available1 = available1 * scale_factor
            step3_applied = True

    step3 = {
        "available0": available0,
        "available1": available1,
        "cap_applied": step3_applied,
    }

    # Final amounts (in raw units)
    amount0_raw = int(available0 * Decimal(10**token0_decimals))
    amount1_raw = int(available1 * Decimal(10**token1_decimals))

    return {
        "input": {
            "balance_token0": balance_token0,
            "balance_token1": balance_token1,
            "params": {
                "max_utilization_percent": params.max_utilization_percent,
                "min_reserve_token0": params.min_reserve_token0,
                "min_reserve_token1": params.min_reserve_token1,
                "max_position_usd": params.max_position_usd,
            },
        },
        "step1_utilization": step1,
        "step2_reserves": step2,
        "step3_usd_cap": step3,
        "final": {
            "amount0": available0,
            "amount1": available1,
            "amount0_raw": amount0_raw,
            "amount1_raw": amount1_raw,
        },
    }


def main():
    print("=" * 70)
    print("DEX CAPITAL ALLOCATION TEST")
    print("=" * 70)

    # Scenario 1: Default params (80% utilization, no reserves)
    print("\n" + "-" * 70)
    print("SCENARIO 1: Default params (80% utilization)")
    print("-" * 70)

    result = simulate_capital_allocation(
        balance_token0=Decimal("1000000"),  # 1M CNGN
        balance_token1=Decimal("1000"),     # 1000 USDC
        params=DexParams(),  # defaults
    )

    print(f"  Wallet: {result['input']['balance_token0']:,.0f} CNGN, {result['input']['balance_token1']:,.0f} USDC")
    print(f"  Max utilization: {result['input']['params']['max_utilization_percent']}%")
    print(f"  → After utilization cap: {result['step1_utilization']['available0']:,.0f} CNGN, {result['step1_utilization']['available1']:,.0f} USDC")
    print(f"  → Final to deploy: {result['final']['amount0']:,.0f} CNGN, {result['final']['amount1']:,.0f} USDC")

    # Scenario 2: With reserves
    print("\n" + "-" * 70)
    print("SCENARIO 2: With minimum reserves")
    print("-" * 70)

    params_with_reserves = DexParams(
        max_utilization_percent=Decimal("90"),
        min_reserve_token0=Decimal("100000"),  # Keep 100k CNGN
        min_reserve_token1=Decimal("200"),      # Keep 200 USDC
    )

    result = simulate_capital_allocation(
        balance_token0=Decimal("1000000"),
        balance_token1=Decimal("1000"),
        params=params_with_reserves,
    )

    print(f"  Wallet: {result['input']['balance_token0']:,.0f} CNGN, {result['input']['balance_token1']:,.0f} USDC")
    print(f"  Max utilization: {result['input']['params']['max_utilization_percent']}%")
    print(f"  Min reserves: {result['input']['params']['min_reserve_token0']:,.0f} CNGN, {result['input']['params']['min_reserve_token1']:,.0f} USDC")
    print(f"  → After utilization cap: {result['step1_utilization']['available0']:,.0f} CNGN, {result['step1_utilization']['available1']:,.0f} USDC")
    print(f"  → After reserves: {result['step2_reserves']['available0']:,.0f} CNGN, {result['step2_reserves']['available1']:,.0f} USDC")
    print(f"  → Final to deploy: {result['final']['amount0']:,.0f} CNGN, {result['final']['amount1']:,.0f} USDC")

    # Scenario 3: With USD cap
    print("\n" + "-" * 70)
    print("SCENARIO 3: With USD position cap")
    print("-" * 70)

    params_with_cap = DexParams(
        max_utilization_percent=Decimal("100"),  # Allow full balance
        max_position_usd=Decimal("5000"),        # But cap at $5000 total
    )

    # Reference price: 1 CNGN = $0.0006 (1650 NGN = 1 USD)
    result = simulate_capital_allocation(
        balance_token0=Decimal("10000000"),  # 10M CNGN (~$6000)
        balance_token1=Decimal("5000"),       # 5000 USDC (~$5000)
        params=params_with_cap,
        reference_price_usd=Decimal("0.0006"),
    )

    print(f"  Wallet: {result['input']['balance_token0']:,.0f} CNGN (~$6000), {result['input']['balance_token1']:,.0f} USDC")
    print(f"  Max position: ${result['input']['params']['max_position_usd']:,.0f}")
    print(f"  → After utilization: {result['step1_utilization']['available0']:,.0f} CNGN, {result['step1_utilization']['available1']:,.0f} USDC")
    print(f"  → USD cap applied: {result['step3_usd_cap']['cap_applied']}")
    print(f"  → Final to deploy: {result['final']['amount0']:,.0f} CNGN, {result['final']['amount1']:,.0f} USDC")

    # Scenario 4: Conservative production settings
    print("\n" + "-" * 70)
    print("SCENARIO 4: Conservative production settings")
    print("-" * 70)

    production_params = DexParams(
        max_utilization_percent=Decimal("70"),   # Only use 70%
        min_reserve_token0=Decimal("50000"),     # Keep 50k CNGN for gas/emergencies
        min_reserve_token1=Decimal("100"),       # Keep 100 USDC
        max_position_usd=Decimal("10000"),       # Max $10k position
    )

    result = simulate_capital_allocation(
        balance_token0=Decimal("5000000"),   # 5M CNGN
        balance_token1=Decimal("3000"),       # 3000 USDC
        params=production_params,
        reference_price_usd=Decimal("0.0006"),
    )

    print(f"  Wallet: {result['input']['balance_token0']:,.0f} CNGN, {result['input']['balance_token1']:,.0f} USDC")
    print(f"  Max utilization: {result['input']['params']['max_utilization_percent']}%")
    print(f"  Min reserves: {result['input']['params']['min_reserve_token0']:,.0f} CNGN, {result['input']['params']['min_reserve_token1']:,.0f} USDC")
    print(f"  Max position: ${result['input']['params']['max_position_usd']:,.0f}")
    print(f"  → After utilization: {result['step1_utilization']['available0']:,.0f} CNGN, {result['step1_utilization']['available1']:,.0f} USDC")
    print(f"  → After reserves: {result['step2_reserves']['available0']:,.0f} CNGN, {result['step2_reserves']['available1']:,.0f} USDC")
    print(f"  → USD cap applied: {result['step3_usd_cap']['cap_applied']}")
    print(f"  → Final to deploy: {result['final']['amount0']:,.0f} CNGN, {result['final']['amount1']:,.0f} USDC")

    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
