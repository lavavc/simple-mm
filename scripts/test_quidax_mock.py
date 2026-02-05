#!/usr/bin/env python3
"""Test script demonstrating Quidax mock usage."""

import asyncio
from decimal import Decimal

from engine.venues.cex import QuidaxAdapter, MockQuidaxClient
from engine.api.schemas import CexParams


async def main():
    # Create mock client with initial balances
    mock = MockQuidaxClient(
        initial_balances={
            "cngn": "1000000",  # 1M CNGN
            "usdt": "1000",     # 1000 USDT
            "usdc": "0",
            "ngn": "50000",
        },
        simulate_latency=False,  # Set True to simulate network delays
    )

    # Create adapter with mock client injected
    adapter = QuidaxAdapter(
        api_key="test_key",
        api_secret="test_secret",
        params=CexParams(
            ladder_levels=5,
            ladder_increment_ngn=Decimal("0.50"),
            liquidity_per_level_percent=Decimal("10.0"),
        ),
        market="cngnusdt",
        http_client=mock,
    )

    print("=" * 60)
    print("QUIDAX MOCK TEST")
    print("=" * 60)

    # 1. Get initial position
    print("\n1. Initial Position:")
    position = await adapter.get_position()
    print(f"   CNGN: {position.balances['cngn']}")
    print(f"   USDT: {position.balances['usdt']}")
    print(f"   Open orders: {position.open_orders}")

    # 2. Sync order ladder at reference price
    reference_price = Decimal("1650.00")  # NGN per USDT
    print(f"\n2. Syncing order ladder at {reference_price} NGN/USDT...")
    await adapter.sync_order_ladder(reference_price)

    # 3. Check orders created
    print("\n3. Orders after ladder sync:")
    stats = mock.get_stats()
    for order in stats["orders"]:
        print(f"   {order['side'].upper():4} | Price: {float(order['price']):,.2f} | Volume: {float(order['volume']):,.2f}")

    # 4. Simulate some fills
    print("\n4. Simulating order fills...")
    orders = list(mock.orders.keys())
    if orders:
        # Fill the first buy order 50%
        buy_orders = [oid for oid, o in mock.orders.items() if o["side"] == "buy"]
        if buy_orders:
            mock.simulate_fill(buy_orders[0], fill_percent=50)
            print(f"   Filled 50% of buy order {buy_orders[0]}")

        # Fill the first sell order 100%
        sell_orders = [oid for oid, o in mock.orders.items() if o["side"] == "sell"]
        if sell_orders:
            mock.simulate_fill(sell_orders[0], fill_percent=100)
            print(f"   Filled 100% of sell order {sell_orders[0]}")

    # 5. Check updated position
    print("\n5. Position after fills:")
    position = await adapter.get_position()
    print(f"   CNGN: {position.balances['cngn']}")
    print(f"   USDT: {position.balances['usdt']}")
    print(f"   Open buy orders: {position.open_orders['buy_count']}")
    print(f"   Open sell orders: {position.open_orders['sell_count']}")

    # 6. Cancel all orders
    print("\n6. Cancelling all orders...")
    cancelled = await adapter.cancel_all_orders()
    print(f"   Cancelled {cancelled} orders")

    # 7. Final state
    print("\n7. Final Mock Stats:")
    stats = mock.get_stats()
    print(f"   Balances: {stats['balances']}")
    print(f"   Open orders: {stats['open_orders']}")
    print(f"   Total actions logged: {stats['total_actions']}")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
