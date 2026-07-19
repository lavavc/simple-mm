import json
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv()

from engine.config import settings

CACHE_FILE = "data/tx_senders_cache.json"
SWAPS_FILE = str(Path(__file__).resolve().parents[1] / "data" / "pool_swaps.json")

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print("Error loading cache:", e)
    return {}

def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def main():
    # Load cache
    cache = load_cache()
    print(f"Loaded cache with {len(cache)} transactions.")

    # Load swaps
    with open(SWAPS_FILE, "r") as f:
        swaps_data = json.load(f)

    # Gather missing transactions
    base_txs = sorted({x["tx"] for x in swaps_data.get("base", [])})
    bsc_txs = sorted({x["tx"] for x in swaps_data.get("bsc", [])})

    missing_base = [tx for tx in base_txs if tx not in cache]
    missing_bsc = [tx for tx in bsc_txs if tx not in cache]

    print(f"Base: {len(base_txs)} total, {len(missing_base)} missing.")
    print(f"BSC: {len(bsc_txs)} total, {len(missing_bsc)} missing.")

    if not missing_base and not missing_bsc:
        print("All transactions are already cached.")
        return

    # Initialize web3
    w3_base = Web3(Web3.HTTPProvider(settings.base_rpc_url))
    w3_bsc = Web3(Web3.HTTPProvider(settings.bsc_rpc_url))

    def fetch_sender(tx_hash, chain):
        w3 = w3_base if chain == "base" else w3_bsc
        # Ensure 0x prefix for web3 query
        full_tx = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
        try:
            tx = w3.eth.get_transaction(full_tx)
            return tx_hash, tx["from"]
        except Exception as e:
            # print(f"Error fetching {tx_hash} on {chain}: {e}")
            return tx_hash, None

    # We will query Base first, then BSC
    new_results = {}
    
    # Base
    if missing_base:
        print(f"Fetching {len(missing_base)} Base transactions...")
        with ThreadPoolExecutor(max_workers=25) as executor:
            futures = {executor.submit(fetch_sender, tx, "base"): tx for tx in missing_base}
            completed = 0
            for fut in as_completed(futures):
                tx_hash, sender = fut.result()
                if sender:
                    new_results[tx_hash] = sender.lower()
                completed += 1
                if completed % 100 == 0:
                    print(f"  Base progress: {completed}/{len(missing_base)}")

    # BSC
    if missing_bsc:
        print(f"Fetching {len(missing_bsc)} BSC transactions...")
        with ThreadPoolExecutor(max_workers=25) as executor:
            futures = {executor.submit(fetch_sender, tx, "bsc"): tx for tx in missing_bsc}
            completed = 0
            for fut in as_completed(futures):
                tx_hash, sender = fut.result()
                if sender:
                    new_results[tx_hash] = sender.lower()
                completed += 1
                if completed % 200 == 0:
                    print(f"  BSC progress: {completed}/{len(missing_bsc)}")

    # Merge and save
    cache.update(new_results)
    save_cache(cache)
    print(f"Saved cache with {len(cache)} total transactions.")

if __name__ == "__main__":
    main()
