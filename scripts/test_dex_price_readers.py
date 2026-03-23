"""Test live on-chain DEX price reads using PoolPriceReader.

Reads slot0() from the AssetChain V3 pool. No private keys required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.venues.dex.pool_reader_v3 import PoolPriceReader
from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG


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

    assetchain = PoolPriceReader(config=ASSETCHAIN_POOL_READ_CONFIG, source_name="assetchain")
    test_reader("AssetChain V3 — cNGN/USDT", assetchain)

    print(f"\n{'=' * 60}")
    print("  Done!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
