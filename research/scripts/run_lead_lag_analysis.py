import json
import os
import sys
import bisect
import sqlite3
from pathlib import Path

# Set up paths
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SWAPS_FILE = str(Path(__file__).resolve().parents[1] / "data" / "pool_swaps.json")
CACHE_FILE = "data/tx_senders_cache.json"
DB_FILE = "data/cngn.db"

# Bot wallet addresses from engine/accounts.py config
BOT_WALLETS = {
    "0xB25dB46588634D1153c058407D08361AbC6323fE".lower(),
    "0x23DF63f5388064c614204A5042Bdc833c82e14E4".lower(),
    "0x595D90aEDC76F6fBD4b075c62292DaBC2aF26023".lower(),
    "0x71A39D4663d52FFEb2EC78CAa3FC73d4Cc7E9302".lower(),
    "0x74b479868e3B8a21BDE4bb09F85177aCF9976A2d".lower()
}

def load_swaps():
    with open(SWAPS_FILE, "r") as f:
        data = json.load(f)
    # Sort by timestamp
    base = sorted(data.get("base", []), key=lambda x: x["ts"])
    bsc = sorted(data.get("bsc", []), key=lambda x: x["ts"])
    return base, bsc

def load_senders_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def load_db_txs():
    if not os.path.exists(DB_FILE):
        return set()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    txs = set()
    # arb_attempts
    try:
        cursor.execute("SELECT buy_tx_hash, sell_tx_hash FROM arb_attempts")
        for r in cursor.fetchall():
            if r[0]: txs.add(r[0].strip().lower())
            if r[1]: txs.add(r[1].strip().lower())
    except Exception:
        pass
    # arb_legs
    try:
        cursor.execute("SELECT tx_hash FROM arb_legs")
        for r in cursor.fetchall():
            if r[0]: txs.add(r[0].strip().lower())
    except Exception:
        pass
    # actions
    try:
        cursor.execute("SELECT tx_hash FROM actions")
        for r in cursor.fetchall():
            if r[0]: txs.add(r[0].strip().lower())
    except Exception:
        pass
    conn.close()
    # strip 0x prefix to match pool_swaps.json
    return {tx[2:] if tx.startswith("0x") else tx for tx in txs}

def filter_swaps(swaps, senders_cache, db_txs):
    filtered = []
    filtered_bot_count = 0
    for s in swaps:
        tx = s["tx"].lower()
        sender = senders_cache.get(tx)
        
        is_bot = False
        if sender and sender.lower() in BOT_WALLETS:
            is_bot = True
        if tx in db_txs or f"0x{tx}" in db_txs:
            is_bot = True
            
        if is_bot:
            filtered_bot_count += 1
        else:
            filtered.append(s)
    return filtered, filtered_bot_count

def get_price_at(swaps, t, strictly_before=False):
    ts_list = [s["ts"] for s in swaps]
    if strictly_before:
        idx = bisect.bisect_left(ts_list, t)
        if idx == 0:
            return swaps[0]["ngn_per_usd"]
        return swaps[idx-1]["ngn_per_usd"]
    else:
        idx = bisect.bisect_right(ts_list, t)
        if idx == 0:
            return swaps[0]["ngn_per_usd"]
        return swaps[idx-1]["ngn_per_usd"]

def calculate_returns(swaps):
    # Calculate return of each swap relative to the price before it
    # R = (P_curr - P_prev) / P_prev
    returns = []
    for i in range(len(swaps)):
        if i == 0:
            returns.append(0.0)
        else:
            prev_price = swaps[i-1]["ngn_per_usd"]
            curr_price = swaps[i]["ngn_per_usd"]
            returns.append((curr_price - prev_price) / prev_price)
    return returns

def sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)

