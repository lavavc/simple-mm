"""Test live on-chain DEX price reads using PoolPriceReader.

Reads slot0() from Aerodrome (Base) and PancakeSwap (BSC) mainnet pools.
No private keys required -- just public RPC calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.venues.dex.pool_reader import PoolPriceReader
from engine.venues.dex.aerodrome import AERODROME_POOL_READ_CONFIG
from engine.venues.dex.pancakeswap import PANCAKESWAP_POOL_READ_CONFIG


def test_reader(name: str, reader: PoolPriceReader) -> None:
    cfg = reader.config
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"  RPC:    {cfg.rpc_url}")
    print(f"  Pool:   {cfg.pool_address}")
    print(f"  Pair:   {cfg.token0_symbol}({cfg.token0_decimals})/{cfg.token1_symbol}({cfg.token1_decimals})")
    print(f"  Invert: {cfg.invert_price}")
    print(f"{'=' * 60}")

    quote = reader.get_price()
    if quote is None:
        print("  FAIL: returned None")
        return

    print(f"  Source:  {quote.source}")
    print(f"  Mid:     {quote.mid}")
    print(f"  Bid:     {quote.bid}")
    print(f"  Ask:     {quote.ask}")

    # Sanity: cNGN should be worth roughly 0.0006–0.0008 USD
    mid_float = float(quote.mid)
    if 0.0001 < mid_float < 0.01:
        print(f"  OK: {mid_float:.6f} USD per cNGN")
    else:
        print(f"  WARNING: price {mid_float} outside expected range (0.0001–0.01)")


def main():
    print("Live DEX price reads (no private keys needed)")

    aerodrome = PoolPriceReader(config=AERODROME_POOL_READ_CONFIG, source_name="aerodrome")
    test_reader("Aerodrome (Base) — cNGN/USDC", aerodrome)

    pancakeswap = PoolPriceReader(config=PANCAKESWAP_POOL_READ_CONFIG, source_name="pancakeswap")
    test_reader("PancakeSwap V3 (BSC) — cNGN/USDT", pancakeswap)

    print(f"\n{'=' * 60}")
    print("  Done!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
