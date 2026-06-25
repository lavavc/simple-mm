"""Paper-style LP episode reconstruction from V4 lifecycle ledger rows."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Deque, Sequence

from research.backtester.clmm_math import sqrt_price_x96_to_native_price, tick_to_sqrt_price_x96
from research.backtester.v4_export import POOL_CONFIGS, ExportPoolConfig
from research.backtester.v4_lp_ledger import OPENING_ATTRIBUTION_EXACT, LPLedgerRow

_EXACT_OPENING_ATTRIBUTION_STATUSES = {OPENING_ATTRIBUTION_EXACT, "fixture_exact"}
CLOSE_ATTRIBUTION_EXACT_COLLECT = "exact_collect"
CLOSE_ATTRIBUTION_SAME_TX_COLLECT = "same_tx_collect"
CLOSE_ATTRIBUTION_INTERIM_COLLECT = "interim_collect"
CLOSE_ATTRIBUTION_MIXED = "mixed_collect"
CLOSE_ATTRIBUTION_ZERO_COLLECT = "zero_collect_close"
CLOSE_ATTRIBUTION_NONE = "none"
_CLOSE_ATTRIBUTION_SOURCE_ORDER = (
    CLOSE_ATTRIBUTION_EXACT_COLLECT,
    CLOSE_ATTRIBUTION_SAME_TX_COLLECT,
    CLOSE_ATTRIBUTION_INTERIM_COLLECT,
)


@dataclass(frozen=True)
class PaperLPEpisode:
    pool: str
    lp_owner: str
    tick_lower: int
    tick_upper: int
    open_ms: int
    close_ms: int
    opening_capital: Decimal
    closing_capital: Decimal
    pnl: Decimal
    start_price: Decimal
    end_price: Decimal
    lower_price: Decimal
    upper_price: Decimal
    closed_liquidity: Decimal
    position_type: int | None
    delta_traversed: Decimal | None
    close_attribution_status: str = CLOSE_ATTRIBUTION_ZERO_COLLECT
    close_attribution_source: str = CLOSE_ATTRIBUTION_NONE


@dataclass(frozen=True)
class PaperEpisodeAttribution:
    episodes: list[PaperLPEpisode]
    unmatched_collect_rows: int
    unmatched_collect_capital: Decimal


@dataclass
class _OpenLot:
    pool: str
    lp_owner: str
    tick_lower: int
    tick_upper: int
    open_ms: int
    initial_liquidity: Decimal
    opening_capital: Decimal
    remaining_liquidity: Decimal
    opening_capital_remaining: Decimal
    closing_capital_realized: Decimal
    closing_attribution_sources: set[str]
    start_price: Decimal
    lower_price: Decimal
    upper_price: Decimal


def reconstruct_paper_episodes(rows: Sequence[LPLedgerRow]) -> list[PaperLPEpisode]:
    return analyze_paper_episode_attribution(rows).episodes


def analyze_paper_episode_attribution(rows: Sequence[LPLedgerRow]) -> PaperEpisodeAttribution:
    sorted_rows = sorted(rows, key=_row_sort_key)
    same_tx_collect_capital = _same_tx_zero_delta_collect_capital(sorted_rows)
    same_tx_close_liquidity = _same_tx_close_liquidity(sorted_rows)
    open_lots: dict[tuple[str, str, int, int], Deque[_OpenLot]] = {}
    episodes: list[PaperLPEpisode] = []
    unmatched_collect_rows = 0
    unmatched_collect_capital = Decimal("0")
    for row in sorted_rows:
        if row.liquidity_delta == 0:
            if (
                row.collect_amount0 != 0
                or row.collect_amount1 != 0
            ) and _tx_position_key(row) not in same_tx_close_liquidity:
                if not _apply_interim_collect_to_open_lots(row, open_lots):
                    unmatched_collect_rows += 1
                    unmatched_collect_capital += _capital_value(
                        row,
                        row.collect_amount0,
                        row.collect_amount1,
                    )
            continue
        owner = _required_owner(row)
        tick_lower = _required_tick(row.tick_lower, "tick_lower", row)
        tick_upper = _required_tick(row.tick_upper, "tick_upper", row)
        pool = _pool_name(row)
        key = (pool, owner, tick_lower, tick_upper)

        if row.liquidity_delta > 0:
            if row.amount_attribution_status not in _EXACT_OPENING_ATTRIBUTION_STATUSES:
                continue
            open_lots.setdefault(key, deque()).append(
                _open_lot(row, pool, owner, tick_lower, tick_upper)
            )
            continue

        lots = open_lots.get(key)
        if not lots:
            unmatched_capital = _unmatched_close_collect_capital(
                row,
                same_tx_collect_capital,
            )
            if unmatched_capital != 0:
                unmatched_collect_rows += 1
                unmatched_collect_capital += unmatched_capital
            continue
        observed_liquidity = sum((lot.remaining_liquidity for lot in lots), Decimal("0"))
        liquidity_to_close = min(Decimal(str(abs(row.liquidity_delta))), observed_liquidity)
        if liquidity_to_close <= 0:
            continue
        close_collects = _close_collect_sources(
            row,
            same_tx_collect_capital,
            same_tx_close_liquidity,
        )
        collect_capital = sum((capital for _source, capital in close_collects), Decimal("0"))
        remaining_to_close = liquidity_to_close
        while lots and remaining_to_close > 0:
            lot = lots[0]
            closed_liquidity = min(lot.remaining_liquidity, remaining_to_close)
            closes_entire_lot = closed_liquidity == lot.remaining_liquidity
            lot_fraction = closed_liquidity / lot.remaining_liquidity
            burn_fraction = closed_liquidity / liquidity_to_close
            opening_capital_consumed = lot.opening_capital_remaining * lot_fraction
            closing_capital_consumed = collect_capital * burn_fraction
            lot.closing_capital_realized += closing_capital_consumed
            if closing_capital_consumed != 0:
                lot.closing_attribution_sources.update(
                    source
                    for source, capital in close_collects
                    if capital != 0
                )
            lot.remaining_liquidity -= closed_liquidity
            lot.opening_capital_remaining -= opening_capital_consumed
            remaining_to_close -= closed_liquidity
            if closes_entire_lot:
                closing_capital = lot.closing_capital_realized
                pnl = closing_capital - lot.opening_capital
                delta_traversed = row.cngn_usd_price_at_event - lot.start_price
                close_status, close_source = _close_attribution(
                    lot.closing_attribution_sources
                )
                episodes.append(
                    PaperLPEpisode(
                        pool=lot.pool,
                        lp_owner=lot.lp_owner,
                        tick_lower=lot.tick_lower,
                        tick_upper=lot.tick_upper,
                        open_ms=lot.open_ms,
                        close_ms=row.timestamp_ms,
                        opening_capital=lot.opening_capital,
                        closing_capital=closing_capital,
                        pnl=pnl,
                        start_price=lot.start_price,
                        end_price=row.cngn_usd_price_at_event,
                        lower_price=lot.lower_price,
                        upper_price=lot.upper_price,
                        closed_liquidity=lot.initial_liquidity,
                        position_type=classify_position_type(
                            lot.start_price,
                            row.cngn_usd_price_at_event,
                            lot.lower_price,
                            lot.upper_price,
                            pnl,
                        ),
                        delta_traversed=delta_traversed,
                        close_attribution_status=close_status,
                        close_attribution_source=close_source,
                    )
                )
                lots.popleft()
        if not lots:
            del open_lots[key]
    return PaperEpisodeAttribution(
        episodes=episodes,
        unmatched_collect_rows=unmatched_collect_rows,
        unmatched_collect_capital=unmatched_collect_capital,
    )


def classify_position_type(
    start_price: Decimal,
    end_price: Decimal,
    lower: Decimal,
    upper: Decimal,
    pnl: Decimal,
) -> int:
    if start_price == end_price:
        if start_price < lower:
            return 14
        if start_price > upper:
            return 15
        return 13

    if start_price < end_price:
        if end_price <= lower:
            return 1
        if start_price < lower and end_price <= upper:
            return 3
        if lower <= start_price and end_price <= upper:
            return 5
        if lower <= start_price < upper < end_price:
            return 7
        if start_price < lower and upper < end_price:
            return 9
        if upper <= start_price:
            return 11

    if end_price < start_price:
        if start_price <= lower:
            return 2
        if end_price < lower and start_price <= upper:
            return 4
        if lower <= end_price and start_price <= upper:
            return 6
        if lower <= end_price < upper < start_price:
            return 8
        if end_price < lower and upper < start_price:
            return 10
        if upper <= end_price:
            return 12

    raise ValueError(
        "could not classify paper position type "
        f"start_price={start_price} end_price={end_price} lower={lower} upper={upper}"
    )


def paper_win_score(
    episodes: Sequence[PaperLPEpisode],
    start_ms: int,
    end_ms: int,
) -> Decimal:
    if end_ms <= start_ms:
        raise ValueError("end_ms must be greater than start_ms")

    deltas_by_time: dict[int, Decimal] = defaultdict(Decimal)
    for episode in episodes:
        deltas_by_time[episode.close_ms] += episode.pnl

    cumulative_pnl = sum(
        (pnl for close_ms, pnl in deltas_by_time.items() if close_ms <= start_ms),
        Decimal("0"),
    )
    states = [cumulative_pnl]
    intervals: list[tuple[int, Decimal]] = []
    previous_ms = start_ms
    for close_ms in sorted(time for time in deltas_by_time if start_ms < time < end_ms):
        intervals.append((close_ms - previous_ms, cumulative_pnl))
        cumulative_pnl += deltas_by_time[close_ms]
        states.append(cumulative_pnl)
        previous_ms = close_ms
    intervals.append((end_ms - previous_ms, cumulative_pnl))

    max_positive = max((max(state, Decimal("0")) for state in states), default=Decimal("0"))
    max_negative = max((-min(state, Decimal("0")) for state in states), default=Decimal("0"))
    normalizer = max(max_positive, max_negative)
    if normalizer == 0:
        return Decimal("0.5")

    positive_area = Decimal("0")
    negative_area = Decimal("0")
    for duration_ms, state in intervals:
        duration = Decimal(str(duration_ms))
        if state > 0:
            positive_area += duration * state / normalizer
        elif state < 0:
            negative_area += duration * (-state) / normalizer
    total_area = positive_area + negative_area
    if total_area == 0:
        return Decimal("0.5")
    return positive_area / total_area


def _open_lot(
    row: LPLedgerRow,
    pool: str,
    owner: str,
    tick_lower: int,
    tick_upper: int,
) -> _OpenLot:
    lower_price, upper_price = _price_bounds_for_row(row, tick_lower, tick_upper)
    opening_capital = _capital_value(row, row.amount0_actual, row.amount1_actual)
    return _OpenLot(
        pool=pool,
        lp_owner=owner,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        open_ms=row.timestamp_ms,
        initial_liquidity=Decimal(str(row.liquidity_delta)),
        opening_capital=opening_capital,
        remaining_liquidity=Decimal(str(row.liquidity_delta)),
        opening_capital_remaining=opening_capital,
        closing_capital_realized=Decimal("0"),
        closing_attribution_sources=set(),
        start_price=row.cngn_usd_price_at_event,
        lower_price=lower_price,
        upper_price=upper_price,
    )


def _same_tx_zero_delta_collect_capital(
    rows: Sequence[LPLedgerRow],
) -> dict[tuple[str, str, int, int, str], Decimal]:
    collect_capital: dict[tuple[str, str, int, int, str], Decimal] = defaultdict(Decimal)
    for row in rows:
        if row.liquidity_delta != 0:
            continue
        if row.collect_amount0 == 0 and row.collect_amount1 == 0:
            continue
        collect_capital[_tx_position_key(row)] += _capital_value(
            row,
            row.collect_amount0,
            row.collect_amount1,
        )
    return dict(collect_capital)


def _same_tx_close_liquidity(
    rows: Sequence[LPLedgerRow],
) -> dict[tuple[str, str, int, int, str], Decimal]:
    close_liquidity: dict[tuple[str, str, int, int, str], Decimal] = defaultdict(Decimal)
    for row in rows:
        if row.liquidity_delta >= 0:
            continue
        close_liquidity[_tx_position_key(row)] += Decimal(str(abs(row.liquidity_delta)))
    return dict(close_liquidity)


def _same_tx_collect_capital_for_close(
    row: LPLedgerRow,
    same_tx_collect_capital: dict[tuple[str, str, int, int, str], Decimal],
    same_tx_close_liquidity: dict[tuple[str, str, int, int, str], Decimal],
) -> Decimal:
    key = _tx_position_key(row)
    collect_capital = same_tx_collect_capital.get(key, Decimal("0"))
    if collect_capital == 0:
        return Decimal("0")
    close_liquidity = same_tx_close_liquidity[key]
    if close_liquidity <= 0:
        raise ValueError(f"non-positive same-tx close liquidity for token_id={row.token_id}")
    return collect_capital * Decimal(str(abs(row.liquidity_delta))) / close_liquidity


def _close_collect_sources(
    row: LPLedgerRow,
    same_tx_collect_capital: dict[tuple[str, str, int, int, str], Decimal],
    same_tx_close_liquidity: dict[tuple[str, str, int, int, str], Decimal],
) -> list[tuple[str, Decimal]]:
    sources: list[tuple[str, Decimal]] = []
    direct_collect_capital = _capital_value(row, row.collect_amount0, row.collect_amount1)
    if direct_collect_capital != 0:
        sources.append((CLOSE_ATTRIBUTION_EXACT_COLLECT, direct_collect_capital))
    same_tx_collect = _same_tx_collect_capital_for_close(
        row,
        same_tx_collect_capital,
        same_tx_close_liquidity,
    )
    if same_tx_collect != 0:
        sources.append((CLOSE_ATTRIBUTION_SAME_TX_COLLECT, same_tx_collect))
    return sources


def _unmatched_close_collect_capital(
    row: LPLedgerRow,
    same_tx_collect_capital: dict[tuple[str, str, int, int, str], Decimal],
) -> Decimal:
    return (
        _capital_value(row, row.collect_amount0, row.collect_amount1)
        + same_tx_collect_capital.get(
            _tx_position_key(row),
            Decimal("0"),
        )
    )


def _apply_interim_collect_to_open_lots(
    row: LPLedgerRow,
    open_lots: dict[tuple[str, str, int, int], Deque[_OpenLot]],
) -> bool:
    owner = _required_owner(row)
    tick_lower = _required_tick(row.tick_lower, "tick_lower", row)
    tick_upper = _required_tick(row.tick_upper, "tick_upper", row)
    pool = _pool_name(row)
    lots = open_lots.get((pool, owner, tick_lower, tick_upper))
    if not lots:
        return False
    collect_capital = _capital_value(row, row.collect_amount0, row.collect_amount1)
    observed_liquidity = sum((lot.remaining_liquidity for lot in lots), Decimal("0"))
    if observed_liquidity <= 0:
        raise ValueError(f"non-positive observed liquidity for collect token_id={row.token_id}")
    for lot in lots:
        collected_capital = collect_capital * lot.remaining_liquidity / observed_liquidity
        lot.closing_capital_realized += collected_capital
        if collected_capital != 0:
            lot.closing_attribution_sources.add(CLOSE_ATTRIBUTION_INTERIM_COLLECT)
    return True


def _close_attribution(sources: set[str]) -> tuple[str, str]:
    ordered_sources = [
        source
        for source in _CLOSE_ATTRIBUTION_SOURCE_ORDER
        if source in sources
    ]
    if not ordered_sources:
        return CLOSE_ATTRIBUTION_ZERO_COLLECT, CLOSE_ATTRIBUTION_NONE
    source_text = "|".join(ordered_sources)
    if len(ordered_sources) == 1:
        return ordered_sources[0], source_text
    return CLOSE_ATTRIBUTION_MIXED, source_text


def _tx_position_key(row: LPLedgerRow) -> tuple[str, str, int, int, str]:
    return (
        _pool_name(row),
        _required_owner(row),
        _required_tick(row.tick_lower, "tick_lower", row),
        _required_tick(row.tick_upper, "tick_upper", row),
        row.tx_hash.lower(),
    )


def _capital_value(row: LPLedgerRow, amount0: Decimal, amount1: Decimal) -> Decimal:
    config = _config_for_row(row)
    if config.token0_symbol == "cNGN":
        return amount0 * row.cngn_usd_price_at_event + amount1
    if config.token1_symbol == "cNGN":
        return amount0 + amount1 * row.cngn_usd_price_at_event
    raise ValueError(f"pool config does not identify cNGN side: {config.name}")


def _price_bounds_for_row(
    row: LPLedgerRow,
    tick_lower: int,
    tick_upper: int,
) -> tuple[Decimal, Decimal]:
    config = _config_for_row(row)
    lower = _cngn_price_from_tick(tick_lower, config)
    upper = _cngn_price_from_tick(tick_upper, config)
    return (lower, upper) if lower <= upper else (upper, lower)


def _cngn_price_from_tick(tick: int, config: ExportPoolConfig) -> Decimal:
    native_price = sqrt_price_x96_to_native_price(
        tick_to_sqrt_price_x96(tick),
        config.token0_decimals,
        config.token1_decimals,
    )
    if native_price <= 0:
        raise ValueError(f"non-positive native price for tick={tick} pool={config.name}")
    if not config.invert_price:
        return native_price
    with localcontext() as ctx:
        ctx.prec = 80
        return +(Decimal("1") / native_price)


def _config_for_row(row: LPLedgerRow) -> ExportPoolConfig:
    if row.pool_id:
        for config in POOL_CONFIGS.values():
            if config.pool_id.lower() == row.pool_id.lower():
                return config
    if row.chain:
        for config in POOL_CONFIGS.values():
            if config.chain == row.chain:
                return config
    raise ValueError(
        "cannot derive pool orientation without known chain or pool_id "
        f"for token_id={row.token_id}"
    )


def _range_bucket(price: Decimal, lower: Decimal, upper: Decimal) -> int:
    if price < lower:
        return 0
    if price > upper:
        return 2
    return 1


def _pool_name(row: LPLedgerRow) -> str:
    return _config_for_row(row).name


def _required_owner(row: LPLedgerRow) -> str:
    if row.lp_owner is None:
        raise ValueError(f"missing LP owner for token_id={row.token_id}")
    return row.lp_owner


def _required_tick(value: int | None, name: str, row: LPLedgerRow) -> int:
    if value is None:
        raise ValueError(f"missing {name} for token_id={row.token_id}")
    return value


def _row_sort_key(row: LPLedgerRow) -> tuple[int, int, int, int]:
    return row.timestamp_ms, row.block_number, row.log_index, row.event_order