def run_lead_lag(trigger_swaps, follow_swaps, trigger_returns, horizon_ms, max_t):
    triggers = []
    follows_mean_ratio = []
    follows_slopes_num = []
    follows_slopes_den = []
    
    reverts_mean_ratio = []
    reverts_slopes_num = []
    reverts_slopes_den = []
    
    for i in range(len(trigger_swaps)):
        s_trigger = trigger_swaps[i]
        r_trigger = trigger_returns[i]
        
        # We only look at meaningful moves (10+ bps)
        if abs(r_trigger) < 0.001:
            continue
            
        t_trigger = s_trigger["ts"]
        
        # Skip if too close to the end of data
        if t_trigger + horizon_ms > max_t:
            continue
            
        # Get prices for follow chain
        p_follow_before = get_price_at(follow_swaps, t_trigger, strictly_before=True)
        p_follow_after = get_price_at(follow_swaps, t_trigger + horizon_ms, strictly_before=False)
        r_follow = (p_follow_after - p_follow_before) / p_follow_before
        
        # Get prices for trigger chain itself (subsequent move)
        p_trigger_after = get_price_at(trigger_swaps, t_trigger + horizon_ms, strictly_before=False)
        p_trigger_curr = s_trigger["ngn_per_usd"]
        r_trigger_subsequent = (p_trigger_after - p_trigger_curr) / p_trigger_curr
        
        triggers.append(r_trigger)
        
        # Follow stats
        follows_mean_ratio.append(r_follow * sign(r_trigger))
        follows_slopes_num.append(r_follow * r_trigger)
        follows_slopes_den.append(r_trigger ** 2)
        
        # Reversion stats (subsequent return in trigger chain)
        reverts_mean_ratio.append(r_trigger_subsequent * sign(r_trigger))
        reverts_slopes_num.append(r_trigger_subsequent * r_trigger)
        reverts_slopes_den.append(r_trigger ** 2)

    n_triggers = len(triggers)
    if n_triggers == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
        
    avg_trigger_size = sum(abs(x) for x in triggers) / n_triggers
    
    # Follow stats
    follow_ratio = (sum(follows_mean_ratio) / n_triggers) / avg_trigger_size
    follow_slope = sum(follows_slopes_num) / sum(follows_slopes_den)
    
    # Reversion stats
    revert_ratio = (sum(reverts_mean_ratio) / n_triggers) / avg_trigger_size
    revert_slope = sum(reverts_slopes_num) / sum(reverts_slopes_den)
    
    return n_triggers, avg_trigger_size, follow_slope, follow_ratio, revert_slope

def analyze_scenario(base, bsc, scenario_name):
    # Calculate returns for each swap
    base_returns = calculate_returns(base)
    bsc_returns = calculate_returns(bsc)
    
    max_t = max(base[-1]["ts"], bsc[-1]["ts"])
    
    print(f"=== {scenario_name} ===")
    print(f"Base swaps: {len(base)}, BSC swaps: {len(bsc)}")
    
    for label, trigger_swaps, follow_swaps, trigger_returns in [
        ("Base -> BSC", base, bsc, base_returns),
        ("BSC -> Base", bsc, base, bsc_returns),
    ]:
        print(f"\nDirection: {label}")
        for min_horizon in [3, 30]:
            horizon_ms = min_horizon * 60 * 1000
            n, avg_sz, slope, ratio, rev_slope = run_lead_lag(
                trigger_swaps, follow_swaps, trigger_returns, horizon_ms, max_t
            )
            print(f"  Horizon {min_horizon}m:")
            print(f"    Triggers (>= 10 bps): {n}")
            if n > 0:
                print(f"    Avg trigger size: {avg_sz*10000:.2f} bps")
                print(f"    Follow slope (Beta): {slope*100:.2f}%")
                print(f"    Follow ratio (Mean Ratio): {ratio*100:.2f}%")
                print(f"    Trigger pool subsequent move (Beta): {rev_slope*100:.2f}%")

def main():
    base, bsc = load_swaps()
    senders_cache = load_senders_cache()
    db_txs = load_db_txs()
    
    print(f"Database tx hashes loaded: {len(db_txs)}")
    print(f"Senders cache size: {len(senders_cache)}")
    
    # Scenario 1: All swaps
    analyze_scenario(base, bsc, "Scenario A: All Trades")
    
    # Filter swaps
    base_filtered, base_bot_cnt = filter_swaps(base, senders_cache, db_txs)
    bsc_filtered, bsc_bot_cnt = filter_swaps(bsc, senders_cache, db_txs)
    
    print(f"\nFiltered out bot swaps: Base={base_bot_cnt}, BSC={bsc_bot_cnt}")
    
    # Scenario 2: Excluding bot trades
    analyze_scenario(base_filtered, bsc_filtered, "Scenario B: Excluding Bot Trades")

if __name__ == "__main__":
    main()
