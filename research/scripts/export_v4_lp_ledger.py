"""Export Uniswap v4 LP lifecycle ledger rows."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.web3_utils import coerce_hex_str  # noqa: E402
from research.backtester.lp_ledger_attribution import (  # noqa: E402
    build_fixture_ledger_coverage_bytes,
    build_rpc_ledger_coverage_bytes,
    ledger_coverage_path,
    load_ledger_coverage,
    pool_attribution_orientation,
)
from research.backtester.lp_ledger_checkpoint import render_safe_failure  # noqa: E402
from research.backtester.v4_event_replay import (  # noqa: E402
    ReplayedEvent,
    ReplayEvent,
    attach_event_time_state,
)
from research.backtester.v4_export import (  # noqa: E402
    POOL_CONFIGS,
    POSITION_MANAGER_ABI,
    STATE_VIEW_ABI,
    V4_INITIALIZE_TOPIC,
    V4_MODIFY_LIQUIDITY_TOPIC,
    V4_SWAP_TOPIC,
    _block_timestamp_from_raw,
    _fetch_logs_with_debug,
    _make_web3,
    _position_state_from_chain,
    _raw_get_block,
    _state_seed_before_block,
    decode_initialize_row,
    decode_swap_row,
)
from research.backtester.v4_lp_ledger import (  # noqa: E402
    TRANSFER_EVENT_TOPIC,
    DecodedLiquidityAction,
    LedgerPositionState,
    LPLedgerRow,
    OwnershipEvent,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
)
from research.cross_pool.contracts import CrossPoolContractError  # noqa: E402

_LP_CANDIDATE_EVENT_TYPES = frozenset({"mint", "burn", "collect"})


@dataclass(frozen=True)
class RpcLPCandidateDiscovery:
    action_transaction_hashes: tuple[str, ...]
    ownership_transaction_hashes: tuple[str, ...]
    all_transaction_hashes: tuple[str, ...]


@dataclass(frozen=True)
class _RpcTransactionBundle:
    transaction_hash: str
    transaction: dict[str, Any]
    receipt: dict[str, Any]
    block_number: int
    transaction_index: int


@dataclass(frozen=True)
class _RpcCoverageEndpointSnapshot:
    start_block_hash: str
    start_timestamp_ms: int
    end_block_hash: str
    end_timestamp_ms: int


def _discover_rpc_lp_candidates(
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
) -> RpcLPCandidateDiscovery:
    action_hashes: set[str] = set()
    ownership_hashes: set[str] = set()
    for chunk_start in range(start_block, end_block + 1, config.chunk_size):
        chunk_end = min(chunk_start + config.chunk_size - 1, end_block)
        action_logs = _fetch_logs_with_debug(
            w3,
            {
                "address": Web3.to_checksum_address(config.pool_manager),
                "topics": [V4_MODIFY_LIQUIDITY_TOPIC, config.pool_id],
                "fromBlock": chunk_start,
                "toBlock": chunk_end,
            },
            context=(
                f"[{config.name}] pool modify-liquidity logs "
                f"{chunk_start:,}->{chunk_end:,}"
            ),
        )
        transfer_logs = _fetch_logs_with_debug(
            w3,
            {
                "address": Web3.to_checksum_address(config.position_manager),
                "topics": [TRANSFER_EVENT_TOPIC],
                "fromBlock": chunk_start,
                "toBlock": chunk_end,
            },
            context=(
                f"[{config.name}] position-manager transfer logs "
                f"{chunk_start:,}->{chunk_end:,}"
            ),
        )
        action_hashes.update(_required_log_transaction_hashes(action_logs))
        ownership_hashes.update(_required_log_transaction_hashes(transfer_logs))
    canonical_actions = tuple(sorted(action_hashes))
    canonical_ownership = tuple(sorted(ownership_hashes))
    return RpcLPCandidateDiscovery(
        action_transaction_hashes=canonical_actions,
        ownership_transaction_hashes=canonical_ownership,
        all_transaction_hashes=tuple(
            sorted(action_hashes.union(ownership_hashes))
        ),
    )


def _required_log_transaction_hashes(logs: Sequence[Any]) -> tuple[str, ...]:
    transaction_hashes: list[str] = []
    for log in logs:
        raw_hash = log.get("transactionHash")
        if raw_hash is None:
            raise ValueError("verified LP discovery log is missing transactionHash")
        transaction_hashes.append(
            _normalize_transaction_hash(
                raw_hash,
                label="verified LP discovery log transactionHash",
            )
        )
    return tuple(transaction_hashes)


def export_fixture_lp_ledger(
    pool: str,
    start_block: int,
    end_block: int,
    decoded_actions_path: Path,
    ownership_events_path: Path,
    price_events_path: Path,
    output_path: Path,
) -> int:
    ledger_coverage_path(output_path)
    rows = build_lp_ledger_rows(
        decoded_actions=[
            _decoded_action_from_json(item) for item in _read_json_list(decoded_actions_path)
        ],
        ownership_events=[
            OwnershipEvent(**item) for item in _read_json_list(ownership_events_path)
        ],
        price_events=[ReplayedEvent(**item) for item in _read_json_list(price_events_path)],
    )
    ledger_bytes = _render_ledger_rows(rows)
    coverage_bytes = build_fixture_ledger_coverage_bytes(
        pool,
        ledger_bytes,
        requested_start_block=start_block,
        requested_end_block=end_block,
        fixture_input_sha256={
            "decoded_actions": _sha256_path(decoded_actions_path),
            "ownership_events": _sha256_path(ownership_events_path),
            "price_events": _sha256_path(price_events_path),
        },
    )
    _publish_ledger_pair(pool, output_path, ledger_bytes, coverage_bytes)
    return len(rows)


def export_rpc_lp_ledger(
    pool: str,
    start_block: int,
    end_block: int,
    output_path: Path,
    candidate_tx_hashes: Sequence[str] | None = None,
) -> int:
    ledger_coverage_path(output_path)
    config = POOL_CONFIGS[pool]
    w3 = _make_web3(config)
    chain_id = _coverage_chain_id(w3, pool)
    initial_endpoint = _capture_rpc_coverage_endpoint(
        w3,
        start_block,
        end_block,
    )
    used_full_rpc_scan = candidate_tx_hashes is None
    if candidate_tx_hashes is None:
        discovery = _discover_rpc_lp_candidates(w3, config, start_block, end_block)
        resolved_candidate_hashes = discovery.all_transaction_hashes
        required_action_hashes = frozenset(discovery.action_transaction_hashes)
    else:
        resolved_candidate_hashes = tuple(candidate_tx_hashes)
        required_action_hashes = frozenset()
    decoded_actions, ownership_events = _decode_rpc_lp_inputs(
        w3,
        config,
        resolved_candidate_hashes,
        required_action_tx_hashes=required_action_hashes,
    )
    price_events = _replayed_price_events_for_actions(
        w3,
        config,
        start_block,
        end_block,
        decoded_actions,
    )
    rows = build_lp_ledger_rows(decoded_actions, ownership_events, price_events)
    final_endpoint = _capture_rpc_coverage_endpoint(
        w3,
        start_block,
        end_block,
    )
    if final_endpoint != initial_endpoint:
        raise CrossPoolContractError(
            "RPC coverage endpoint snapshot changed during LP ledger export"
        )
    ledger_bytes = _render_ledger_rows(rows)
    coverage_bytes = build_rpc_ledger_coverage_bytes(
        pool,
        ledger_bytes,
        chain_id=chain_id,
        covered_start_block=start_block,
        covered_start_block_hash=initial_endpoint.start_block_hash,
        covered_start_timestamp_ms=initial_endpoint.start_timestamp_ms,
        covered_end_block=end_block,
        covered_end_block_hash=initial_endpoint.end_block_hash,
        covered_end_timestamp_ms=initial_endpoint.end_timestamp_ms,
        candidate_transaction_hashes=resolved_candidate_hashes,
        verification_mode=("rpc_verified" if used_full_rpc_scan else "candidate_list_unverified"),
    )
    _publish_ledger_pair(pool, output_path, ledger_bytes, coverage_bytes)
    return len(rows)


def _decode_rpc_lp_inputs(
    w3: Web3,
    config: Any,
    candidate_tx_hashes: Sequence[str],
    *,
    required_action_tx_hashes: frozenset[str],
) -> tuple[list[DecodedLiquidityAction], list[OwnershipEvent]]:
    position_manager = w3.eth.contract(
        address=Web3.to_checksum_address(config.position_manager),
        abi=POSITION_MANAGER_ABI,
    )
    token_state: dict[int, LedgerPositionState] = {}
    decoded_actions: list[DecodedLiquidityAction] = []
    ownership_events: list[OwnershipEvent] = []

    def resolve_position(token_id: int, block_number: int) -> LedgerPositionState | None:
        position = _position_state_from_chain(token_id, position_manager, config, block_number)
        if position is None:
            return None
        return LedgerPositionState(
            pool_id=position.pool_id,
            tick_lower=position.tick_lower,
            tick_upper=position.tick_upper,
            liquidity_after=position.liquidity,
        )

    normalized_required_action_hashes = frozenset(
        _normalize_transaction_hash(value, label="required action transaction hash")
        for value in required_action_tx_hashes
    )
    tx_receipts: list[_RpcTransactionBundle] = []
    for raw_requested_hash in candidate_tx_hashes:
        requested_hash = _normalize_transaction_hash(
            raw_requested_hash,
            label="candidate transaction hash",
        )
        tx = dict(w3.eth.get_transaction(requested_hash))
        receipt = dict(w3.eth.get_transaction_receipt(requested_hash))
        _require_matching_response_hash(
            requested_hash,
            tx.get("hash"),
            label="RPC transaction response",
        )
        _require_matching_response_hash(
            requested_hash,
            receipt.get("transactionHash"),
            label="RPC receipt response",
        )
        block_number, transaction_index = _require_matching_transaction_location(
            requested_hash,
            tx,
            receipt,
        )
        tx_receipts.append(
            _RpcTransactionBundle(
                transaction_hash=requested_hash,
                transaction=tx,
                receipt=receipt,
                block_number=block_number,
                transaction_index=transaction_index,
            )
        )

    for bundle in sorted(tx_receipts, key=_tx_receipt_sort_key):
        block_timestamp = _block_timestamp_for_number(w3, bundle.block_number)
        ownership_events.extend(
            decode_ownership_events_from_receipt(
                bundle.receipt,
                config.position_manager,
            )
        )
        transaction_actions = decode_liquidity_actions_for_tx(
            bundle.transaction,
            bundle.receipt,
            block_timestamp,
            config,
            token_state,
            position_resolver=resolve_position,
        )
        transaction_hash = bundle.transaction_hash
        if transaction_hash in normalized_required_action_hashes:
            expected_log_indices = _target_pool_modify_log_indices(
                bundle.receipt,
                config,
            )
            if not expected_log_indices:
                raise ValueError(
                    "target-pool candidate receipt has no target-pool "
                    f"ModifyLiquidity log: {transaction_hash}"
                )
            if not transaction_actions:
                raise ValueError(
                    "target-pool liquidity transaction is not representable by the "
                    f"configured PositionManager decoder: {transaction_hash}"
                )
            expected_log_index_set = frozenset(expected_log_indices)
            represented_log_indices = tuple(
                sorted(
                    action.log_index
                    for action in transaction_actions
                    if action.log_index in expected_log_index_set
                )
            )
            if represented_log_indices != expected_log_indices:
                raise ValueError(
                    "configured PositionManager decoder did not represent every "
                    f"target-pool ModifyLiquidity log: {transaction_hash}"
                )
        decoded_actions.extend(transaction_actions)
    return decoded_actions, ownership_events


def _normalize_transaction_hash(value: Any, *, label: str) -> str:
    try:
        transaction_hash = coerce_hex_str(value).lower()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a 32-byte hex value") from exc
    if len(transaction_hash) != 66:
        raise ValueError(f"{label} must be a 32-byte hex value")
    try:
        bytes.fromhex(transaction_hash[2:])
    except ValueError as exc:
        raise ValueError(f"{label} must be a 32-byte hex value") from exc
    return transaction_hash


def _require_matching_response_hash(
    requested_hash: str,
    response_hash: Any,
    *,
    label: str,
) -> None:
    try:
        normalized_response_hash = _normalize_transaction_hash(
            response_hash,
            label=f"{label} hash",
        )
    except ValueError as exc:
        raise ValueError(
            f"{label} does not match requested candidate hash {requested_hash}"
        ) from exc
    if normalized_response_hash != requested_hash:
        raise ValueError(
            f"{label} does not match requested candidate hash {requested_hash}"
        )


def _require_matching_transaction_location(
    requested_hash: str,
    transaction: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[int, int]:
    location: list[int] = []
    for field_name in ("blockNumber", "transactionIndex"):
        transaction_value = _required_rpc_int_field(
            transaction,
            field_name,
            label=(
                "RPC transaction response for requested candidate hash "
                f"{requested_hash}"
            ),
        )
        receipt_value = _required_rpc_int_field(
            receipt,
            field_name,
            label=(
                "RPC receipt response for requested candidate hash "
                f"{requested_hash}"
            ),
        )
        if transaction_value != receipt_value:
            raise ValueError(
                "RPC transaction and receipt "
                f"{field_name} differ for requested candidate hash {requested_hash}"
            )
        location.append(transaction_value)
    return location[0], location[1]


def _required_rpc_int_field(
    payload: dict[str, Any],
    field_name: str,
    *,
    label: str,
) -> int:
    if field_name not in payload or payload[field_name] is None:
        raise ValueError(f"{label} is missing {field_name}")
    try:
        value = _int_from_rpc_value(payload[field_name])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} has invalid {field_name}") from exc
    if value < 0:
        raise ValueError(f"{label} has invalid {field_name}")
    return value


def _target_pool_modify_log_indices(
    receipt: dict[str, Any],
    config: Any,
) -> tuple[int, ...]:
    pool_manager = Web3.to_checksum_address(config.pool_manager)
    pool_id = coerce_hex_str(config.pool_id).lower()
    indices: list[int] = []
    for log in receipt.get("logs", []):
        if Web3.to_checksum_address(log["address"]) != pool_manager:
            continue
        topics = log.get("topics", [])
        if len(topics) < 2:
            continue
        if coerce_hex_str(topics[0]).lower() != V4_MODIFY_LIQUIDITY_TOPIC:
            continue
        if coerce_hex_str(topics[1]).lower() != pool_id:
            continue
        indices.append(_int_from_rpc_value(log["logIndex"]))
    return tuple(sorted(indices))


def _tx_receipt_sort_key(item: _RpcTransactionBundle) -> tuple[int, int, str]:
    return item.block_number, item.transaction_index, item.transaction_hash


def _int_from_rpc_value(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("RPC quantity cannot be boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return int.from_bytes(bytes(value), "big")
    raise TypeError("RPC quantity must be integer, string, or bytes")


def _block_timestamp_for_number(w3: Web3, block_number: int) -> int:
    return _block_timestamp_from_raw(_raw_get_block(w3, block_number, False))


def _coverage_block_header(w3: Web3, block_number: int) -> tuple[str, int]:
    raw_block = _raw_get_block(w3, block_number, False)
    observed_block_number = _required_rpc_int_field(
        raw_block,
        "number",
        label="RPC coverage block header",
    )
    if observed_block_number != block_number:
        raise ValueError(
            "RPC coverage block header returned block "
            f"{observed_block_number} for requested block {block_number}"
        )
    block_hash = _normalize_transaction_hash(
        raw_block.get("hash"),
        label="RPC coverage block hash",
    )
    timestamp_ms = _block_timestamp_from_raw(raw_block) * 1_000
    return block_hash, timestamp_ms


def _capture_rpc_coverage_endpoint(
    w3: Web3,
    start_block: int,
    end_block: int,
) -> _RpcCoverageEndpointSnapshot:
    start_block_hash, start_timestamp_ms = _coverage_block_header(w3, start_block)
    end_block_hash, end_timestamp_ms = _coverage_block_header(w3, end_block)
    return _RpcCoverageEndpointSnapshot(
        start_block_hash=start_block_hash,
        start_timestamp_ms=start_timestamp_ms,
        end_block_hash=end_block_hash,
        end_timestamp_ms=end_timestamp_ms,
    )


def _coverage_chain_id(w3: Web3, pool: str) -> int:
    observed_chain_id = _int_from_rpc_value(w3.eth.chain_id)
    expected_chain_id = pool_attribution_orientation(pool).chain_id
    if observed_chain_id != expected_chain_id:
        raise ValueError(
            f"{pool} RPC chain ID {observed_chain_id} does not match {expected_chain_id}"
        )
    return observed_chain_id


def _replayed_price_events_for_actions(
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
    decoded_actions: list[DecodedLiquidityAction],
) -> list[ReplayedEvent]:
    if not decoded_actions:
        return []
    state_view = w3.eth.contract(
        address=Web3.to_checksum_address(config.state_view),
        abi=STATE_VIEW_ABI,
    )
    replay_events = _pool_replay_events(w3, config, start_block, end_block)
    replay_events.extend(
        ReplayEvent(
            block_number=action.block_number,
            log_index=action.log_index,
            event_order=action.event_order,
            event_type=action.action_type,
            sqrt_price_x96=None,
            tick=None,
        )
        for action in decoded_actions
    )
    first_block = min(event.block_number for event in replay_events)
    initial_state = _state_seed_before_block(state_view, config, first_block)
    replayed_events = attach_event_time_state(replay_events, initial_state)
    action_keys = {
        (action.block_number, action.log_index, action.event_order) for action in decoded_actions
    }
    return [
        event
        for event in replayed_events
        if (event.block_number, event.log_index, event.event_order) in action_keys
    ]


def _pool_replay_events(
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
) -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    block_timestamps: dict[int, int] = {}
    for topic in (V4_INITIALIZE_TOPIC, V4_SWAP_TOPIC):
        logs = _fetch_logs_with_debug(
            w3,
            {
                "address": Web3.to_checksum_address(config.pool_manager),
                "topics": [topic, config.pool_id],
                "fromBlock": start_block,
                "toBlock": end_block,
            },
            context=f"[{config.name}] pool price logs {start_block:,}->{end_block:,}",
        )
        for log in logs:
            block_number = int(log["blockNumber"])
            block_timestamp = block_timestamps.get(block_number)
            if block_timestamp is None:
                block_timestamp = _block_timestamp_for_number(w3, block_number)
                block_timestamps[block_number] = block_timestamp
            if topic == V4_INITIALIZE_TOPIC:
                row = decode_initialize_row(log, block_timestamp, config)
            else:
                row = decode_swap_row(log, block_timestamp, config)
            events.append(
                ReplayEvent(
                    block_number=row.block_number,
                    log_index=row.log_index,
                    event_order=0,
                    event_type=row.event_type,
                    sqrt_price_x96=row.sqrt_price_x96,
                    tick=row.tick,
                )
            )
    return events


def _candidate_tx_hashes_from_csv(path: Path, start_block: int, end_block: int) -> list[str]:
    tx_hashes: set[str] = set()
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required_fields = {"block_number", "event_type", "tx_hash"}
        if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
            raise ValueError(
                f"candidate tx CSV missing required fields {sorted(required_fields)}: {path}"
            )
        for row in reader:
            if row["event_type"] not in _LP_CANDIDATE_EVENT_TYPES:
                continue
            block_number = int(row["block_number"])
            if block_number < start_block or block_number > end_block:
                continue
            tx_hash = row["tx_hash"].strip()
            if not tx_hash:
                continue
            tx_hashes.add(coerce_hex_str(tx_hash))
    return sorted(tx_hashes)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"expected a list of objects: {path}")
    return payload


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decoded_action_from_json(payload: dict[str, Any]) -> DecodedLiquidityAction:
    decimal_fields = {
        "amount0",
        "amount1",
        "amount0_actual",
        "amount1_actual",
        "collect_amount0",
        "collect_amount1",
    }
    normalized = {
        key: Decimal(str(value)) if key in decimal_fields else value
        for key, value in payload.items()
    }
    return DecodedLiquidityAction(**normalized)


def _render_ledger_rows(rows: Sequence[LPLedgerRow]) -> bytes:
    fieldnames = [field.name for field in fields(LPLedgerRow)]
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(
        handle,
        fieldnames=fieldnames,
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row))
    return handle.getvalue().encode("utf-8")


def _publish_ledger_pair(
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
) -> None:
    with _publication_lock(ledger_path):
        _publish_ledger_pair_locked(pool, ledger_path, ledger_bytes, coverage_bytes)


@contextmanager
def _publication_lock(ledger_path: Path) -> Iterator[None]:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_name(f"{ledger_path.name}.publish.lock")
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CrossPoolContractError(
                f"LP ledger publication is already active: {ledger_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publish_ledger_pair_locked(
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
) -> None:
    sidecar_path = ledger_coverage_path(ledger_path)
    ledger_exists = ledger_path.is_file()
    sidecar_exists = sidecar_path.is_file()
    if ledger_exists != sidecar_exists:
        raise CrossPoolContractError(
            "refusing to replace a partial LP ledger and coverage pair"
        )
    staged_ledger = _temporary_ledger_path(ledger_path, "stage")
    staged_sidecar = ledger_coverage_path(staged_ledger)
    backup_ledger: Path | None = None
    backup_sidecar: Path | None = None
    ledger_published = False
    sidecar_published = False
    preserve_backups = False
    try:
        _write_durable_bytes(staged_ledger, ledger_bytes)
        _write_durable_bytes(staged_sidecar, coverage_bytes)
        load_ledger_coverage(pool, staged_ledger)

        if ledger_exists:
            backup_ledger = _temporary_ledger_path(ledger_path, "backup")
            backup_sidecar = ledger_coverage_path(backup_ledger)
            _write_durable_bytes(backup_ledger, ledger_path.read_bytes())
            _write_durable_bytes(backup_sidecar, sidecar_path.read_bytes())
            load_ledger_coverage(pool, backup_ledger)

        # Publishing the ledger first makes every intermediate state fail closed
        # against the old or missing sidecar until the matching sidecar is visible.
        _replace_path(staged_ledger, ledger_path)
        ledger_published = True
        _replace_path(staged_sidecar, sidecar_path)
        sidecar_published = True
        load_ledger_coverage(pool, ledger_path)
    except BaseException:
        try:
            if backup_ledger is not None and backup_sidecar is not None:
                if ledger_published:
                    _replace_path(backup_ledger, ledger_path)
                if sidecar_published:
                    _replace_path(backup_sidecar, sidecar_path)
            else:
                if ledger_published:
                    ledger_path.unlink(missing_ok=True)
                if sidecar_published:
                    sidecar_path.unlink(missing_ok=True)
        except BaseException as rollback_error:
            preserve_backups = True
            retained_backups = tuple(
                path
                for path in (backup_ledger, backup_sidecar)
                if path is not None and path.exists()
            )
            retained_text = ", ".join(str(path) for path in retained_backups)
            raise RuntimeError(
                "LP ledger publication rollback failed; recover from retained "
                f"backup files: {retained_text}"
            ) from rollback_error
        raise
    finally:
        staged_ledger.unlink(missing_ok=True)
        staged_sidecar.unlink(missing_ok=True)
        if not preserve_backups:
            if backup_ledger is not None:
                backup_ledger.unlink(missing_ok=True)
            if backup_sidecar is not None:
                backup_sidecar.unlink(missing_ok=True)


def _temporary_ledger_path(ledger_path: Path, purpose: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{ledger_path.stem}.{purpose}.",
        suffix=".csv",
        dir=ledger_path.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def _write_durable_bytes(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_path(source: Path, destination: Path) -> None:
    source.replace(destination)


def _csv_row(row: LPLedgerRow) -> dict[str, object]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(row).items()
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, choices=sorted(POOL_CONFIGS))
    parser.add_argument("--start-block", required=True, type=int)
    parser.add_argument("--end-block", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--candidate-tx-csv", type=Path)
    parser.add_argument("--decoded-actions", type=Path)
    parser.add_argument("--ownership-events", type=Path)
    parser.add_argument("--price-events", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        fixture_paths = (args.decoded_actions, args.ownership_events, args.price_events)
        if all(path is not None for path in fixture_paths):
            count = export_fixture_lp_ledger(
                args.pool,
                args.start_block,
                args.end_block,
                args.decoded_actions,
                args.ownership_events,
                args.price_events,
                args.out,
            )
            print(f"wrote {count} LP ledger rows to {args.out}")
            return 0
        if any(path is not None for path in fixture_paths):
            raise ValueError(
                "provide all fixture inputs together: --decoded-actions, "
                "--ownership-events, and --price-events"
            )
        candidate_tx_hashes = (
            _candidate_tx_hashes_from_csv(
                args.candidate_tx_csv,
                args.start_block,
                args.end_block,
            )
            if args.candidate_tx_csv is not None
            else None
        )
        count = export_rpc_lp_ledger(
            args.pool,
            args.start_block,
            args.end_block,
            args.out,
            candidate_tx_hashes=candidate_tx_hashes,
        )
    except Exception as exc:
        print(
            render_safe_failure(
                pool=args.pool,
                phase="preflight",
                error_code="unknown_error",
                exception=exc,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"wrote {count} LP ledger rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
