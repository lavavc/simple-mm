"""H12 capacity-curve analysis: paired marginals, share collapse, analytic C*.

Consumes the artifact from research/scripts/h12_capital_sweep.py. Three outputs per
(pool, config):

1. Paired per-window marginal APR between adjacent capital levels —
   marginal value of the last dollar, hurdle-adjusted (pnl + idle credit),
   with a drop-one-window jackknife range on the mean.
2. Share-space view of the same marginals, so the two pools' curves can be
   compared in the coordinate where fee dilution lives.
3. Analytic C* = sqrt(pot_rate * D / hurdle) - D per window (pot and D
   backed out of a reference capital level), against the simulated argmax.
   Agreement validates the structural fee-share model; disagreement shows
   which cost terms it is missing.

Rows with mean_share_diluted above the validity ceiling are excluded from
inference and reported separately.

Usage: python research/scripts/h12_capacity_analysis.py [--artifact research/results/h12/capacity_curve.csv]
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict

HURDLE_APR = 0.0425
REFERENCE_CAPITAL = 1000.0  # level used to back out pot and D per window


def _excess_usd(row: dict) -> float:
    """Hurdle-adjusted dollar PnL: position PnL plus sGHO on the idle rest.

    The full-bankroll sGHO benchmark cancels in paired differences, so the
    marginal of this quantity is the marginal value of moving one more
    dollar out of sGHO into the position.
    """
    return float(row["pnl_usd"]) + float(row["idle_hurdle_credit_usd"])


def _jackknife_range(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        return (float("nan"), float("nan"))
    means = [statistics.fmean(values[:i] + values[i + 1:]) for i in range(len(values))]
    return min(means), max(means)


def main() -> None:
    parser = argparse.ArgumentParser(description="H12 capacity-curve analysis")
    parser.add_argument("--artifact", default="research/results/h12/capacity_curve.csv")
    args = parser.parse_args()

    with open(args.artifact, newline="") as handle:
        rows = list(csv.DictReader(handle))

    invalid = [r for r in rows if r["share_validity_ok"] == "False"]
    valid = [r for r in rows if r["share_validity_ok"] == "True"]
    print(f"{len(rows)} rows; {len(invalid)} excluded above the share validity ceiling")
    if invalid:
        by_cell = defaultdict(int)
        for r in invalid:
            by_cell[(r["pool"], r["config"], float(r["capital_usd"]))] += 1
        for (pool, config, capital), count in sorted(by_cell.items()):
            print(f"  excluded: {pool}/{config} @ ${capital:,.0f} ({count} windows)")

    grouped: dict[tuple[str, str], dict[float, dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))
    for r in valid:
        grouped[(r["pool"], r["config"])][float(r["capital_usd"])][int(r["window_index"])] = r

    for (pool, config), by_capital in sorted(grouped.items()):
        capitals = sorted(by_capital)
        print(f"\n=== {pool} / {config} ===")

        # Total hurdle-adjusted PnL per capital level (windows where the level is valid).
        print(f"{'capital':>9} {'windows':>7} {'mean excess $/win':>18} {'mean share':>10}")
        for capital in capitals:
            window_rows = by_capital[capital]
            excesses = [_excess_usd(r) for r in window_rows.values()]
            shares = [float(r["mean_share_diluted"]) for r in window_rows.values()]
            print(f"{capital:>9,.0f} {len(window_rows):>7} {statistics.fmean(excesses):>18.4f} {statistics.fmean(shares):>10.2%}")

        # Argmax only over capitals retaining most windows, on their common
        # window set — otherwise levels that lost windows to the validity
        # filter are compared on cherry-picked (usually quiet) windows.
        max_coverage = max(len(by_capital[c]) for c in capitals)
        comparable = [c for c in capitals if len(by_capital[c]) >= 0.8 * max_coverage]
        common_windows = set.intersection(*(set(by_capital[c]) for c in comparable))
        totals = {
            c: statistics.fmean(_excess_usd(by_capital[c][w]) for w in common_windows)
            for c in comparable
        }
        argmax_capital = max(totals, key=lambda c: totals[c])
        dropped = [c for c in capitals if c not in comparable]
        print(
            f"simulated argmax: ${argmax_capital:,.0f} "
            f"(over {len(common_windows)} common windows; "
            f"dropped sparse levels: {', '.join(f'${c:,.0f}' for c in dropped) or 'none'})"
        )

        # Paired per-window marginal APR between adjacent capital levels.
        print(f"\n{'pair':>16} {'windows':>7} {'mean mAPR':>10} {'median':>8} {'pos%':>6} {'jackknife mean range':>22} {'mid share':>9}")
        for low, high in zip(capitals, capitals[1:]):
            common = sorted(set(by_capital[low]) & set(by_capital[high]))
            if not common:
                continue
            marginals = []
            mid_shares = []
            for w in common:
                row_low, row_high = by_capital[low][w], by_capital[high][w]
                years = float(row_high["duration_years"])
                if years <= 0:
                    continue
                marginals.append((_excess_usd(row_high) - _excess_usd(row_low)) / (high - low) / years)
                mid_shares.append((float(row_low["mean_share_diluted"]) + float(row_high["mean_share_diluted"])) / 2)
            if not marginals:
                continue
            jk_lo, jk_hi = _jackknife_range(marginals)
            print(
                f"{f'{low:,.0f}->{high:,.0f}':>16} {len(marginals):>7} "
                f"{statistics.fmean(marginals):>10.2%} {statistics.median(marginals):>8.2%} "
                f"{sum(1 for m in marginals if m > 0) / len(marginals):>6.0%} "
                f"{f'[{jk_lo:+.2%}, {jk_hi:+.2%}]':>22} "
                f"{statistics.fmean(mid_shares):>9.2%}"
            )

        # Analytic C* from the reference capital level.
        reference = by_capital.get(REFERENCE_CAPITAL, {})
        c_stars = []
        for r in reference.values():
            share = float(r["mean_share_diluted"])
            fees = float(r["fees_usd"])
            years = float(r["duration_years"])
            if share <= 0 or years <= 0 or fees <= 0:
                continue
            pot_rate = fees / share / years  # $/yr of fees available over our range
            depth = REFERENCE_CAPITAL * (1 - share) / share  # competing depth in $
            c_star = math.sqrt(pot_rate * depth / HURDLE_APR) - depth
            c_stars.append(max(c_star, 0.0))
        if c_stars:
            print(
                f"\nanalytic C* (ref ${REFERENCE_CAPITAL:,.0f}): "
                f"median ${statistics.median(c_stars):,.0f}, "
                f"IQR ${statistics.quantiles(c_stars, n=4)[0]:,.0f}-${statistics.quantiles(c_stars, n=4)[2]:,.0f} "
                f"({len(c_stars)} windows) vs simulated argmax ${argmax_capital:,.0f}"
            )
        else:
            print("\nanalytic C*: no usable windows at the reference capital (no entries or zero fees)")


if __name__ == "__main__":
    main()
