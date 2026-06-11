"""H7+H6 — Three-way comparison of BSC/Base walk-forward results.

Baselines:
- A: default gas + old ranking (existing CSVs in backtester/results/)
- B: calibrated gas + old ranking (h7/ — produced by in-flight pre-H6 run)
- C: calibrated gas + new H6 ranking (h7_h6/)
"""

from __future__ import annotations

import pandas as pd

A_DIR = "backtester/results"
B_DIR = "backtester/results/h7"
C_DIR = "backtester/results/h7_h6"


def summarise(path: str, label: str) -> dict:
    df = pd.read_csv(path)
    eligible = df[df["eligible"] == True] if "eligible" in df.columns else df.iloc[:0]
    positive = df[df["mean_validation_net_return"] > 0]
    fee_cost_col = "mean_validation_fee_to_tx_cost_ratio"
    fee_cost = df[fee_cost_col] if fee_cost_col in df.columns else None
    summary = {
        "label": label,
        "total_configs": len(df),
        "eligible_count": int(len(eligible)),
        "positive_mean_val_count": int(len(positive)),
        "max_mean_val_net_return": float(df["mean_validation_net_return"].max()),
        "median_mean_val_net_return": float(df["mean_validation_net_return"].median()),
        "min_mean_val_net_return": float(df["mean_validation_net_return"].min()),
        "mean_tx_cost_per_window": float(df["mean_validation_total_transaction_cost"].mean()),
        "mean_fees_per_window": float(df["mean_validation_total_fees"].mean()),
        "fee_cost_ratio_median": float(fee_cost.median()) if fee_cost is not None and len(fee_cost) > 0 else None,
        "best_config_idx": int(df["mean_validation_net_return"].idxmax()),
    }
    return summary


def top_configs(path: str, key_cols: list[str], n: int = 5) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df.nlargest(n, "mean_validation_net_return")[key_cols].reset_index(drop=True)


def main() -> None:
    print("BASELINES")
    print("  A = default gas, old ranking (existing CSVs)")
    print("  B = calibrated gas, old ranking (h7/)")
    print("  C = calibrated gas, new H6 ranking (h7_h6/)\n")

    pools_methods = [
        ("uni_bsc", "paper", "BSC Paper"),
        ("uni_bsc", "ewma", "BSC EWMA"),
        ("uni_base", "paper", "Base Paper"),
        ("uni_base", "ewma", "Base EWMA"),
    ]

    rows = []
    for pool, method, label in pools_methods:
        for baseline, directory, name_suffix in [
            ("A", A_DIR, "_swapwf_aggregate"),
            ("B", B_DIR, "_swapwf_calibrated_aggregate"),
            ("C", C_DIR, "_swapwf_aggregate"),
        ]:
            path = f"{directory}/{pool}_{method}{name_suffix}.csv"
            try:
                s = summarise(path, f"{label} ({baseline})")
                s["baseline"] = baseline
                s["pool_method"] = label
                rows.append(s)
            except FileNotFoundError:
                rows.append({"baseline": baseline, "pool_method": label, "label": "MISSING"})

    df = pd.DataFrame(rows)
    cols = ["pool_method", "baseline", "total_configs", "eligible_count", "positive_mean_val_count", "max_mean_val_net_return", "median_mean_val_net_return", "mean_tx_cost_per_window", "mean_fees_per_window", "fee_cost_ratio_median"]
    cols = [c for c in cols if c in df.columns]
    print(df[cols].to_string(index=False))
    print()

    # Top configs per pool/method per baseline
    paper_cols = ["fixed_width_pct", "center_mode", "harvest_upward_range_fraction", "profit_take_return", "stop_loss_return", "out_of_range_overshoot_fraction", "valid_window_count", "positive_window_rate", "mean_validation_net_return", "mean_validation_total_fees", "mean_validation_total_transaction_cost", "mean_validation_rebalance_count"]
    ewma_cols = ["sd_multiplier", "ewma_lambda", "downside_skew", "preemptive_rebalance", "rebalance_threshold_pct", "center_mode", "valid_window_count", "positive_window_rate", "mean_validation_net_return", "mean_validation_total_fees", "mean_validation_total_transaction_cost", "mean_validation_rebalance_count"]
    for pool, method, label in pools_methods:
        cols_ = paper_cols if method == "paper" else ewma_cols
        for baseline, directory, name_suffix in [
            ("A", A_DIR, "_swapwf_aggregate"),
            ("B", B_DIR, "_swapwf_calibrated_aggregate"),
            ("C", C_DIR, "_swapwf_aggregate"),
        ]:
            path = f"{directory}/{pool}_{method}{name_suffix}.csv"
            try:
                top = top_configs(path, cols_, n=3)
                print(f"\n--- {label} ({baseline}) top 3 by mean validation ---")
                print(top.to_string(index=False))
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
