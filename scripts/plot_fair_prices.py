"""Plot MarketFairPrice, ExecutableFairPrice, and StrategyFairPrice over historical data.

Replays a V4 or legacy CSV event file through the three-tier fair price pipeline and
produces a 3-panel matplotlib chart:

  Panel 1: All three cNGN/USD prices on a shared timeline
  Panel 2: DEX imbalance signal and inventory skew_bps
  Panel 3: net_cNGN holding over time (from balance history CSV if provided)

Usage:
    # V4 format (uni-base, uni-bsc):
    python scripts/plot_fair_prices.py \
        --csv data/uni_base_v4.csv \
        --format v4 \
        [--balance-csv data/cngn_balance_history.csv] \
        [--out fair_prices.png]

    # Legacy format (Aerodrome / Pancakeswap):
    python scripts/plot_fair_prices.py \
        --csv "data/Aerodrome&Pancakeswap_HistoricalData.csv" \
        --format legacy \
        [--balance-csv data/cngn_balance_history.csv] \
        [--out fair_prices.png]
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

from backtester.data import (
    SwapEvent,
    load_events,
    load_v4_events,
    V4Event,
    LegacyEvent,
)
from engine.types import PriceQuote
from engine.market.price_aggregation import NormalizedPrice
from engine.market.venue_prices import VenuePrice
from engine.market.fair_price import (
    MarketFairPrice,
    MarketFairPriceCalculator,
    StrategyPriceCalculator,
)


# ---------------------------------------------------------------------------
# Balance history loader
# ---------------------------------------------------------------------------


class BalanceSeries:
    """Interpolates net_cNGN at any timestamp from a sorted balance CSV."""

    def __init__(self, rows: list[tuple[datetime, float, str]]) -> None:
        # rows: (block_time, balance_cngn, chain)
        self._rows = sorted(rows, key=lambda r: r[0])

    @classmethod
    def from_csv(cls, path: str) -> "BalanceSeries":
        rows = []
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.fromisoformat(row["block_time"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                rows.append((dt, float(row["balance_cngn"]), row["chain"]))
        return cls(rows)

    def balance_at(self, t: datetime, chain: str | None = None) -> float:
        """Return balance at or just before time t (step-function interpolation)."""
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        result = 0.0
        for dt, bal, c in self._rows:
            if chain and c != chain:
                continue
            if dt <= t:
                result = bal
            else:
                break
        return result

    def __bool__(self) -> bool:
        return bool(self._rows)


# ---------------------------------------------------------------------------
# Imbalance tracker (rolling window of last N swap directions)
# ---------------------------------------------------------------------------


class ImbalanceTracker:
    """Rolling DEX imbalance from recent swap directions.

    +1 = cNGN bought (buy-side pressure)
    -1 = cNGN sold   (sell-side pressure)
    Returns a value in [-1, 1].
    """

    def __init__(self, window: int = 20) -> None:
        self._signals: deque[float] = deque(maxlen=window)

    def update(self, cngn_bought: bool) -> float:
        self._signals.append(1.0 if cngn_bought else -1.0)
        return self.value

    @property
    def value(self) -> float:
        if not self._signals:
            return 0.0
        return sum(self._signals) / len(self._signals)


# ---------------------------------------------------------------------------
# Helpers: build NormalizedPrice / VenuePrice stubs from a pool price
# ---------------------------------------------------------------------------

REPLAY_VENUE = "replay-pool"
CNGN_USD_PAIR = "cNGN/USDC"


def _make_normalized(price_usd: float, amount_usd: float) -> NormalizedPrice:
    p = Decimal(str(price_usd))
    vol = Decimal(str(max(amount_usd, 0.0)))
    quote = PriceQuote(
        source=REPLAY_VENUE,
        timestamp=0,
        bid=p,
        ask=p,
        mid=p,
    )
    return NormalizedPrice(
        venue=REPLAY_VENUE,
        cngn_usd=p,
        raw_quote=quote,
        basis=CNGN_USD_PAIR,
        timestamp=0,
        volume_24h_usd=vol if vol > 0 else None,
    )


def _make_venue_price(price_usd: float) -> VenuePrice:
    import time as _time
    p = Decimal(str(price_usd))
    quote = PriceQuote(
        source=REPLAY_VENUE,
        timestamp=int(_time.time() * 1000),
        bid=p,
        ask=p,
        mid=p,
    )
    return VenuePrice(
        venue=REPLAY_VENUE,
        pair=CNGN_USD_PAIR,
        quote=quote,
        fetched_at=_time.time(),  # age = 0 → recency weight = 1.0
    )


# ---------------------------------------------------------------------------
# Tier 2: microprice computation (inline, no pool cache dependency)
# ---------------------------------------------------------------------------


def _executable_price(market_price: float, imbalance: float, fee_rate: float) -> float:
    """Stoikov microprice: market_price + (spread/2) * imbalance.

    Uses pool fee rate as the spread proxy (same as production spread_quality fallback).
    """
    half_spread = fee_rate  # fee_rate is already a fraction (e.g. 0.0015 = 15 bps)
    return market_price * (1.0 + half_spread * imbalance)


# ---------------------------------------------------------------------------
# Main replay loop
# ---------------------------------------------------------------------------


class Frame(NamedTuple):
    ts: datetime
    pool_price: float
    market_price: float
    executable_price: float
    strategy_price: float
    imbalance: float
    skew_bps: float
    net_cngn: float


def replay(
    events: list[LegacyEvent | V4Event],
    balance_series: BalanceSeries | None,
    target_cngn: float = 0.0,
    max_scale: float = 1_000_000.0,
    beta_bps: float = 10.0,
    max_skew_bps: float = 20.0,
) -> list[Frame]:
    market_calc = MarketFairPriceCalculator(pool_addresses={})
    strategy_calc = StrategyPriceCalculator(
        target_cngn=Decimal(str(target_cngn)),
        max_scale=Decimal(str(max_scale)),
        beta_bps=beta_bps,
        max_skew_bps=max_skew_bps,
    )
    imbalance_tracker = ImbalanceTracker(window=20)
    frames: list[Frame] = []

    for event in events:
        # Only plot swap events
        if isinstance(event, V4Event):
            if event.event_type != "swap":
                continue
            price = event.cngn_usd_price
            amount_usd = event.amount_usd
            fee_rate = event.fee_rate
            # On BSC pancakeswap, token0=cNGN; amount0 negative = cNGN leaving = sold
            # On Base, token0=cNGN; amount0 negative = cNGN leaving = sold
            cngn_bought = event.amount0 < 0   # pool sold cNGN → external buyer bought it
            chain = event.chain
        elif isinstance(event, SwapEvent):
            if event.cngn_usd_price <= 0:
                continue
            price = event.cngn_usd_price
            amount_usd = event.amount_usd
            fee_rate = 0.0015   # pancakeswap BSC default: 15 bps
            cngn_bought = event.token_bought_symbol.upper() == "CNGN"
            chain = getattr(event, "blockchain", "bsc")
        else:
            continue

        if price <= 0:
            continue

        # Tier 1: MarketFairPrice
        np_ = _make_normalized(price, amount_usd)
        vp = _make_venue_price(price)
        try:
            mfp = market_calc.compute(
                {REPLAY_VENUE: np_},
                {REPLAY_VENUE: vp},
            )
        except ValueError:
            continue

        # Tier 2: ExecutableFairPrice (inline microprice)
        imbalance = imbalance_tracker.update(cngn_bought)
        exec_price = _executable_price(float(mfp.price), imbalance, fee_rate)

        # Tier 3: StrategyFairPrice
        net_cngn = 0.0
        if balance_series:
            ts = event.block_time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            net_cngn = balance_series.balance_at(ts, chain=chain)

        # Wrap exec_price in a MarketFairPrice so StrategyPriceCalculator can consume it
        mfp_for_strategy = MarketFairPrice(
            price=Decimal(str(exec_price)),
            weights=mfp.weights,
            confidence=mfp.confidence,
            timestamp=mfp.timestamp,
        )
        sfp = strategy_calc.compute(mfp_for_strategy, net_cngn=Decimal(str(net_cngn)))

        ts = event.block_time
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        frames.append(Frame(
            ts=ts,
            pool_price=price,
            market_price=float(mfp.price),
            executable_price=exec_price,
            strategy_price=float(sfp.price),
            imbalance=imbalance,
            skew_bps=float(sfp.skew_bps),
            net_cngn=net_cngn,
        ))

    return frames


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------


def _clip_percentile(values: list[float], lo: float = 0.5, hi: float = 99.5) -> tuple[float, float]:
    """Return (low, high) axis limits based on percentiles, with 5% headroom."""
    sorted_v = sorted(v for v in values if v > 0)
    if not sorted_v:
        return 0.0, 1.0
    n = len(sorted_v)
    lo_idx = max(0, int(n * lo / 100))
    hi_idx = min(n - 1, int(n * hi / 100))
    lo_v, hi_v = sorted_v[lo_idx], sorted_v[hi_idx]
    pad = (hi_v - lo_v) * 0.05 or hi_v * 0.05
    return max(0.0, lo_v - pad), hi_v + pad


def plot(frames: list[Frame], out_path: str, title_suffix: str = "") -> None:
    if not frames:
        print("No swap events to plot.", file=sys.stderr)
        sys.exit(1)

    ts_list    = [f.ts for f in frames]
    pool_px    = [f.pool_price for f in frames]
    market_px  = [f.market_price for f in frames]
    exec_px    = [f.executable_price for f in frames]
    strat_px   = [f.strategy_price for f in frames]
    imbalance  = [f.imbalance for f in frames]
    skew_bps   = [f.skew_bps for f in frames]
    net_cngn   = [f.net_cngn for f in frames]

    has_inventory = any(v != 0.0 for v in net_cngn)
    n_panels = 3 if has_inventory else 2
    height_ratios = [3, 1, 1] if has_inventory else [3, 1]

    fig, axes = plt.subplots(
        n_panels, 1,
        figsize=(14, 8 if has_inventory else 6),
        sharex=True,
        gridspec_kw={"height_ratios": height_ratios},
    )
    axes_list = list(axes) if hasattr(axes, "__iter__") else [axes]
    while len(axes_list) < 3:
        axes_list.append(None)
    ax1, ax2, ax3 = axes_list[0], axes_list[1], axes_list[2]

    # -- Panel 1: Prices --
    # Clip y-axis to 99.5th percentile to suppress data spikes
    y_lo, y_hi = _clip_percentile(pool_px + market_px + exec_px + strat_px)
    ax1.plot(ts_list, pool_px,   color="#cccccc", linewidth=0.5, label="Pool price (raw)", zorder=1)
    ax1.plot(ts_list, market_px, color="#2196F3", linewidth=1.2, label="MarketFairPrice (Tier 1)", zorder=2)
    ax1.plot(ts_list, exec_px,   color="#FF9800", linewidth=1.0, label="ExecutableFairPrice (Tier 2)", zorder=3)
    ax1.plot(ts_list, strat_px,  color="#F44336", linewidth=1.2, label="StrategyFairPrice (Tier 3)", zorder=4)
    ax1.set_ylim(y_lo, y_hi)
    ax1.set_ylabel("cNGN/USD", fontsize=9)
    ax1.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.6f"))
    ax1.legend(loc="upper left", fontsize=7, framealpha=0.8)
    ax1.set_title(f"Fair Price Pipeline — Historical Replay{title_suffix}", fontsize=11)
    ax1.grid(axis="y", alpha=0.25)

    # -- Panel 2: Imbalance (rolling line) + skew_bps (right axis) --
    # Use a step/line rather than bars — readable at any time scale
    ax2.plot(ts_list, imbalance, color="#4CAF50", linewidth=0.7, label="DEX imbalance (rolling-20)")
    ax2.axhline(0, color="#4CAF50", linewidth=0.4, linestyle="--", alpha=0.5)
    ax2.fill_between(ts_list, imbalance, 0,
                     where=[v >= 0 for v in imbalance], color="#4CAF50", alpha=0.15)
    ax2.fill_between(ts_list, imbalance, 0,
                     where=[v < 0 for v in imbalance],  color="#F44336",  alpha=0.15)
    ax2r = ax2.twinx()
    ax2r.plot(ts_list, skew_bps, color="#9C27B0", linewidth=0.8, alpha=0.7, label="Skew bps")
    ax2r.axhline(0, color="#9C27B0", linewidth=0.3, linestyle="--", alpha=0.4)
    ax2.set_ylabel("Imbalance", fontsize=8)
    ax2r.set_ylabel("Skew (bps)", fontsize=8, color="#9C27B0")
    ax2.set_ylim(-1.2, 1.2)
    ax2.legend(loc="upper left", fontsize=7)
    ax2r.legend(loc="upper right", fontsize=7)
    ax2.grid(axis="y", alpha=0.2)

    # -- Panel 3: Net cNGN inventory --
    if ax3 is not None and has_inventory:
        ax3.fill_between(ts_list, net_cngn, 0, alpha=0.35,
                         color="#795548", label="net cNGN holding")
        ax3.axhline(0, color="#795548", linewidth=0.7, linestyle="--")
        ax3.set_ylabel("cNGN", fontsize=8)
        ax3.yaxis.set_major_formatter(mticker.EngFormatter())
        ax3.legend(loc="upper left", fontsize=7)
        ax3.grid(axis="y", alpha=0.2)

    # Shared x-axis formatting
    bottom_ax = ax3 if (has_inventory and ax3 is not None) else ax2
    bottom_ax.set_xlabel("Time (UTC)", fontsize=9)
    bottom_ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%Y"))
    bottom_ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=0, ha="center")

    fig.tight_layout(h_pad=0.5)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved → {out_path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv",     required=True, help="Path to event CSV")
    ap.add_argument("--format",  choices=["v4", "legacy"], default="legacy",
                    help="CSV format: v4 (Uniswap v4 export) or legacy (Aerodrome/Pancakeswap)")
    ap.add_argument("--pool",    default=None, help="Filter to specific pool_address or pool_id")
    ap.add_argument("--balance-csv", default=None,
                    help="Optional CSV from fetch_cngn_balance_history.py for Tier 3 inventory")
    ap.add_argument("--target-cngn",  type=float, default=0.0,
                    help="Target cNGN inventory (flat = 0)")
    ap.add_argument("--max-scale",    type=float, default=1_000_000.0,
                    help="Max cNGN inventory scale for A-S skew normalization")
    ap.add_argument("--beta-bps",     type=float, default=10.0,
                    help="Avellaneda-Stoikov β coefficient (bps per unit imbalance)")
    ap.add_argument("--max-skew-bps", type=float, default=20.0,
                    help="Maximum inventory skew cap (bps)")
    ap.add_argument("--out", default="fair_prices.png", help="Output PNG path")
    args = ap.parse_args()

    print(f"Loading events from {args.csv} (format={args.format})…")
    events: list[LegacyEvent | V4Event]
    if args.format == "v4":
        events = list(load_v4_events(args.csv, pool_id=args.pool))
    else:
        events = list(load_events(args.csv, pool_address=args.pool))
    print(f"  {len(events)} events loaded.")

    balance_series: BalanceSeries | None = None
    if args.balance_csv:
        print(f"Loading balance history from {args.balance_csv}…")
        balance_series = BalanceSeries.from_csv(args.balance_csv)
        print(f"  {len(balance_series._rows)} balance snapshots.")

    print("Replaying fair price pipeline…")
    frames = replay(
        events,
        balance_series,
        target_cngn=args.target_cngn,
        max_scale=args.max_scale,
        beta_bps=args.beta_bps,
        max_skew_bps=args.max_skew_bps,
    )
    print(f"  {len(frames)} swap frames computed.")

    if not frames:
        print("No frames to plot — check CSV format or pool filter.", file=sys.stderr)
        sys.exit(1)

    date_range = f" ({frames[0].ts.strftime('%Y-%m-%d')} → {frames[-1].ts.strftime('%Y-%m-%d')})"
    plot(frames, args.out, title_suffix=date_range)


if __name__ == "__main__":
    main()
