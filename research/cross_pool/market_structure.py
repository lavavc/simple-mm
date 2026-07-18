"""Post-hoc venue activity and anonymized LP-capital diagnostics."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Mapping, Sequence

from research.backtester.lp_ledger_attribution import (
    LedgerAttributionRow,
    load_ledger_attribution_rows,
    pool_attribution_orientation,
)
from research.cross_pool.contracts import (
    CrossPoolContractError,
    PoolName,
)

_MEANINGFUL_MOVE_BPS = Decimal("10")
_RATIO_PRECISION = 28
_CALCULATION_PRECISION = 60
_POOL_ORDER: tuple[PoolName, ...] = ("uni-base", "uni-bsc")
_SUPPORTED_EVENT_TYPES = frozenset(("initialize", "swap", "mint", "burn", "collect"))
_REPLAY_REQUIRED_FIELDS = frozenset(
    (
        "block_time",
        "chain",
        "pool_id",
        "event_type",
        "tx_hash",
        "log_index",
        "block_number",
        "sqrt_price_x96",
        "active_liquidity",
        "fee_rate",
        "amount_usd",
        "token0_symbol",
        "token1_symbol",
    )
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_Q96 = 2**96


@dataclass(frozen=True)
class DistributionSummary:
    count: int
    minimum: Decimal
    median: Decimal
    p95: Decimal
    p99: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        _require_positive_int(self.count, "distribution count")
        values = (
            self.minimum,
            self.median,
            self.p95,
            self.p99,
            self.maximum,
        )
        for value in values:
            _require_finite_decimal(value, "distribution value")
        if self.minimum < 0:
            raise CrossPoolContractError("distribution values must be nonnegative")
        if tuple(sorted(values)) != values:
            raise CrossPoolContractError(
                "distribution values must be ordered minimum through maximum"
            )


@dataclass(frozen=True)
class VenueStructureSummary:
    pool: PoolName
    activity_start_timestamp_ms: int
    activity_end_timestamp_ms: int
    ledger_cutoff_timestamp_ms: int
    swap_count: int
    meaningful_move_count: int
    fee_rate: Decimal
    update_gaps_ms: DistributionSummary
    active_liquidity: DistributionSummary
    volume_usd: DistributionSummary
    total_volume_usd: Decimal
    ledger_rows: int
    opening_count: int
    exact_opening_count: int
    ambiguous_opening_count: int
    known_owner_count: int
    exact_unknown_owner_opening_count: int
    exact_opening_capital_usd: Decimal
    exact_known_owner_capital_usd: Decimal
    exact_known_owner_capital_coverage: Decimal
    ambiguous_opening_liquidity_share: Decimal
    exact_opening_capital_top_owner_share: Decimal
    exact_opening_capital_top_three_share: Decimal
    exact_opening_capital_hhi: Decimal

    def __post_init__(self) -> None:
        if self.pool not in _POOL_ORDER:
            raise CrossPoolContractError("market structure requires a supported pool")
        _require_positive_int(
            self.activity_start_timestamp_ms,
            "activity start timestamp",
        )
        _require_positive_int(
            self.activity_end_timestamp_ms,
            "activity end timestamp",
        )
        if self.activity_start_timestamp_ms > self.activity_end_timestamp_ms:
            raise CrossPoolContractError("market structure activity interval is empty")
        if self.ledger_cutoff_timestamp_ms != self.activity_end_timestamp_ms:
            raise CrossPoolContractError(
                "market structure ledger cutoff must equal the activity end"
            )
        _require_positive_int(self.swap_count, "market structure swap_count")
        _require_nonnegative_int(
            self.meaningful_move_count,
            "market structure meaningful_move_count",
        )
        if self.meaningful_move_count > self.swap_count - 1:
            raise CrossPoolContractError("meaningful_move_count exceeds consecutive swap pairs")
        _require_finite_decimal(self.fee_rate, "market structure fee_rate")
        if not Decimal("0") < self.fee_rate < Decimal("1"):
            raise CrossPoolContractError("market structure fee_rate must be in (0, 1)")
        if self.update_gaps_ms.count != self.swap_count - 1:
            raise CrossPoolContractError("update-gap count must equal swap_count minus one")
        if self.active_liquidity.count != self.swap_count:
            raise CrossPoolContractError("active-liquidity count must equal swap_count")
        if self.volume_usd.count != self.swap_count:
            raise CrossPoolContractError("volume count must equal swap_count")
        _require_nonnegative_decimal(
            self.total_volume_usd,
            "market structure total_volume_usd",
        )
        for count_value, label in (
            (self.ledger_rows, "ledger_rows"),
            (self.opening_count, "opening_count"),
            (self.exact_opening_count, "exact_opening_count"),
            (self.ambiguous_opening_count, "ambiguous_opening_count"),
            (self.known_owner_count, "known_owner_count"),
            (
                self.exact_unknown_owner_opening_count,
                "exact_unknown_owner_opening_count",
            ),
        ):
            _require_nonnegative_int(count_value, f"market structure {label}")
        if self.ledger_rows < self.opening_count:
            raise CrossPoolContractError("ledger_rows cannot be below opening_count")
        if self.opening_count != (self.exact_opening_count + self.ambiguous_opening_count):
            raise CrossPoolContractError("opening_count must equal exact plus ambiguous openings")
        if self.exact_unknown_owner_opening_count > self.exact_opening_count:
            raise CrossPoolContractError("unknown exact openings cannot exceed exact openings")
        if self.known_owner_count <= 0:
            raise CrossPoolContractError(
                "market structure requires a positive exact known-owner count"
            )
        _require_positive_decimal(
            self.exact_opening_capital_usd,
            "market structure exact opening capital",
        )
        _require_positive_decimal(
            self.exact_known_owner_capital_usd,
            "market structure exact known-owner capital",
        )
        if self.exact_known_owner_capital_usd > self.exact_opening_capital_usd:
            raise CrossPoolContractError(
                "known-owner capital cannot exceed all exact opening capital"
            )
        for ratio_value, label in (
            (
                self.exact_known_owner_capital_coverage,
                "exact known-owner capital coverage",
            ),
            (
                self.ambiguous_opening_liquidity_share,
                "ambiguous opening liquidity share",
            ),
            (
                self.exact_opening_capital_top_owner_share,
                "top-owner capital share",
            ),
            (
                self.exact_opening_capital_top_three_share,
                "top-three capital share",
            ),
            (self.exact_opening_capital_hhi, "opening-capital HHI"),
        ):
            _require_unit_interval(ratio_value, label)
        if self.exact_known_owner_capital_coverage != _ratio(
            self.exact_known_owner_capital_usd,
            self.exact_opening_capital_usd,
        ):
            raise CrossPoolContractError("exact known-owner capital coverage is inconsistent")
        if self.exact_opening_capital_top_owner_share > self.exact_opening_capital_top_three_share:
            raise CrossPoolContractError("top-owner capital share cannot exceed top-three share")


@dataclass(frozen=True)
class _ReplaySwap:
    pool: PoolName
    timestamp_ms: int
    block_number: int
    log_index: int
    tx_hash: str
    raw_mid: Decimal
    active_liquidity: Decimal
    fee_rate: Decimal
    amount_usd: Decimal


def summarize_market_structure(
    *,
    base_replay_path: Path,
    bsc_replay_path: Path,
    base_ledger_path: Path,
    bsc_ledger_path: Path,
) -> tuple[VenueStructureSummary, VenueStructureSummary]:
    base_swaps = _load_replay_swaps("uni-base", base_replay_path)
    bsc_swaps = _load_replay_swaps("uni-bsc", bsc_replay_path)
    activity_start_timestamp_ms = max(
        base_swaps[0].timestamp_ms,
        bsc_swaps[0].timestamp_ms,
    )
    activity_end_timestamp_ms = min(
        base_swaps[-1].timestamp_ms,
        bsc_swaps[-1].timestamp_ms,
    )
    if activity_start_timestamp_ms > activity_end_timestamp_ms:
        raise CrossPoolContractError("replay streams have no common swap interval")

    base_common = _common_swaps(
        base_swaps,
        start_timestamp_ms=activity_start_timestamp_ms,
        end_timestamp_ms=activity_end_timestamp_ms,
    )
    bsc_common = _common_swaps(
        bsc_swaps,
        start_timestamp_ms=activity_start_timestamp_ms,
        end_timestamp_ms=activity_end_timestamp_ms,
    )
    base_ledger = tuple(
        row
        for row in load_ledger_attribution_rows("uni-base", base_ledger_path)
        if row.timestamp_ms <= activity_end_timestamp_ms
    )
    bsc_ledger = tuple(
        row
        for row in load_ledger_attribution_rows("uni-bsc", bsc_ledger_path)
        if row.timestamp_ms <= activity_end_timestamp_ms
    )
    return (
        _summarize_venue(
            pool="uni-base",
            swaps=base_common,
            ledger_rows=base_ledger,
            activity_start_timestamp_ms=activity_start_timestamp_ms,
            activity_end_timestamp_ms=activity_end_timestamp_ms,
        ),
        _summarize_venue(
            pool="uni-bsc",
            swaps=bsc_common,
            ledger_rows=bsc_ledger,
            activity_start_timestamp_ms=activity_start_timestamp_ms,
            activity_end_timestamp_ms=activity_end_timestamp_ms,
        ),
    )


def serialize_market_structure(rows: Sequence[VenueStructureSummary]) -> str:
    materialized = tuple(rows)
    by_pool = {row.pool: row for row in materialized}
    if len(materialized) != len(by_pool):
        raise CrossPoolContractError("market structure summaries contain duplicate pools")
    if tuple(sorted(by_pool, key=_POOL_ORDER.index)) != _POOL_ORDER:
        raise CrossPoolContractError(
            "market structure serialization requires Base and BSC summaries"
        )
    payload = {"venues": [_venue_payload(by_pool[pool]) for pool in _POOL_ORDER]}
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def _load_replay_swaps(pool: PoolName, path: Path) -> tuple[_ReplaySwap, ...]:
    if not path.is_file():
        raise CrossPoolContractError(f"replay CSV does not exist: {path}")
    orientation = pool_attribution_orientation(pool)
    swaps: list[_ReplaySwap] = []
    identities: set[tuple[str, int]] = set()
    previous_order: tuple[int, int] | None = None
    previous_timestamp_ms: int | None = None
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CrossPoolContractError(f"replay CSV has no header: {path}")
        missing = sorted(_REPLAY_REQUIRED_FIELDS.difference(reader.fieldnames))
        if missing:
            raise CrossPoolContractError(f"replay CSV missing required fields {missing}: {path}")

        for row_number, row in enumerate(reader, start=2):
            chain = _required_cell(row, "chain", row_number).lower()
            pool_id = _required_cell(row, "pool_id", row_number)
            token0_symbol = _required_cell(row, "token0_symbol", row_number)
            token1_symbol = _required_cell(row, "token1_symbol", row_number)
            if (
                chain != orientation.chain.lower()
                or pool_id.lower() != orientation.pool_id.lower()
                or token0_symbol != orientation.token0_symbol
                or token1_symbol != orientation.token1_symbol
            ):
                raise _replay_row_error(
                    row_number,
                    "chain, pool_id, or token symbols violate pool orientation",
                )
            event_type = _required_cell(row, "event_type", row_number).lower()
            if event_type not in _SUPPORTED_EVENT_TYPES:
                raise _replay_row_error(row_number, "unsupported replay event_type")
            block_number = _parse_positive_int(row, "block_number", row_number)
            log_index = _parse_nonnegative_int(row, "log_index", row_number)
            tx_hash = _required_cell(row, "tx_hash", row_number)
            identity = (tx_hash.lower(), log_index)
            if identity in identities:
                raise _replay_row_error(
                    row_number,
                    f"duplicate replay identity {identity}",
                )
            identities.add(identity)
            order = (block_number, log_index)
            if previous_order is not None and order <= previous_order:
                raise _replay_row_error(
                    row_number,
                    "replay rows must use strict block/log order",
                )
            timestamp_ms = _parse_timestamp_ms(
                _required_cell(row, "block_time", row_number),
                row_number,
            )
            if previous_timestamp_ms is not None and timestamp_ms < previous_timestamp_ms:
                raise _replay_row_error(
                    row_number,
                    "replay timestamps must be nondecreasing",
                )
            previous_order = order
            previous_timestamp_ms = timestamp_ms
            if event_type != "swap":
                continue

            sqrt_price_x96 = _parse_positive_int(
                row,
                "sqrt_price_x96",
                row_number,
            )
            active_liquidity = Decimal(
                _parse_positive_int(
                    row,
                    "active_liquidity",
                    row_number,
                )
            )
            fee_rate = _parse_decimal(row, "fee_rate", row_number)
            if not Decimal("0") < fee_rate < Decimal("1"):
                raise _replay_row_error(row_number, "fee_rate must be in (0, 1)")
            amount_usd = _parse_decimal(row, "amount_usd", row_number)
            if amount_usd < 0:
                raise _replay_row_error(row_number, "amount_usd must be nonnegative")
            raw_mid = _canonical_mid_from_sqrt_price_x96(
                sqrt_price_x96,
                pool=pool,
            )
            _require_positive_decimal(raw_mid, "canonical replay mid")
            swaps.append(
                _ReplaySwap(
                    pool=pool,
                    timestamp_ms=timestamp_ms,
                    block_number=block_number,
                    log_index=log_index,
                    tx_hash=tx_hash,
                    raw_mid=raw_mid,
                    active_liquidity=active_liquidity,
                    fee_rate=fee_rate,
                    amount_usd=amount_usd,
                )
            )
    if not swaps:
        raise CrossPoolContractError(f"{pool} replay requires at least one swap")
    return tuple(swaps)


def _common_swaps(
    swaps: Sequence[_ReplaySwap],
    *,
    start_timestamp_ms: int,
    end_timestamp_ms: int,
) -> tuple[_ReplaySwap, ...]:
    common = tuple(
        row for row in swaps if start_timestamp_ms <= row.timestamp_ms <= end_timestamp_ms
    )
    if len(common) < 2:
        raise CrossPoolContractError(
            f"{swaps[0].pool} common activity interval requires at least two swaps"
        )
    return common


def _summarize_venue(
    *,
    pool: PoolName,
    swaps: Sequence[_ReplaySwap],
    ledger_rows: Sequence[LedgerAttributionRow],
    activity_start_timestamp_ms: int,
    activity_end_timestamp_ms: int,
) -> VenueStructureSummary:
    fee_rates = {row.fee_rate for row in swaps}
    if len(fee_rates) != 1:
        raise CrossPoolContractError(f"{pool} swaps must use one fee rate")
    fee_rate = next(iter(fee_rates))
    if fee_rate != pool_attribution_orientation(pool).fee_rate:
        raise CrossPoolContractError(f"{pool} swaps must use the frozen fee rate")
    update_gaps = tuple(
        Decimal(current.timestamp_ms - previous.timestamp_ms)
        for previous, current in zip(swaps, swaps[1:], strict=False)
    )
    meaningful_move_count = sum(
        _meets_meaningful_move_threshold(_log_move_bps(previous.raw_mid, current.raw_mid))
        for previous, current in zip(swaps, swaps[1:], strict=False)
    )
    active_liquidity = tuple(row.active_liquidity for row in swaps)
    volumes = tuple(row.amount_usd for row in swaps)

    materialized_ledger = tuple(ledger_rows)
    openings = tuple(row for row in materialized_ledger if row.liquidity_delta > 0)
    exact_openings = tuple(row for row in openings if row.attribution_class == "exact")
    ambiguous_openings = tuple(row for row in openings if row.attribution_class == "ambiguous")
    tracked_liquidity = _sum_decimals(
        row.liquidity_delta for row in exact_openings + ambiguous_openings
    )
    if tracked_liquidity <= 0:
        raise CrossPoolContractError(f"{pool} requires positive tracked opening liquidity")
    exact_capital = _sum_decimals(
        (row.opening_capital_usd for row in exact_openings if row.opening_capital_usd is not None)
    )
    if exact_capital <= 0:
        raise CrossPoolContractError(f"{pool} requires positive exact opening capital")
    owner_capital: dict[str, Decimal] = {}
    for row in exact_openings:
        if row.owner is None or row.opening_capital_usd is None:
            continue
        owner_capital[row.owner] = _sum_decimals(
            (
                owner_capital.get(row.owner, Decimal("0")),
                row.opening_capital_usd,
            )
        )
    known_capital = _sum_decimals(owner_capital.values())
    if known_capital <= 0:
        raise CrossPoolContractError(f"{pool} requires positive exact known-owner capital")
    ordered_owner_capitals = tuple(sorted(owner_capital.values(), reverse=True))
    owner_shares = tuple(_ratio(value, known_capital) for value in ordered_owner_capitals)
    ambiguous_liquidity = _sum_decimals(row.liquidity_delta for row in ambiguous_openings)

    return VenueStructureSummary(
        pool=pool,
        activity_start_timestamp_ms=activity_start_timestamp_ms,
        activity_end_timestamp_ms=activity_end_timestamp_ms,
        ledger_cutoff_timestamp_ms=activity_end_timestamp_ms,
        swap_count=len(swaps),
        meaningful_move_count=meaningful_move_count,
        fee_rate=fee_rate,
        update_gaps_ms=_distribution(update_gaps),
        active_liquidity=_distribution(active_liquidity),
        volume_usd=_distribution(volumes),
        total_volume_usd=_sum_decimals(volumes),
        ledger_rows=len(materialized_ledger),
        opening_count=len(openings),
        exact_opening_count=len(exact_openings),
        ambiguous_opening_count=len(ambiguous_openings),
        known_owner_count=len(owner_capital),
        exact_unknown_owner_opening_count=sum(row.owner is None for row in exact_openings),
        exact_opening_capital_usd=exact_capital,
        exact_known_owner_capital_usd=known_capital,
        exact_known_owner_capital_coverage=_ratio(known_capital, exact_capital),
        ambiguous_opening_liquidity_share=_ratio(
            ambiguous_liquidity,
            tracked_liquidity,
        ),
        exact_opening_capital_top_owner_share=owner_shares[0],
        exact_opening_capital_top_three_share=_sum_decimals(
            owner_shares[:3],
            precision=_RATIO_PRECISION,
        ),
        exact_opening_capital_hhi=_sum_decimals(
            (share * share for share in owner_shares),
            precision=_RATIO_PRECISION,
        ),
    )


def _distribution(values: Sequence[Decimal]) -> DistributionSummary:
    ordered = tuple(sorted(values))
    if not ordered:
        raise CrossPoolContractError("market-structure distribution has no observations")
    return DistributionSummary(
        count=len(ordered),
        minimum=ordered[0],
        median=_type7_quantile(ordered, Decimal("0.50")),
        p95=_type7_quantile(ordered, Decimal("0.95")),
        p99=_type7_quantile(ordered, Decimal("0.99")),
        maximum=ordered[-1],
    )


def _type7_quantile(values: Sequence[Decimal], probability: Decimal) -> Decimal:
    if not values:
        raise CrossPoolContractError("type-7 quantile requires observations")
    if not Decimal("0") <= probability <= Decimal("1"):
        raise CrossPoolContractError("type-7 quantile probability must be in [0, 1]")
    with localcontext() as context:
        context.prec = _CALCULATION_PRECISION
        position = Decimal(len(values) - 1) * probability
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(values) - 1)
        if lower_index == upper_index:
            return values[lower_index]
        fraction = position - Decimal(lower_index)
        return +(values[lower_index] + fraction * (values[upper_index] - values[lower_index]))


def _log_move_bps(previous_mid: Decimal, current_mid: Decimal) -> Decimal:
    _require_positive_decimal(previous_mid, "previous canonical mid")
    _require_positive_decimal(current_mid, "current canonical mid")
    try:
        with localcontext() as context:
            context.prec = _CALCULATION_PRECISION
            return +abs((current_mid / previous_mid).ln()) * Decimal("10000")
    except DecimalException as exc:
        raise CrossPoolContractError("canonical replay log move must be finite") from exc


def _canonical_mid_from_sqrt_price_x96(
    sqrt_price_x96: int,
    *,
    pool: PoolName,
) -> Decimal:
    orientation = pool_attribution_orientation(pool)
    with localcontext() as context:
        context.prec = 80
        native = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2
        native *= Decimal(10) ** Decimal(orientation.token0_decimals - orientation.token1_decimals)
        if orientation.token0_symbol.upper() == "CNGN":
            return +native
    if orientation.token1_symbol.upper() == "CNGN":
        with localcontext() as context:
            context.prec = _CALCULATION_PRECISION
            return +(Decimal("1") / native)
    raise CrossPoolContractError(f"unsupported pool token orientation for {pool}")


def _meets_meaningful_move_threshold(move_bps: Decimal) -> bool:
    _require_nonnegative_decimal(move_bps, "meaningful move magnitude")
    return move_bps >= _MEANINGFUL_MOVE_BPS


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    _require_nonnegative_decimal(numerator, "ratio numerator")
    _require_positive_decimal(denominator, "ratio denominator")
    with localcontext() as context:
        context.prec = _RATIO_PRECISION
        return +(numerator / denominator)


def _sum_decimals(
    values: Iterable[Decimal],
    *,
    precision: int = _CALCULATION_PRECISION,
) -> Decimal:
    with localcontext() as context:
        context.prec = precision
        return +sum(values, Decimal("0"))


def _venue_payload(row: VenueStructureSummary) -> dict[str, object]:
    return {
        "pool": row.pool,
        "activity_start_timestamp_ms": row.activity_start_timestamp_ms,
        "activity_end_timestamp_ms": row.activity_end_timestamp_ms,
        "ledger_cutoff_timestamp_ms": row.ledger_cutoff_timestamp_ms,
        "swap_count": row.swap_count,
        "meaningful_move_count": row.meaningful_move_count,
        "fee_rate": _decimal_text(row.fee_rate),
        "update_gaps_ms": _distribution_payload(row.update_gaps_ms),
        "active_liquidity": _distribution_payload(row.active_liquidity),
        "volume_usd": _distribution_payload(row.volume_usd),
        "total_volume_usd": _decimal_text(row.total_volume_usd),
        "ledger_rows": row.ledger_rows,
        "opening_count": row.opening_count,
        "exact_opening_count": row.exact_opening_count,
        "ambiguous_opening_count": row.ambiguous_opening_count,
        "known_owner_count": row.known_owner_count,
        "exact_unknown_owner_opening_count": row.exact_unknown_owner_opening_count,
        "exact_opening_capital_usd": _decimal_text(row.exact_opening_capital_usd),
        "exact_known_owner_capital_usd": _decimal_text(row.exact_known_owner_capital_usd),
        "exact_known_owner_capital_coverage": _decimal_text(row.exact_known_owner_capital_coverage),
        "ambiguous_opening_liquidity_share": _decimal_text(row.ambiguous_opening_liquidity_share),
        "exact_opening_capital_top_owner_share": _decimal_text(
            row.exact_opening_capital_top_owner_share
        ),
        "exact_opening_capital_top_three_share": _decimal_text(
            row.exact_opening_capital_top_three_share
        ),
        "exact_opening_capital_hhi": _decimal_text(row.exact_opening_capital_hhi),
    }


def _distribution_payload(summary: DistributionSummary) -> dict[str, object]:
    return {
        "count": summary.count,
        "minimum": _decimal_text(summary.minimum),
        "median": _decimal_text(summary.median),
        "p95": _decimal_text(summary.p95),
        "p99": _decimal_text(summary.p99),
        "maximum": _decimal_text(summary.maximum),
    }


def _decimal_text(value: Decimal) -> str:
    _require_finite_decimal(value, "serialized decimal")
    return format(value, "f")


def _parse_timestamp_ms(value: str, row_number: int) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _replay_row_error(row_number, "block_time must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _replay_row_error(row_number, "block_time must be UTC-aware")
    normalized = parsed.astimezone(timezone.utc)
    if normalized.microsecond % 1_000 != 0:
        raise _replay_row_error(row_number, "block_time must have millisecond precision")
    elapsed = normalized - _EPOCH
    timestamp_ms = (
        elapsed.days * 86_400_000 + elapsed.seconds * 1_000 + elapsed.microseconds // 1_000
    )
    if timestamp_ms <= 0:
        raise _replay_row_error(row_number, "block_time must follow the Unix epoch")
    return timestamp_ms


def _required_cell(
    row: Mapping[str, str | None],
    field: str,
    row_number: int,
) -> str:
    value = row.get(field)
    if value is None or not value.strip():
        raise _replay_row_error(row_number, f"{field} cannot be empty")
    return value.strip()


def _parse_positive_int(
    row: Mapping[str, str | None],
    field: str,
    row_number: int,
) -> int:
    value = _parse_int(row, field, row_number)
    if value <= 0:
        raise _replay_row_error(row_number, f"{field} must be positive")
    return value


def _parse_nonnegative_int(
    row: Mapping[str, str | None],
    field: str,
    row_number: int,
) -> int:
    value = _parse_int(row, field, row_number)
    if value < 0:
        raise _replay_row_error(row_number, f"{field} must be nonnegative")
    return value


def _parse_int(
    row: Mapping[str, str | None],
    field: str,
    row_number: int,
) -> int:
    value = _required_cell(row, field, row_number)
    try:
        return int(value)
    except ValueError as exc:
        raise _replay_row_error(row_number, f"{field} must be an integer") from exc


def _parse_decimal(
    row: Mapping[str, str | None],
    field: str,
    row_number: int,
) -> Decimal:
    value = _required_cell(row, field, row_number)
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise _replay_row_error(row_number, f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise _replay_row_error(row_number, f"{field} must be finite")
    return parsed


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{label} must be a positive integer")


def _require_nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{label} must be a nonnegative integer")


def _require_finite_decimal(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CrossPoolContractError(f"{label} must be finite")


def _require_nonnegative_decimal(value: Decimal, label: str) -> None:
    _require_finite_decimal(value, label)
    if value < 0:
        raise CrossPoolContractError(f"{label} must be nonnegative")


def _require_positive_decimal(value: Decimal, label: str) -> None:
    _require_finite_decimal(value, label)
    if value <= 0:
        raise CrossPoolContractError(f"{label} must be positive")


def _require_unit_interval(value: Decimal, label: str) -> None:
    _require_finite_decimal(value, label)
    if not Decimal("0") <= value <= Decimal("1"):
        raise CrossPoolContractError(f"{label} must be in [0, 1]")


def _replay_row_error(row_number: int, message: str) -> CrossPoolContractError:
    return CrossPoolContractError(f"replay row {row_number}: {message}")
