"""Post-hoc venue activity and anonymized LP-capital diagnostics."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Mapping, Sequence

from research.backtester.lp_ledger_attribution import (
    FROZEN_REPLAY_EVENT_SOURCES,
    FROZEN_REPLAY_INPUT_FIELDS,
    FROZEN_REPLAY_PARSER_VERSION,
    FROZEN_REPLAY_PRICE_EVENT_TYPES,
    LedgerAttributionRow,
    VerifiedLedgerCoverageEvidence,
    frozen_replay_evidence_sha256,
    frozen_replay_header_sha256,
    frozen_replay_parser_contract_sha256,
    frozen_replay_price_semantics_sha256,
    load_verified_ledger_attribution_rows,
    pool_attribution_orientation,
)
from research.backtester.pool_price_semantics import raw_sqrt_mid_from_row
from research.backtester.v4_event_replay import ReplayEvent
from research.cross_pool.contracts import (
    CrossPoolContractError,
    PoolName,
)

_MEANINGFUL_MOVE_BPS = Decimal("10")
_RATIO_PRECISION = 28
_CALCULATION_PRECISION = 60
_POOL_ORDER: tuple[PoolName, ...] = ("uni-base", "uni-bsc")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TX_HASH_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")


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
class MarketStructureAnalysis:
    venues: tuple[VenueStructureSummary, VenueStructureSummary]
    ledger_coverage: tuple[
        VerifiedLedgerCoverageEvidence,
        VerifiedLedgerCoverageEvidence,
    ]

    def __post_init__(self) -> None:
        if tuple(row.pool for row in self.venues) != _POOL_ORDER:
            raise CrossPoolContractError(
                "market structure analysis requires ordered Base and BSC venues"
            )
        if tuple(row.pool for row in self.ledger_coverage) != _POOL_ORDER:
            raise CrossPoolContractError(
                "market structure analysis requires ordered Base and BSC coverage"
            )


@dataclass(frozen=True)
class ReplayStreamEvidence:
    pool: PoolName
    sha256: str
    byte_length: int
    row_count: int
    header_sha256: str
    parser_version: str
    parser_contract_sha256: str
    price_semantics_sha256: str
    first_block: int
    last_block: int
    artifact_first_timestamp_ms: int
    artifact_last_timestamp_ms: int
    price_event_count: int
    price_events_sha256: str
    swap_count: int
    first_timestamp_ms: int
    last_timestamp_ms: int

    def __post_init__(self) -> None:
        if self.pool not in _POOL_ORDER:
            raise CrossPoolContractError("replay evidence requires a supported pool")
        for digest in (
            self.sha256,
            self.header_sha256,
            self.parser_contract_sha256,
            self.price_semantics_sha256,
            self.price_events_sha256,
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise CrossPoolContractError(
                    "replay evidence requires canonical SHA-256 values"
                )
        if (
            self.parser_version != FROZEN_REPLAY_PARSER_VERSION
            or self.header_sha256 != frozen_replay_header_sha256()
            or self.parser_contract_sha256
            != frozen_replay_parser_contract_sha256()
            or self.price_semantics_sha256
            != frozen_replay_price_semantics_sha256()
        ):
            raise CrossPoolContractError("replay parser evidence is inconsistent")
        for value, label in (
            (self.byte_length, "replay evidence byte length"),
            (self.row_count, "replay evidence row_count"),
            (self.first_block, "replay evidence first block"),
            (self.last_block, "replay evidence last block"),
            (
                self.artifact_first_timestamp_ms,
                "replay evidence artifact first timestamp",
            ),
            (
                self.artifact_last_timestamp_ms,
                "replay evidence artifact last timestamp",
            ),
            (self.price_event_count, "replay evidence price event count"),
        ):
            _require_positive_int(value, label)
        _require_positive_int(self.swap_count, "replay evidence swap_count")
        _require_positive_int(
            self.first_timestamp_ms,
            "replay evidence first timestamp",
        )
        _require_positive_int(
            self.last_timestamp_ms,
            "replay evidence last timestamp",
        )
        if self.first_block > self.last_block:
            raise CrossPoolContractError("replay evidence block interval is empty")
        if self.artifact_first_timestamp_ms > self.artifact_last_timestamp_ms:
            raise CrossPoolContractError("replay artifact interval is empty")
        if self.first_timestamp_ms > self.last_timestamp_ms:
            raise CrossPoolContractError("replay evidence interval is empty")
        if (
            self.swap_count > self.price_event_count
            or self.price_event_count > self.row_count
            or not (
                self.artifact_first_timestamp_ms
                <= self.first_timestamp_ms
                <= self.last_timestamp_ms
                <= self.artifact_last_timestamp_ms
            )
        ):
            raise CrossPoolContractError(
                "replay swap evidence falls outside the replay artifact"
            )


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


@dataclass(frozen=True)
class _ReplayLoad:
    swaps: tuple[_ReplaySwap, ...]
    evidence: ReplayStreamEvidence


def inspect_replay_stream(pool: PoolName, path: Path) -> ReplayStreamEvidence:
    """Validate one replay and expose whole-file and swap-only evidence."""
    return _load_replay_stream(pool, path).evidence


def validate_ledger_replay_binding(
    coverage: VerifiedLedgerCoverageEvidence,
    replay: ReplayStreamEvidence,
) -> None:
    """Require one verified ledger to attest the exact replay artifact."""
    bound = coverage.evidence.replay_input
    if coverage.pool != replay.pool or (
        bound.sha256,
        bound.byte_length,
        bound.row_count,
        bound.header_sha256,
        bound.parser_version,
        bound.parser_contract_sha256,
        bound.price_semantics_sha256,
        bound.first_block,
        bound.last_block,
        bound.first_timestamp_ms,
        bound.last_timestamp_ms,
        bound.price_event_count,
        bound.price_events_sha256,
    ) != (
        replay.sha256,
        replay.byte_length,
        replay.row_count,
        replay.header_sha256,
        replay.parser_version,
        replay.parser_contract_sha256,
        replay.price_semantics_sha256,
        replay.first_block,
        replay.last_block,
        replay.artifact_first_timestamp_ms,
        replay.artifact_last_timestamp_ms,
        replay.price_event_count,
        replay.price_events_sha256,
    ):
        raise CrossPoolContractError(
            f"{replay.pool} LP ledger coverage does not bind the exact replay artifact"
        )


def replay_end_block_at_or_before(
    pool: PoolName,
    path: Path,
    *,
    cutoff_timestamp_ms: int,
) -> int:
    """Return the last validated swap block observable by a UTC cutoff."""
    _require_positive_int(cutoff_timestamp_ms, "replay cutoff timestamp")
    swaps = _load_replay_stream(pool, path).swaps
    eligible = tuple(
        row for row in swaps if row.timestamp_ms <= cutoff_timestamp_ms
    )
    if not eligible:
        raise CrossPoolContractError(
            f"{pool} replay has no swap at or before the required cutoff"
        )
    return eligible[-1].block_number


def summarize_market_structure(
    *,
    base_replay_path: Path,
    bsc_replay_path: Path,
    base_ledger_path: Path,
    bsc_ledger_path: Path,
) -> tuple[VenueStructureSummary, VenueStructureSummary]:
    return analyze_market_structure(
        base_replay_path=base_replay_path,
        bsc_replay_path=bsc_replay_path,
        base_ledger_path=base_ledger_path,
        bsc_ledger_path=bsc_ledger_path,
    ).venues


def analyze_market_structure(
    *,
    base_replay_path: Path,
    bsc_replay_path: Path,
    base_ledger_path: Path,
    bsc_ledger_path: Path,
) -> MarketStructureAnalysis:
    base_replay = _load_replay_stream("uni-base", base_replay_path)
    bsc_replay = _load_replay_stream("uni-bsc", bsc_replay_path)
    base_swaps = base_replay.swaps
    bsc_swaps = bsc_replay.swaps
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
    base_verified = load_verified_ledger_attribution_rows(
        "uni-base",
        base_ledger_path,
        required_end_block=base_common[-1].block_number,
        required_end_timestamp_ms=activity_end_timestamp_ms,
    )
    bsc_verified = load_verified_ledger_attribution_rows(
        "uni-bsc",
        bsc_ledger_path,
        required_end_block=bsc_common[-1].block_number,
        required_end_timestamp_ms=activity_end_timestamp_ms,
    )
    validate_ledger_replay_binding(base_verified.coverage, base_replay.evidence)
    validate_ledger_replay_binding(bsc_verified.coverage, bsc_replay.evidence)
    base_ledger = tuple(
        row
        for row in base_verified.rows
        if row.timestamp_ms <= activity_end_timestamp_ms
    )
    bsc_ledger = tuple(
        row
        for row in bsc_verified.rows
        if row.timestamp_ms <= activity_end_timestamp_ms
    )
    return MarketStructureAnalysis(
        venues=(
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
        ),
        ledger_coverage=(
            base_verified.coverage,
            bsc_verified.coverage,
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
    return _load_replay_stream(pool, path).swaps


def _load_replay_stream(pool: PoolName, path: Path) -> _ReplayLoad:
    if not path.is_file():
        raise CrossPoolContractError(f"replay CSV does not exist: {path}")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError(f"replay CSV could not be read: {path}") from exc
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError(f"replay CSV must use UTF-8: {path}") from exc
    orientation = pool_attribution_orientation(pool)
    swaps: list[_ReplaySwap] = []
    identities: set[tuple[str, int]] = set()
    previous_order: tuple[int, int] | None = None
    previous_timestamp_ms: int | None = None
    row_count = 0
    first_block: int | None = None
    first_timestamp_ms: int | None = None
    price_events: list[ReplayEvent] = []
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        if reader.fieldnames is None or tuple(reader.fieldnames) != (
            FROZEN_REPLAY_INPUT_FIELDS
        ):
            raise CrossPoolContractError(
                f"replay CSV does not use the exact frozen header: {path}"
            )

        for row_number, row in enumerate(reader, start=2):
            if set(row) != set(FROZEN_REPLAY_INPUT_FIELDS) or not all(
                isinstance(value, str) for value in row.values()
            ):
                raise _replay_row_error(row_number, "malformed replay row")
            row_count += 1
            chain = _required_cell(row, "chain", row_number)
            pool_id = _required_cell(row, "pool_id", row_number)
            token0_symbol = _required_cell(row, "token0_symbol", row_number)
            token1_symbol = _required_cell(row, "token1_symbol", row_number)
            if (
                chain != orientation.chain
                or pool_id != orientation.pool_id
                or token0_symbol != orientation.token0_symbol
                or token1_symbol != orientation.token1_symbol
            ):
                raise _replay_row_error(
                    row_number,
                    "chain, pool_id, or token symbols violate pool orientation",
                )
            event_type = _required_cell(row, "event_type", row_number)
            if event_type not in FROZEN_REPLAY_EVENT_SOURCES:
                raise _replay_row_error(row_number, "unsupported replay event_type")
            event_source = _required_cell(row, "event_source", row_number)
            if event_source not in FROZEN_REPLAY_EVENT_SOURCES[event_type]:
                raise _replay_row_error(row_number, "invalid replay event_source")
            block_number = _parse_positive_int(row, "block_number", row_number)
            log_index = _parse_nonnegative_int(row, "log_index", row_number)
            tx_hash = _required_cell(row, "tx_hash", row_number)
            if not _TX_HASH_PATTERN.fullmatch(tx_hash):
                raise _replay_row_error(
                    row_number,
                    "transaction hash must be canonical",
                )
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
            if first_block is None:
                first_block = block_number
                first_timestamp_ms = timestamp_ms
            if previous_timestamp_ms is not None and timestamp_ms < previous_timestamp_ms:
                raise _replay_row_error(
                    row_number,
                    "replay timestamps must be nondecreasing",
                )
            previous_order = order
            previous_timestamp_ms = timestamp_ms
            if event_type in FROZEN_REPLAY_PRICE_EVENT_TYPES:
                sqrt_price_x96 = _parse_positive_int(
                    row,
                    "sqrt_price_x96",
                    row_number,
                )
                tick = _parse_int(row, "tick", row_number)
                raw_mid = raw_sqrt_mid_from_row(row)
                _require_positive_decimal(raw_mid, "canonical replay mid")
                price_events.append(
                    ReplayEvent(
                        block_number=block_number,
                        log_index=log_index,
                        event_order=0,
                        event_type=event_type,
                        sqrt_price_x96=sqrt_price_x96,
                        tick=tick,
                    )
                )
            if event_type != "swap":
                continue
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
    except csv.Error as exc:
        raise CrossPoolContractError(f"replay CSV is malformed: {path}") from exc
    if not swaps:
        raise CrossPoolContractError(f"{pool} replay requires at least one swap")
    assert first_block is not None
    assert first_timestamp_ms is not None
    assert previous_order is not None
    assert previous_timestamp_ms is not None
    evidence = ReplayStreamEvidence(
        pool=pool,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        byte_length=len(raw_bytes),
        row_count=row_count,
        header_sha256=frozen_replay_header_sha256(),
        parser_version=FROZEN_REPLAY_PARSER_VERSION,
        parser_contract_sha256=frozen_replay_parser_contract_sha256(),
        price_semantics_sha256=frozen_replay_price_semantics_sha256(),
        first_block=first_block,
        last_block=previous_order[0],
        artifact_first_timestamp_ms=first_timestamp_ms,
        artifact_last_timestamp_ms=previous_timestamp_ms,
        price_event_count=len(price_events),
        price_events_sha256=frozen_replay_evidence_sha256(
            [asdict(event) for event in price_events]
        ),
        swap_count=len(swaps),
        first_timestamp_ms=swaps[0].timestamp_ms,
        last_timestamp_ms=swaps[-1].timestamp_ms,
    )
    return _ReplayLoad(swaps=tuple(swaps), evidence=evidence)


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
    if value is None or not value or value != value.strip():
        raise _replay_row_error(row_number, f"{field} cannot be empty")
    return value


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
        parsed = int(value)
    except ValueError as exc:
        raise _replay_row_error(row_number, f"{field} must be an integer") from exc
    if str(parsed) != value:
        raise _replay_row_error(row_number, f"{field} must be canonical")
    return parsed


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
