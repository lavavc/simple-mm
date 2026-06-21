"""Paper-style LP episode reconstruction from V4 lifecycle ledger rows."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Deque, Sequence

from backtester.clmm_math import sqrt_price_x96_to_native_price, tick_to_sqrt_price_x96
from backtester.v4_export import ExportPoolConfig, POOL_CONFIGS
from backtester.v4_lp_ledger import LPLedgerRow


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
    start_price: Decimal
    lower_price: Decimal
    upper_price: Decimal


def reconstruct_paper_episodes(rows: Sequence[LPLedgerRow]) -> list[PaperLPEpisode]:
    open_lots: dict[tuple[str, str, int, int], Deque[_OpenLot]] = {}
    episodes: list[PaperLPEpisode] = []
    for row in sorted(rows, key=_row_sort_key):
        if row.liquidity_delta == 0:
            continue
        owner = _required_owner(row)
        tick_lower = _required_tick(row.tick_lower, "tick_lower", row)
        tick_upper = _required_tick(row.tick_upper, "tick_upper", row)
        pool = _pool_name(row)
        key = (pool, owner, tick_lower, tick_upper)

        if row.liquidity_delta > 0:
            open_lots.setdefault(key, deque()).append(_open_lot(row, pool, owner, tick_lower, tick_upper))
            continue

        lots = open_lots.get(key)
        if not lots:
            continue
        observed_liquidity = sum((lot.remaining_liquidity for lot in lots), Decimal("0"))
        liquidity_to_close = min(Decimal(str(abs(row.liquidity_delta))), observed_liquidity)
        if liquidity_to_close <= 0:
            continue
        collect_capital = _capital_value(row, row.collect_amount0, row.collect_amount1)
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
            lot.remaining_liquidity -= closed_liquidity
            lot.opening_capital_remaining -= opening_capital_consumed
            remaining_to_close -= closed_liquidity
            if closes_entire_lot:
                closing_capital = lot.closing_capital_realized
                pnl = closing_capital - lot.opening_capital
                delta_traversed = row.cngn_usd_price_at_event - lot.start_price
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
                    )
                )
                lots.popleft()
        if not lots:
            del open_lots[key]
    return episodes


def classify_position_type(
    start_price: Decimal,
    end_price: Decimal,
    lower: Decimal,
    upper: Decimal,
    pnl: Decimal,
) -> int:
    start_bucket = _range_bucket(start_price, lower, upper)
    end_bucket = _range_bucket(end_price, lower, upper)
    if start_bucket != 1 and end_bucket == 1:
        return 3
    if start_bucket == 1 and end_bucket != 1:
        return 4
    if start_bucket == 1 and end_bucket == 1:
        return 5 if pnl >= 0 else 6
    if start_bucket == end_bucket:
        return 1 if start_bucket < 1 else 2
    return 7


def paper_win_score(
    episodes: Sequence[PaperLPEpisode],
    start_ms: int,
    end_ms: int,
) -> Decimal:
    if end_ms <= start_ms:
        raise ValueError("end_ms must be greater than start_ms")
    total_ms = Decimal(str(end_ms - start_ms))
    winning_ms = Decimal("0")
    for episode in episodes:
        overlap_start = max(start_ms, episode.open_ms)
        overlap_end = min(end_ms, episode.close_ms)
        if overlap_end <= overlap_start:
            continue
        if episode.pnl > 0:
            winning_ms += Decimal(str(overlap_end - overlap_start))
    return winning_ms / total_ms


def _open_lot(
    row: LPLedgerRow,
    pool: str,
    owner: str,
    tick_lower: int,
    tick_upper: int,
) -> _OpenLot:
    lower_price, upper_price = _price_bounds_for_row(row, tick_lower, tick_upper)
    opening_capital = _capital_value(row, row.amount0, row.amount1)
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
        start_price=row.cngn_usd_price_at_event,
        lower_price=lower_price,
        upper_price=upper_price,
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
