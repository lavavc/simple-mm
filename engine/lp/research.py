"""LP episode reconstruction and paper-style analytics from persisted engine data."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class PriceRangeBucket(str, Enum):
    BELOW = "below"
    IN_RANGE = "in_range"
    ABOVE = "above"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EpisodeSnapshot:
    timestamp_ms: int
    token_id: str
    tick_lower: int | None
    tick_upper: int | None
    range_min: Decimal | None
    range_max: Decimal | None
    current_price: Decimal | None
    price_position_fraction: Decimal | None
    in_range: bool | None
    position_value_usd: Decimal | None
    our_share_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class LPEpisode:
    token_id: str
    venue: str
    started_at_ms: int
    ended_at_ms: int | None
    closed: bool
    start_value_usd: Decimal | None
    end_value_usd: Decimal | None
    pnl_usd: Decimal | None
    pnl_return: Decimal | None
    win: bool | None
    duration_ms: int | None
    range_min: Decimal | None
    range_max: Decimal | None
    start_price: Decimal | None
    end_price: Decimal | None
    start_bucket: PriceRangeBucket
    end_bucket: PriceRangeBucket
    position_type: str
    start_fraction: Decimal | None
    end_fraction: Decimal | None
    delta_traversed: Decimal | None
    mean_active_share_pct: Decimal | None
    snapshots: tuple[EpisodeSnapshot, ...]


@dataclass(frozen=True, slots=True)
class LPResearchSummary:
    venue: str
    total_episodes: int
    closed_episodes: int
    profitable_episodes: int
    profitable_share: Decimal
    total_pnl_usd: Decimal
    avg_pnl_return: Decimal | None
    win_score: Decimal
    avg_delta_traversed: Decimal | None
    dominant_position_type: str | None
    type_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class LPResearchReport:
    summary: LPResearchSummary
    episodes: tuple[LPEpisode, ...]
    open_episode: LPEpisode | None


def _price_bucket(price: Decimal | None, a: Decimal | None, b: Decimal | None) -> PriceRangeBucket:
    if price is None or a is None or b is None:
        return PriceRangeBucket.UNKNOWN
    if price < a:
        return PriceRangeBucket.BELOW
    if price > b:
        return PriceRangeBucket.ABOVE
    return PriceRangeBucket.IN_RANGE


def _position_type(start: PriceRangeBucket, end: PriceRangeBucket) -> str:
    return f"{start.value}_to_{end.value}"


def _clip_fraction(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def _snapshot_from_row(row: dict[str, Any]) -> EpisodeSnapshot | None:
    lp = row.get("lp_position")
    if not lp or lp.get("token_id") is None:
        return None
    return EpisodeSnapshot(
        timestamp_ms=int(row["timestamp"]),
        token_id=str(lp["token_id"]),
        tick_lower=lp.get("tick_lower"),
        tick_upper=lp.get("tick_upper"),
        range_min=_to_decimal(lp.get("range_min")),
        range_max=_to_decimal(lp.get("range_max")),
        current_price=_to_decimal(lp.get("current_price")),
        price_position_fraction=_to_decimal(lp.get("price_position_fraction")),
        in_range=lp.get("in_range"),
        position_value_usd=_to_decimal(row.get("position_value_usd")),
        our_share_pct=_to_decimal(lp.get("our_share_pct")),
    )


def _group_snapshots_by_token(snapshot_rows: list[dict[str, Any]]) -> dict[str, list[EpisodeSnapshot]]:
    grouped: dict[str, list[EpisodeSnapshot]] = {}
    for row in snapshot_rows:
        snap = _snapshot_from_row(row)
        if snap is None:
            continue
        grouped.setdefault(snap.token_id, []).append(snap)
    return grouped


def _win_score(closed_episodes: list[LPEpisode]) -> Decimal:
    spans: list[tuple[int, int, Decimal]] = []
    cumulative = Decimal("0")
    for episode in closed_episodes:
        if episode.pnl_usd is None or episode.duration_ms is None or episode.duration_ms <= 0:
            continue
        start = episode.started_at_ms
        end = episode.ended_at_ms or episode.started_at_ms
        cumulative += episode.pnl_usd
        spans.append((start, end, cumulative))
    if not spans:
        return Decimal("0.5")
    total = sum(max(end - start, 0) for start, end, _ in spans)
    if total <= 0:
        return Decimal("0.5")
    positive = sum(max(end - start, 0) for start, end, cum in spans if cum > 0)
    return Decimal(positive) / Decimal(total)


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _episode_from_group(
    venue: str,
    token_id: str,
    snapshots: list[EpisodeSnapshot],
    removal_ts_ms: int | None,
) -> LPEpisode:
    ordered = sorted(snapshots, key=lambda item: item.timestamp_ms)
    first = ordered[0]
    last = ordered[-1]
    closed = removal_ts_ms is not None and removal_ts_ms >= last.timestamp_ms
    ended_at_ms = removal_ts_ms if closed else last.timestamp_ms
    start_value = first.position_value_usd
    end_value = last.position_value_usd
    pnl = None
    pnl_return = None
    if start_value is not None and end_value is not None:
        pnl = end_value - start_value
        if start_value > 0:
            pnl_return = pnl / start_value
    start_bucket = _price_bucket(first.current_price, first.range_min, first.range_max)
    end_bucket = _price_bucket(last.current_price, last.range_min, last.range_max)
    start_fraction = _clip_fraction(first.price_position_fraction)
    end_fraction = _clip_fraction(last.price_position_fraction)
    delta = None
    if start_fraction is not None and end_fraction is not None:
        delta = end_fraction - start_fraction
    mean_share = _average([s.our_share_pct for s in ordered if s.our_share_pct is not None])
    win = None if pnl is None else pnl >= 0
    duration_ms = None if ended_at_ms is None else max(ended_at_ms - first.timestamp_ms, 0)
    return LPEpisode(
        token_id=token_id,
        venue=venue,
        started_at_ms=first.timestamp_ms,
        ended_at_ms=ended_at_ms,
        closed=closed,
        start_value_usd=start_value,
        end_value_usd=end_value,
        pnl_usd=pnl,
        pnl_return=pnl_return,
        win=win,
        duration_ms=duration_ms,
        range_min=first.range_min,
        range_max=first.range_max,
        start_price=first.current_price,
        end_price=last.current_price,
        start_bucket=start_bucket,
        end_bucket=end_bucket,
        position_type=_position_type(start_bucket, end_bucket),
        start_fraction=start_fraction,
        end_fraction=end_fraction,
        delta_traversed=delta,
        mean_active_share_pct=mean_share,
        snapshots=tuple(ordered),
    )


def analyze_lp_strategy(
    venue: str,
    action_rows: list[dict[str, Any]],
    snapshot_rows: list[dict[str, Any]],
) -> LPResearchReport:
    removals: dict[str, int] = {}
    for row in action_rows:
        if row.get("status") != "confirmed" or row.get("action_type") not in {"remove_position", "manual_withdraw", "shutdown_unwind"}:
            continue
        metadata = row.get("metadata") or {}
        token_id = metadata.get("token_id")
        if token_id is not None:
            removals[str(token_id)] = int(row["timestamp"])

    episodes = [
        _episode_from_group(venue, token_id, snaps, removals.get(token_id))
        for token_id, snaps in _group_snapshots_by_token(snapshot_rows).items()
    ]
    episodes.sort(key=lambda item: item.started_at_ms)

    closed_episodes = [episode for episode in episodes if episode.closed]
    profitable = [episode for episode in closed_episodes if episode.win]
    type_counts: dict[str, int] = {}
    for episode in episodes:
        type_counts[episode.position_type] = type_counts.get(episode.position_type, 0) + 1
    dominant_type = max(type_counts.items(), key=lambda item: item[1])[0] if type_counts else None
    returns = [episode.pnl_return for episode in closed_episodes if episode.pnl_return is not None]
    deltas = [episode.delta_traversed for episode in profitable if episode.delta_traversed is not None]
    total_pnl = sum((episode.pnl_usd or Decimal("0")) for episode in closed_episodes)
    profitable_share = (
        Decimal(len(profitable)) / Decimal(len(closed_episodes))
        if closed_episodes
        else Decimal("0")
    )
    open_episode = next((episode for episode in reversed(episodes) if not episode.closed), None)
    summary = LPResearchSummary(
        venue=venue,
        total_episodes=len(episodes),
        closed_episodes=len(closed_episodes),
        profitable_episodes=len(profitable),
        profitable_share=profitable_share,
        total_pnl_usd=total_pnl,
        avg_pnl_return=_average([value for value in returns if value is not None]),
        win_score=_win_score(closed_episodes),
        avg_delta_traversed=_average([value for value in deltas if value is not None]),
        dominant_position_type=dominant_type,
        type_counts=type_counts,
    )
    return LPResearchReport(summary=summary, episodes=tuple(episodes), open_episode=open_episode)
