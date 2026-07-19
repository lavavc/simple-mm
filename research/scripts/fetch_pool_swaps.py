"""Fetch all V4 Swap events for the Base and BSC cNGN pools since April 8, with block timestamps."""
import json, time
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from engine.config import settings
from engine.market.dex_volume import V4_SWAP_TOPIC
from engine.math.v3 import sqrt_price_x96_to_decimal
from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG as BASECFG
from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG as BSCCFG

START_TS = int(datetime(2026, 4, 8, tzinfo=timezone.utc).timestamp())

def block_at_ts(w3, ts):
    lo, hi = 1, w3.eth.block_number
    while lo < hi:
        mid = (lo + hi) // 2
        if w3.eth.get_block(mid)["timestamp"] < ts: lo = mid + 1
        else: hi = mid
    return lo

def fetch(chain, rpc, cfg, ngn_from_price):
    w3 = Web3(Web3.HTTPProvider(rpc))
    if chain == "bsc":
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    start = block_at_ts(w3, START_TS); latest = w3.eth.block_number
    print(f"{chain}: blocks {start}..{latest}", flush=True)
    logs = []
    lo, chunk = start, 100_000
    while lo <= latest:
        hi = min(lo + chunk - 1, latest)
        try:
            batch = w3.eth.get_logs({
                "address": Web3.to_checksum_address(cfg.pool_manager),
                "topics": [V4_SWAP_TOPIC, cfg.pool_address],
                "fromBlock": lo, "toBlock": hi,
            })
        except Exception as e:
            if chunk > 2000:
                chunk //= 2; continue
            raise
        logs.extend(batch); lo = hi + 1
    print(f"{chain}: {len(logs)} swaps", flush=True)

    blocks = sorted({l["blockNumber"] for l in logs})
    ts_map = {}
    for i, b in enumerate(blocks):
        ts_map[b] = w3.eth.get_block(b)["timestamp"]
        if i % 500 == 0: print(f"{chain}: ts {i}/{len(blocks)}", flush=True)

    out = []
    for l in logs:
        data = bytes(l["data"])
        amount0 = int.from_bytes(data[0:32], "big", signed=True)
        amount1 = int.from_bytes(data[32:64], "big", signed=True)
        sqrtp = int.from_bytes(data[64:96], "big")
        p = sqrt_price_x96_to_decimal(sqrtp, cfg.token0_decimals, cfg.token1_decimals)
        out.append({
            "ts": ts_map[l["blockNumber"]] * 1000,
            "block": l["blockNumber"],
            "tx": l["transactionHash"].hex(),
            "amount0": str(amount0), "amount1": str(amount1),
            "ngn_per_usd": float(ngn_from_price(p)),
        })
    out.sort(key=lambda r: (r["ts"], r["block"]))
    return out

import os
OUT = str(Path(__file__).resolve().parents[1] / "data" / "pool_swaps.json")
data = json.load(open(OUT)) if os.path.exists(OUT) else {}
if "base" not in data:
    data["base"] = fetch("base", settings.base_rpc_url, BASECFG, lambda p: 1 / p)  # token0=cNGN, token1=USDC
    json.dump(data, open(OUT, "w")); print("base saved", flush=True)
if "bsc" not in data:
    data["bsc"] = fetch("bsc", settings.bsc_rpc_url, BSCCFG, lambda p: p)          # token0=USDT, token1=cNGN
    json.dump(data, open(OUT, "w")); print("bsc saved", flush=True)
print(f"saved: base={len(data['base'])} bsc={len(data['bsc'])} -> {OUT}", flush=True)
