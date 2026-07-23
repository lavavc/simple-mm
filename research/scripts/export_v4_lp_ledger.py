"""Export Uniswap v4 LP lifecycle ledger rows."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout
from web3 import Web3
from web3.exceptions import (
    MultipleFailedRequests,
    ProviderConnectionError,
    RequestTimedOut,
    TooManyRequests,
    Web3RPCError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.web3_utils import as_hexstr, coerce_hex_str  # noqa: E402
from research.backtester.lp_ledger_attribution import (  # noqa: E402
    FROZEN_REPLAY_EVENT_SOURCES,
    FROZEN_REPLAY_INPUT_FIELDS,
    FROZEN_REPLAY_PARSER_VERSION,
    FROZEN_REPLAY_PRICE_EVENT_TYPES,
    AcquisitionPolicyCoverage,
    BundleDigestCoverage,
    EligibleBundleCoverage,
    FrozenTokenCoverage,
    FullTransferCoverage,
    ReconciliationCoverage,
    ReplayCoverage,
    RpcLedgerEvidence,
    TransferChunkCoverage,
    WitnessSetCoverage,
    build_candidate_list_ledger_coverage_bytes,
    build_fixture_ledger_coverage_bytes,
    build_rpc_ledger_coverage_bytes,
    frozen_replay_evidence_sha256,
    frozen_replay_header_sha256,
    frozen_replay_parser_contract_sha256,
    frozen_replay_price_semantics_sha256,
    ledger_coverage_path,
    load_ledger_coverage,
    pool_attribution_orientation,
)
from research.backtester.lp_ledger_checkpoint import (  # noqa: E402
    CHECKPOINT_SCHEMA_VERSION,
    CHECKPOINT_SOURCE_PATHS,
    ActionDecodeIdentity,
    ActionPriceBinding,
    BlockHeader,
    BuildInputs,
    CandidateBundle,
    CheckpointContractError,
    CheckpointPaths,
    CheckpointSnapshot,
    DecoderStateUpsert,
    DiscoveryWitness,
    EndpointSnapshot,
    ErrorCode,
    EventTimeStateSource,
    LPLedgerCheckpoint,
    Phase,
    PositionKeyMapping,
    PositionResolution,
    ProgressSnapshot,
    PublicationState,
    ReplayInputEvidence,
    RunIdentity,
    acquire_run_lock,
    candidate_payload_sha256,
    fsync_directory,
    open_regular_leaf,
    progress_from_snapshot,
    render_safe_failure,
    should_write_progress,
    terminal_noncheckpoint_failure_progress,
    terminal_noncheckpoint_progress,
    validate_output_namespace,
    validate_regular_leaf,
    write_progress_atomically,
)
from research.backtester.pool_price_semantics import raw_sqrt_mid_from_row  # noqa: E402
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
    ExportPoolConfig,
    _block_timestamp_from_raw,
    _decode_position_info,
    _fetch_logs_with_debug,
    _make_web3,
    _pool_id_prefix_matches,
    _position_state_from_chain,
    _raw_get_block,
    _state_seed_before_block,
    decode_initialize_row,
    decode_swap_row,
)
from research.backtester.v4_lp_ledger import (  # noqa: E402
    ENTRYPOINT_V08_ADDRESS,
    DecodedLiquidityAction,
    LedgerPositionState,
    LPLedgerRow,
    OwnershipEvent,
    PoolPositionKey,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
)
from research.backtester.v4_lp_ledger import (  # noqa: E402
    _pool_key_matches as _ledger_pool_key_matches,
)
from research.cross_pool.contracts import CrossPoolContractError  # noqa: E402

_LP_CANDIDATE_EVENT_TYPES = frozenset({"mint", "burn", "collect"})
_RPC_QUANTITY_FIELDS = frozenset(
    {
        "baseFeePerGas",
        "blobGasPrice",
        "blobGasUsed",
        "blockNumber",
        "chainId",
        "cumulativeGasUsed",
        "depositNonce",
        "depositReceiptVersion",
        "effectiveGasPrice",
        "gas",
        "gasPrice",
        "gasUsed",
        "l1BaseFeeScalar",
        "l1BlobBaseFee",
        "l1BlobBaseFeeScalar",
        "l1Fee",
        "l1GasPrice",
        "l1GasUsed",
        "logIndex",
        "maxFeePerBlobGas",
        "maxFeePerGas",
        "maxPriorityFeePerGas",
        "nonce",
        "operatorFeeConstant",
        "operatorFeeScalar",
        "status",
        "transactionIndex",
        "type",
        "v",
        "value",
        "yParity",
    }
)
_RPC_HASH_FIELDS = frozenset({"blockHash", "hash", "transactionHash"})
_RPC_ADDRESS_FIELDS = frozenset(
    {"address", "contractAddress", "creates", "from", "to"}
)
_RPC_DATA_FIELDS = frozenset({"data", "input", "logsBloom", "r", "s"})
_POSITION_MANAGER_TRANSFER_TOPIC = str(
    coerce_hex_str(Web3.keccak(text="Transfer(address,address,uint256)").hex())
).lower()
_MAX_TRANSFER_LOG_QUERY_BLOCKS = 2_000
_TRANSFER_LOG_RETRY_ATTEMPTS = 3
_REPLAY_INPUT_PARSER_VERSION = FROZEN_REPLAY_PARSER_VERSION
_REPLAY_INPUT_FIELDS = FROZEN_REPLAY_INPUT_FIELDS
_REPLAY_EVENT_TYPES = frozenset(FROZEN_REPLAY_EVENT_SOURCES)
_PRICE_EVENT_TYPES = FROZEN_REPLAY_PRICE_EVENT_TYPES
_REPLAY_EVENT_SOURCES = FROZEN_REPLAY_EVENT_SOURCES
_CHECKPOINT_EXPORTER_VERSION = "lp-ledger-checkpoint-v2"
_FROZEN_REPLAY_PATHS = {
    "uni-base": REPO_ROOT / "research/data/derived/uni_base_pool_history_replay.csv",
    "uni-bsc": REPO_ROOT / "research/data/derived/uni_bsc_pool_history_replay.csv",
}


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


@dataclass(frozen=True)
class _FrozenReplayInput:
    evidence: ReplayInputEvidence
    price_events: tuple[ReplayEvent, ...]


class _CheckpointExportError(RuntimeError):
    def __init__(self, *, phase: Phase, error_code: ErrorCode) -> None:
        super().__init__("LP ledger export failed")
        self.phase = phase
        self.error_code = error_code


_ProgressCallback = Callable[[CheckpointSnapshot], None]


@dataclass
class _CheckpointProgressWriter:
    paths: CheckpointPaths
    previous: ProgressSnapshot | None = None

    def emit(self, snapshot: CheckpointSnapshot, *, force: bool = False) -> None:
        current = progress_from_snapshot(
            snapshot,
            datetime.now(timezone.utc),
            time.monotonic(),
            self.previous,
        )
        if force or should_write_progress(self.previous, current):
            write_progress_atomically(self.paths, current)
            self.previous = current


def _notify_progress(
    callback: _ProgressCallback | None,
    snapshot: CheckpointSnapshot,
) -> CheckpointSnapshot:
    if callback is not None:
        callback(snapshot)
    return snapshot


@contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    previous_handler: Any = None
    installed = False

    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, interrupt)
        installed = True
    except ValueError:
        pass
    try:
        yield
    finally:
        if installed and previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def _checkpoint_failure_code(
    phase: Phase,
    exception: Exception,
) -> ErrorCode:
    if isinstance(
        exception,
        (
            RequestsConnectionError,
            RequestsTimeout,
            MultipleFailedRequests,
            ProviderConnectionError,
            RequestTimedOut,
            TooManyRequests,
            Web3RPCError,
        ),
    ):
        return "rpc_error"
    if isinstance(exception, CheckpointContractError):
        return "checkpoint_corrupt"
    if phase in ("action_decode", "token_set_freeze", "relevant_transfer_decode"):
        return "decode_error"
    if phase in ("replay_input_bind", "price_replay"):
        return "replay_error"
    if phase in ("build", "publish"):
        return "publication_error"
    return "unknown_error"


def _record_noncheckpoint_failure(
    pool: str,
    mode: Literal["candidate_list_unverified", "fixture"],
    start_block: int,
    end_block: int,
    output_path: Path,
    phase: Phase,
    error_code: ErrorCode,
) -> None:
    write_progress_atomically(
        CheckpointPaths.from_output(output_path),
        terminal_noncheckpoint_failure_progress(
            pool=pool,
            mode=mode,
            start_block=start_block,
            end_block=end_block,
            output_filename=output_path.name,
            phase=phase,
            error_code=error_code,
            now=datetime.now(timezone.utc),
        ),
    )


def _stage_action_discovery(
    run: LPLedgerCheckpoint,
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
    *,
    on_progress: _ProgressCallback | None = None,
) -> CheckpointSnapshot:
    _validate_frozen_range(start_block, end_block)
    snapshot = run.snapshot()
    for block_range in run.incomplete_action_chunks():
        if (
            block_range.start_block < start_block
            or block_range.end_block > end_block
        ):
            raise ValueError("checkpoint action chunk is outside the frozen range")
        logs = _fetch_logs_with_debug(
            w3,
            {
                "address": Web3.to_checksum_address(config.pool_manager),
                "topics": [V4_MODIFY_LIQUIDITY_TOPIC, config.pool_id],
                "fromBlock": block_range.start_block,
                "toBlock": block_range.end_block,
            },
            context=(
                f"[{config.name}] target-pool action logs "
                f"{block_range.start_block:,}->{block_range.end_block:,}"
            ),
        )
        witnesses = tuple(
            sorted(
                (
                    _normalize_action_discovery_witness(log, config)
                    for log in logs
                ),
                key=_discovery_witness_sort_key,
            )
        )
        snapshot = run.commit_action_chunk(block_range, witnesses)
        _notify_progress(on_progress, snapshot)
    return run.snapshot()


def _normalize_action_discovery_witness(
    raw_log: object,
    config: Any,
) -> DiscoveryWitness:
    if not isinstance(raw_log, Mapping):
        raise ValueError("target-pool action log must be a mapping")
    payload = dict(raw_log)
    removed = payload.get("removed")
    if removed is not None and (type(removed) is not bool or removed):
        raise ValueError("target-pool action log is removed")
    block_number = _required_rpc_int_field(
        payload,
        "blockNumber",
        label="target-pool action log",
    )
    transaction_index = _required_rpc_int_field(
        payload,
        "transactionIndex",
        label="target-pool action log",
    )
    log_index = _required_rpc_int_field(
        payload,
        "logIndex",
        label="target-pool action log",
    )
    raw_topics = payload.get("topics")
    if (
        not isinstance(raw_topics, Sequence)
        or isinstance(raw_topics, (str, bytes, bytearray, memoryview))
        or not raw_topics
    ):
        raise ValueError("target-pool action log topics are invalid")
    topics = tuple(
        _normalize_fixed_hex(topic, 32, f"target-pool action topic {index}")
        for index, topic in enumerate(raw_topics)
    )
    witness = DiscoveryWitness(
        source="pool_modify",
        block_number=block_number,
        block_hash=_normalize_fixed_hex(
            payload.get("blockHash"),
            32,
            "target-pool action block hash",
        ),
        transaction_hash=_normalize_transaction_hash(
            payload.get("transactionHash"),
            label="target-pool action transaction hash",
        ),
        transaction_index=transaction_index,
        log_index=log_index,
        address=_normalize_fixed_hex(
            payload.get("address"),
            20,
            "target-pool action address",
        ),
        topics=topics,
        data=_normalize_hex_data(payload.get("data"), "target-pool action data"),
    )
    expected_address = _normalize_fixed_hex(
        config.pool_manager,
        20,
        "configured PoolManager",
    )
    expected_topic = _normalize_fixed_hex(
        V4_MODIFY_LIQUIDITY_TOPIC,
        32,
        "configured ModifyLiquidity topic",
    )
    expected_pool = _normalize_fixed_hex(
        config.pool_id,
        32,
        "configured pool ID",
    )
    if (
        witness.address != expected_address
        or len(witness.topics) < 2
        or witness.topics[0] != expected_topic
        or witness.topics[1] != expected_pool
    ):
        raise ValueError("target-pool action log identity is invalid")
    return witness


def _stage_action_bundles(
    run: LPLedgerCheckpoint,
    w3: Web3,
    start_block: int,
    end_block: int,
    *,
    on_progress: _ProgressCallback | None = None,
) -> CheckpointSnapshot:
    _validate_frozen_range(start_block, end_block)
    snapshot = run.snapshot()
    pending_hashes = run.unfetched_action_hashes()
    for transaction_hash in pending_hashes:
        witnesses = run.action_witnesses(transaction_hash)
        bundle = _fetch_and_validate_candidate_bundle(
            w3,
            transaction_hash,
            witnesses,
            start_block,
            end_block,
        )
        snapshot = run.commit_action_bundle(bundle)
        _notify_progress(on_progress, snapshot)
    if (
        not pending_hashes
        and snapshot.phase == "action_fetch"
        and not run.action_candidate_hashes()
    ):
        snapshot = run.complete_phase("action_fetch")
        _notify_progress(on_progress, snapshot)
    return snapshot


def _stage_action_decode(
    run: LPLedgerCheckpoint,
    w3: Web3,
    config: ExportPoolConfig,
    *,
    on_progress: _ProgressCallback | None = None,
) -> CheckpointSnapshot:
    identity = run.action_decode_identity()
    _require_matching_action_decode_identity(identity, config)
    token_state = run.load_decoder_state()
    position_keys = {
        mapping.token_id: PoolPositionKey(
            pool_id=mapping.pool_id,
            tick_lower=mapping.tick_lower,
            tick_upper=mapping.tick_upper,
            salt=mapping.salt,
        )
        for mapping in run.load_position_keys()
    }
    position_manager = w3.eth.contract(
        address=Web3.to_checksum_address(config.position_manager),
        abi=POSITION_MANAGER_ABI,
    )
    pending_bundles = run.undecoded_action_bundles()
    snapshot = run.snapshot()
    for bundle in pending_bundles:
        transaction = _canonical_bundle_mapping(
            bundle.transaction_json,
            "candidate transaction JSON",
        )
        receipt = _canonical_bundle_mapping(
            bundle.receipt_json,
            "candidate receipt JSON",
        )
        headers = {
            bundle.block_number: _load_or_fetch_decode_header(
                run,
                w3,
                bundle.block_number,
                expected_hash=bundle.block_hash,
            )
        }
        new_resolutions: dict[tuple[int, int], PositionResolution] = {}

        def resolve_position(
            token_id: int,
            query_block: int,
        ) -> LedgerPositionState | None:
            key = (token_id, query_block)
            pending_resolution = new_resolutions.get(key)
            if pending_resolution is not None:
                return pending_resolution.state
            cached_resolution = run.load_position_resolution(token_id, query_block)
            if cached_resolution is not None:
                return cached_resolution.state
            if query_block not in headers:
                headers[query_block] = _load_or_fetch_decode_header(
                    run,
                    w3,
                    query_block,
                )
            state = _strict_position_state_from_chain(
                token_id,
                position_manager,
                config,
                query_block,
            )
            new_resolutions[key] = PositionResolution(
                token_id=token_id,
                query_block=query_block,
                found=state is not None,
                state=state,
            )
            return state

        prior_state = dict(token_state)
        prior_keys = dict(position_keys)
        actions = decode_liquidity_actions_for_tx(
            transaction,
            receipt,
            headers[bundle.block_number].timestamp_ms // 1_000,
            config,
            token_state,
            position_keys,
            wrapper_entrypoint=identity.wrapper_entrypoint,
            position_resolver=resolve_position,
        )
        action_token_ids = {action.token_id for action in actions}
        for token_id in tuple(token_state):
            if token_id not in prior_state and token_id not in action_token_ids:
                token_state.pop(token_id)
                if token_id not in prior_keys:
                    position_keys.pop(token_id, None)
        state_upserts, state_deletes = _decoder_state_changes(
            prior_state,
            token_state,
            actions,
        )
        position_key_mappings = _new_position_key_mappings(
            prior_keys,
            position_keys,
            actions,
            receipt,
            config,
        )
        snapshot = run.commit_decoded_action_transaction(
            bundle,
            headers=tuple(headers[number] for number in sorted(headers)),
            resolutions=tuple(
                new_resolutions[key] for key in sorted(new_resolutions)
            ),
            state_upserts=state_upserts,
            state_deletes=state_deletes,
            actions=actions,
            position_keys=position_key_mappings,
        )
        _notify_progress(on_progress, snapshot)
    if (
        not pending_bundles
        and snapshot.phase == "action_decode"
        and not run.undecoded_action_bundles()
    ):
        snapshot = run.complete_phase("action_decode")
        _notify_progress(on_progress, snapshot)
    return snapshot


def _freeze_action_token_set(run: LPLedgerCheckpoint) -> CheckpointSnapshot:
    snapshot = run.snapshot()
    if snapshot.phase != "token_set_freeze":
        raise ValueError("action token set cannot freeze before action decode completes")
    if run.undecoded_action_bundles():
        raise ValueError("action token set cannot freeze with undecoded bundles")
    return run.freeze_action_token_set()


def _stage_full_transfer_scan(
    run: LPLedgerCheckpoint,
    w3: Web3,
    config: ExportPoolConfig,
    start_block: int,
    end_block: int,
    *,
    on_progress: _ProgressCallback | None = None,
) -> CheckpointSnapshot:
    _validate_frozen_range(start_block, end_block)
    transfer_w3 = _transfer_scan_web3(w3, config)
    frozen_token_ids = frozenset(run.frozen_token_ids())
    snapshot = run.snapshot()
    for block_range in run.incomplete_transfer_chunks():
        if (
            block_range.start_block < start_block
            or block_range.end_block > end_block
        ):
            raise ValueError("checkpoint transfer chunk is outside the frozen range")
        raw_logs = _fetch_bounded_position_transfer_logs(
            transfer_w3,
            config,
            block_range.start_block,
            block_range.end_block,
        )
        witnesses = tuple(
            sorted(
                (
                    _normalize_position_transfer_witness(raw_log, config)
                    for raw_log in raw_logs
                ),
                key=_discovery_witness_sort_key,
            )
        )
        _validate_complete_transfer_witnesses(
            witnesses,
            block_range.start_block,
            block_range.end_block,
        )
        relevant_witnesses = tuple(
            witness
            for witness in witnesses
            if int(witness.topics[3], 16) in frozen_token_ids
        )
        snapshot = run.commit_transfer_chunk(
            block_range,
            unfiltered_count=len(witnesses),
            unfiltered_sha256=_transfer_witnesses_sha256(witnesses),
            relevant_witnesses=relevant_witnesses,
        )
        _notify_progress(on_progress, snapshot)
    return snapshot


def _transfer_scan_web3(
    w3: Web3,
    config: ExportPoolConfig,
) -> Web3:
    provider = getattr(w3, "provider", None)
    if provider is None:
        return w3
    if not isinstance(provider, Web3.HTTPProvider):
        raise ValueError("transfer scan requires an HTTP provider")
    if _rpc_provider_origin(str(provider.endpoint_uri)) != _rpc_provider_origin(
        config.rpc_url
    ):
        raise ValueError("transfer scan provider origin does not match its config")
    if provider.exception_retry_configuration is None:
        return w3
    return Web3(
        Web3.HTTPProvider(
            config.rpc_url,
            exception_retry_configuration=None,
        )
    )


def _rpc_provider_origin(rpc_url: str) -> str:
    parsed = urlsplit(rpc_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("RPC provider URL is invalid") from exc
    hostname = parsed.hostname
    effective_port = 443 if port is None else port
    if (
        parsed.scheme not in ("http", "https")
        or hostname is None
        or not 1 <= effective_port <= 65_535
        or ":" in hostname
    ):
        raise ValueError("RPC provider URL is invalid")
    origin = f"{parsed.scheme}://{hostname.lower()}"
    return origin if port is None else f"{origin}:{port}"


def _fetch_bounded_position_transfer_logs(
    w3: Web3,
    config: ExportPoolConfig,
    start_block: int,
    end_block: int,
) -> list[object]:
    logs: list[object] = []
    query_start = start_block
    while query_start <= end_block:
        query_end = min(
            query_start + _MAX_TRANSFER_LOG_QUERY_BLOCKS - 1,
            end_block,
        )
        logs.extend(
            _fetch_position_transfer_query_with_subdivision(
                w3,
                config,
                query_start,
                query_end,
            )
        )
        query_start = query_end + 1
    return logs


def _fetch_position_transfer_query_with_subdivision(
    w3: Web3,
    config: ExportPoolConfig,
    start_block: int,
    end_block: int,
) -> list[object]:
    failure: Exception | None = None
    for _attempt in range(_TRANSFER_LOG_RETRY_ATTEMPTS):
        try:
            response = _fetch_logs_with_debug(
                w3,
                {
                    "address": Web3.to_checksum_address(config.position_manager),
                    "topics": [_POSITION_MANAGER_TRANSFER_TOPIC],
                    "fromBlock": start_block,
                    "toBlock": end_block,
                },
                context=(
                    f"[{config.name}] PositionManager Transfer logs "
                    f"{start_block:,}->{end_block:,}"
                ),
            )
        except Exception as exc:
            if not _is_retryable_transfer_log_error(exc):
                raise
            failure = exc
            continue
        if not isinstance(response, list):
            raise ValueError(
                "PositionManager Transfer response is malformed or explicitly truncated"
            )
        return cast(list[object], response)
    if failure is None:
        raise AssertionError("transfer log retry loop ended without a result")
    if start_block == end_block:
        raise failure
    midpoint = start_block + (end_block - start_block) // 2
    return _fetch_position_transfer_query_with_subdivision(
        w3,
        config,
        start_block,
        midpoint,
    ) + _fetch_position_transfer_query_with_subdivision(
        w3,
        config,
        midpoint + 1,
        end_block,
    )


def _is_retryable_transfer_log_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            RequestsConnectionError,
            RequestsTimeout,
            ConnectionError,
            TimeoutError,
            MultipleFailedRequests,
            ProviderConnectionError,
            RequestTimedOut,
            TooManyRequests,
        ),
    ):
        return True
    message = str(exc).lower()
    retryable_markers = (
        "timeout",
        "timed out",
        "rate limit",
        "too many requests",
        "response size",
        "result limit",
        "query returned more than",
        "block range",
        "capacity",
        "status=408",
        "status=400",
        "status=413",
        "status=429",
        "status=500",
        "status=502",
        "status=503",
        "status=504",
        "status=unavailable",
    )
    if isinstance(exc, Web3RPCError):
        return any(marker in message for marker in retryable_markers)
    if isinstance(exc, RuntimeError) and message.startswith(
        "rpc log request failed"
    ):
        return any(marker in message for marker in retryable_markers)
    return False


def _normalize_position_transfer_witness(
    raw_log: object,
    config: ExportPoolConfig,
) -> DiscoveryWitness:
    if not isinstance(raw_log, Mapping):
        raise ValueError("PositionManager Transfer log must be a mapping")
    payload = dict(raw_log)
    removed = payload.get("removed")
    if removed is not None and (type(removed) is not bool or removed):
        raise ValueError("PositionManager Transfer log is removed")
    raw_topics = payload.get("topics")
    if (
        not isinstance(raw_topics, Sequence)
        or isinstance(raw_topics, (str, bytes, bytearray, memoryview))
        or len(raw_topics) != 4
    ):
        raise ValueError("PositionManager Transfer log topics are invalid")
    topics = tuple(
        _normalize_fixed_hex(topic, 32, f"PositionManager Transfer topic {index}")
        for index, topic in enumerate(raw_topics)
    )
    witness = DiscoveryWitness(
        source="position_transfer",
        block_number=_required_rpc_int_field(
            payload,
            "blockNumber",
            label="PositionManager Transfer log",
        ),
        block_hash=_normalize_fixed_hex(
            payload.get("blockHash"),
            32,
            "PositionManager Transfer block hash",
        ),
        transaction_hash=_normalize_transaction_hash(
            payload.get("transactionHash"),
            label="PositionManager Transfer transaction hash",
        ),
        transaction_index=_required_rpc_int_field(
            payload,
            "transactionIndex",
            label="PositionManager Transfer log",
        ),
        log_index=_required_rpc_int_field(
            payload,
            "logIndex",
            label="PositionManager Transfer log",
        ),
        address=_normalize_fixed_hex(
            payload.get("address"),
            20,
            "PositionManager Transfer address",
        ),
        topics=topics,
        data=_normalize_hex_data(
            payload.get("data"),
            "PositionManager Transfer data",
        ),
    )
    if (
        witness.address
        != _normalize_fixed_hex(
            config.position_manager,
            20,
            "configured PositionManager",
        )
        or witness.topics[0] != _POSITION_MANAGER_TRANSFER_TOPIC
        or witness.data != "0x"
    ):
        raise ValueError("PositionManager Transfer log identity is invalid")
    if witness.topics[1][2:26] != "0" * 24 or witness.topics[2][2:26] != "0" * 24:
        raise ValueError("PositionManager Transfer owner topic is not canonical")
    return witness


def _validate_complete_transfer_witnesses(
    witnesses: tuple[DiscoveryWitness, ...],
    start_block: int,
    end_block: int,
) -> None:
    if witnesses != tuple(sorted(witnesses, key=_discovery_witness_sort_key)):
        raise ValueError("PositionManager Transfer logs are not canonically ordered")
    identities: set[tuple[str, int]] = set()
    locations: set[tuple[int, int]] = set()
    block_hashes: dict[int, str] = {}
    transaction_locations: dict[str, tuple[int, str, int]] = {}
    transaction_slots: dict[tuple[int, int], str] = {}
    for witness in witnesses:
        if not start_block <= witness.block_number <= end_block:
            raise ValueError("PositionManager Transfer log is outside its query range")
        identity = (witness.transaction_hash, witness.log_index)
        location = (witness.block_number, witness.log_index)
        if identity in identities or location in locations:
            raise ValueError("PositionManager Transfer log identity is duplicated")
        identities.add(identity)
        locations.add(location)
        prior_hash = block_hashes.setdefault(
            witness.block_number,
            witness.block_hash,
        )
        if prior_hash != witness.block_hash:
            raise ValueError("PositionManager Transfer block has conflicting hashes")
        transaction_location = (
            witness.block_number,
            witness.block_hash,
            witness.transaction_index,
        )
        prior_location = transaction_locations.setdefault(
            witness.transaction_hash,
            transaction_location,
        )
        if prior_location != transaction_location:
            raise ValueError("PositionManager Transfer transaction location conflicts")
        transaction_slot = (witness.block_number, witness.transaction_index)
        prior_transaction_hash = transaction_slots.setdefault(
            transaction_slot,
            witness.transaction_hash,
        )
        if prior_transaction_hash != witness.transaction_hash:
            raise ValueError("PositionManager Transfer transaction slot conflicts")


def _transfer_witnesses_sha256(
    witnesses: tuple[DiscoveryWitness, ...],
) -> str:
    payload = _canonical_rpc_json([asdict(witness) for witness in witnesses])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stage_relevant_transfer_bundles(
    run: LPLedgerCheckpoint,
    w3: Web3,
    start_block: int,
    end_block: int,
    *,
    on_progress: _ProgressCallback | None = None,
) -> CheckpointSnapshot:
    _validate_frozen_range(start_block, end_block)
    pending_hashes = run.unfetched_relevant_transfer_hashes()
    snapshot = run.snapshot()
    for transaction_hash in pending_hashes:
        reused = run.reuse_staged_bundle_for_relevant_transfer(transaction_hash)
        if reused is not None:
            snapshot = reused
            _notify_progress(on_progress, snapshot)
            continue
        bundle = _fetch_and_validate_candidate_bundle(
            w3,
            transaction_hash,
            run.relevant_transfer_witnesses(transaction_hash),
            start_block,
            end_block,
        )
        snapshot = run.commit_relevant_transfer_bundle(bundle)
        _notify_progress(on_progress, snapshot)
    if (
        not pending_hashes
        and snapshot.phase == "relevant_transfer_fetch"
        and not run.relevant_transfer_transaction_hashes()
    ):
        snapshot = run.complete_phase("relevant_transfer_fetch")
        _notify_progress(on_progress, snapshot)
    return snapshot


def _stage_relevant_transfer_decode(
    run: LPLedgerCheckpoint,
    config: ExportPoolConfig,
    *,
    on_progress: _ProgressCallback | None = None,
) -> CheckpointSnapshot:
    frozen_token_ids = frozenset(run.frozen_token_ids())
    pending_bundles = run.undecoded_relevant_transfer_bundles()
    snapshot = run.snapshot()
    for bundle in pending_bundles:
        receipt = _canonical_bundle_mapping(
            bundle.receipt_json,
            "candidate receipt JSON",
        )
        owners = tuple(
            sorted(
                (
                    owner
                    for owner in decode_ownership_events_from_receipt(
                        receipt,
                        config.position_manager,
                    )
                    if owner.token_id in frozen_token_ids
                ),
                key=lambda owner: (
                    owner.block_number,
                    owner.log_index,
                    owner.event_order,
                    owner.token_id,
                ),
            )
        )
        snapshot = run.commit_decoded_relevant_transfer_transaction(
            bundle,
            owners=owners,
        )
        _notify_progress(on_progress, snapshot)
    if (
        not pending_bundles
        and snapshot.phase == "relevant_transfer_decode"
        and not run.relevant_transfer_transaction_hashes()
    ):
        snapshot = run.complete_phase("relevant_transfer_decode")
        _notify_progress(on_progress, snapshot)
    return snapshot


def _load_frozen_replay_input(
    path: Path,
    config: ExportPoolConfig,
) -> _FrozenReplayInput:
    normalized_path = path.resolve()
    try:
        raw_bytes = normalized_path.read_bytes()
    except OSError as exc:
        raise ValueError("frozen replay input is unreadable") from exc
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("frozen replay input must use UTF-8") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None or tuple(reader.fieldnames) != _REPLAY_INPUT_FIELDS:
        raise ValueError("frozen replay input header is not the exact frozen header")

    price_events: list[ReplayEvent] = []
    row_count = 0
    first_block: int | None = None
    last_block: int | None = None
    first_timestamp_ms: int | None = None
    last_timestamp_ms: int | None = None
    previous_order: tuple[int, int] | None = None
    previous_timestamp_ms: int | None = None
    identities: set[tuple[str, int]] = set()
    for row_number, raw_row in enumerate(reader, start=2):
        if set(raw_row) != set(_REPLAY_INPUT_FIELDS) or not all(
            isinstance(value, str) for value in raw_row.values()
        ):
            raise ValueError(f"frozen replay row {row_number} is malformed")
        row = cast(dict[str, str], raw_row)
        chain = _replay_required_text(row, "chain", row_number)
        pool_id = _replay_required_text(row, "pool_id", row_number)
        token0_symbol = _replay_required_text(row, "token0_symbol", row_number)
        token1_symbol = _replay_required_text(row, "token1_symbol", row_number)
        if (
            chain != config.chain
            or pool_id != config.pool_id
            or token0_symbol != config.token0_symbol
            or token1_symbol != config.token1_symbol
        ):
            raise ValueError(f"frozen replay row {row_number} violates pool identity")

        event_type = _replay_required_text(row, "event_type", row_number)
        if event_type not in _REPLAY_EVENT_TYPES:
            raise ValueError(f"frozen replay row {row_number} has unsupported event type")
        if _replay_required_text(
            row,
            "event_source",
            row_number,
        ) not in _REPLAY_EVENT_SOURCES[event_type]:
            raise ValueError(f"frozen replay row {row_number} has invalid event source")

        transaction_hash = _normalize_transaction_hash(
            _replay_required_text(row, "tx_hash", row_number),
            label=f"frozen replay row {row_number} transaction hash",
        )
        if transaction_hash != row["tx_hash"]:
            raise ValueError(f"frozen replay row {row_number} transaction hash is not canonical")
        block_number = _replay_canonical_int(
            row,
            "block_number",
            row_number,
            positive=True,
        )
        log_index = _replay_canonical_int(
            row,
            "log_index",
            row_number,
            positive=False,
        )
        order = (block_number, log_index)
        if previous_order is not None and order <= previous_order:
            raise ValueError("frozen replay rows must use strict block/log order")
        identity = (transaction_hash, log_index)
        if identity in identities:
            raise ValueError("frozen replay row identity is duplicated")
        identities.add(identity)

        timestamp_ms = _replay_timestamp_ms(
            _replay_required_text(row, "block_time", row_number),
            row_number,
        )
        if previous_timestamp_ms is not None and timestamp_ms < previous_timestamp_ms:
            raise ValueError("frozen replay timestamps must be nondecreasing")

        if event_type in _PRICE_EVENT_TYPES:
            sqrt_price_x96 = _replay_canonical_int(
                row,
                "sqrt_price_x96",
                row_number,
                positive=True,
            )
            tick = _replay_canonical_int(
                row,
                "tick",
                row_number,
                positive=None,
            )
            raw_sqrt_mid_from_row(row)
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

        row_count += 1
        first_block = block_number if first_block is None else first_block
        first_timestamp_ms = timestamp_ms if first_timestamp_ms is None else first_timestamp_ms
        last_block = block_number
        last_timestamp_ms = timestamp_ms
        previous_order = order
        previous_timestamp_ms = timestamp_ms

    if (
        row_count == 0
        or first_block is None
        or last_block is None
        or first_timestamp_ms is None
        or last_timestamp_ms is None
        or not price_events
    ):
        raise ValueError("frozen replay input has no usable rows")
    evidence = ReplayInputEvidence(
        path=str(normalized_path),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        byte_length=len(raw_bytes),
        row_count=row_count,
        header_sha256=frozen_replay_header_sha256(),
        parser_version=_REPLAY_INPUT_PARSER_VERSION,
        parser_contract_sha256=frozen_replay_parser_contract_sha256(),
        price_semantics_sha256=frozen_replay_price_semantics_sha256(),
        chain=config.chain,
        pool_id=config.pool_id,
        first_block=first_block,
        last_block=last_block,
        first_timestamp_ms=first_timestamp_ms,
        last_timestamp_ms=last_timestamp_ms,
        price_event_count=len(price_events),
        price_events_sha256=frozen_replay_evidence_sha256(
            [asdict(event) for event in price_events]
        ),
    )
    return _FrozenReplayInput(evidence=evidence, price_events=tuple(price_events))


def _bind_frozen_replay_input(
    run: LPLedgerCheckpoint,
    config: ExportPoolConfig,
    expected: ReplayInputEvidence,
) -> CheckpointSnapshot:
    if run.snapshot().phase != "replay_input_bind":
        raise ValueError("frozen replay input cannot bind in the current phase")
    observed = _load_frozen_replay_input(Path(expected.path), config)
    if observed.evidence != expected:
        raise ValueError("frozen replay input changed after preflight")
    return run.commit_replay_input(observed.evidence)


def _stage_price_replay(
    run: LPLedgerCheckpoint,
    config: ExportPoolConfig,
) -> CheckpointSnapshot:
    if run.snapshot().phase != "price_replay":
        raise ValueError("price replay cannot run in the current phase")
    expected = run.load_replay_input()
    frozen = _load_frozen_replay_input(Path(expected.path), config)
    if frozen.evidence != expected:
        raise ValueError("frozen replay input changed after binding")
    actions = run.load_decoded_actions()
    action_keys = tuple(
        (action.block_number, action.log_index, action.event_order)
        for action in actions
    )
    if action_keys != tuple(sorted(set(action_keys))):
        raise ValueError("decoded actions are not uniquely canonically ordered")
    price_keys = {
        (event.block_number, event.log_index, event.event_order)
        for event in frozen.price_events
    }
    if price_keys.intersection(action_keys):
        raise ValueError("frozen price event and decoded action identities overlap")
    replay_events = [*frozen.price_events]
    replay_events.extend(
        ReplayEvent(
            block_number=action.block_number,
            log_index=action.log_index,
            event_order=action.event_order,
            event_type=action.action_type,
            sqrt_price_x96=None,
            tick=None,
        )
        for action in actions
    )
    replayed = attach_event_time_state(replay_events, None)
    action_key_set = set(action_keys)
    by_action_key = {
        (event.block_number, event.log_index, event.event_order): event
        for event in replayed
        if (event.block_number, event.log_index, event.event_order) in action_key_set
    }
    if tuple(sorted(by_action_key)) != action_keys:
        raise ValueError("local replay did not produce exactly one state per action")
    bindings = tuple(
        ActionPriceBinding(
            block_number=key[0],
            log_index=key[1],
            event_order=key[2],
            event_time_sqrt_price_x96=by_action_key[key].event_time_sqrt_price_x96,
            event_time_tick=by_action_key[key].event_time_tick,
            event_time_state_source=cast(
                EventTimeStateSource,
                by_action_key[key].event_time_state_source,
            ),
        )
        for key in action_keys
    )
    return run.commit_action_price_bindings(bindings)


def _build_rpc_ledger_pair_from_checkpoint(
    run: LPLedgerCheckpoint,
    identity: RunIdentity,
    config: ExportPoolConfig,
) -> tuple[bytes, bytes, int]:
    inputs = run.load_build_inputs()
    binding_by_key = {
        (binding.block_number, binding.log_index, binding.event_order): binding
        for binding in inputs.action_price_bindings
    }
    if len(binding_by_key) != len(inputs.action_price_bindings):
        raise ValueError("action price bindings are duplicated")
    replayed_actions: list[ReplayedEvent] = []
    for action in inputs.actions:
        key = (action.block_number, action.log_index, action.event_order)
        binding = binding_by_key.get(key)
        if binding is None:
            raise ValueError("action price binding is missing")
        replayed_actions.append(
            ReplayedEvent(
                block_number=action.block_number,
                log_index=action.log_index,
                event_order=action.event_order,
                event_type=action.action_type,
                sqrt_price_x96=None,
                tick=None,
                event_time_sqrt_price_x96=binding.event_time_sqrt_price_x96,
                event_time_tick=binding.event_time_tick,
                event_time_state_source=binding.event_time_state_source,
            )
        )
    rows = build_lp_ledger_rows(
        inputs.actions,
        inputs.ownership_events,
        replayed_actions,
    )
    ledger_bytes = _render_ledger_rows(rows)
    evidence = _rpc_ledger_evidence(identity, config, inputs)
    coverage_bytes = build_rpc_ledger_coverage_bytes(
        identity.pool,
        ledger_bytes,
        chain_id=identity.chain_id,
        covered_start_block=identity.start_block,
        covered_start_block_hash=identity.endpoint.start.block_hash,
        covered_start_timestamp_ms=identity.endpoint.start.timestamp_ms,
        covered_end_block=identity.end_block,
        covered_end_block_hash=identity.endpoint.end.block_hash,
        covered_end_timestamp_ms=identity.endpoint.end.timestamp_ms,
        evidence=evidence,
    )
    return ledger_bytes, coverage_bytes, len(rows)


def _rpc_ledger_evidence(
    identity: RunIdentity,
    config: ExportPoolConfig,
    inputs: BuildInputs,
) -> RpcLedgerEvidence:
    action_witnesses = _witness_set_coverage(
        inputs.action_witnesses,
        inputs.action_transaction_hashes,
    )
    relevant_witnesses = _witness_set_coverage(
        inputs.relevant_transfer_witnesses,
        inputs.relevant_transfer_transaction_hashes,
    )
    token_ids = tuple(str(token_id) for token_id in inputs.frozen_token_ids)
    frozen_tokens = FrozenTokenCoverage(
        token_count=len(token_ids),
        token_ids_sha256=_coverage_digest(list(token_ids)),
        token_ids=token_ids,
    )
    transfer_chunks = tuple(
        TransferChunkCoverage(
            index=value.index,
            start_block=value.start_block,
            end_block=value.end_block,
            unfiltered_count=value.unfiltered_count,
            unfiltered_sha256=value.unfiltered_sha256,
        )
        for value in inputs.transfer_chunk_attestations
    )
    full_transfer = FullTransferCoverage(
        position_manager=config.position_manager.lower(),
        transfer_topic=identity.transfer_topic,
        start_block=identity.start_block,
        end_block=identity.end_block,
        log_count=sum(chunk.unfiltered_count for chunk in transfer_chunks),
        chunks_sha256=_coverage_digest([asdict(chunk) for chunk in transfer_chunks]),
        chunks=transfer_chunks,
    )
    bundles = tuple(
        sorted(
            (
                BundleDigestCoverage(
                    transaction_hash=bundle.transaction_hash,
                    payload_sha256=bundle.payload_sha256,
                )
                for bundle in inputs.eligible_bundles
            ),
            key=lambda value: value.transaction_hash,
        )
    )
    eligible_hashes = tuple(bundle.transaction_hash for bundle in bundles)
    eligible = EligibleBundleCoverage(
        transaction_count=len(eligible_hashes),
        transactions_sha256=_coverage_digest(list(eligible_hashes)),
        bundles_sha256=_coverage_digest([asdict(bundle) for bundle in bundles]),
        transaction_hashes=eligible_hashes,
        bundles=bundles,
    )
    replay = inputs.replay_input
    replay_coverage = ReplayCoverage(
        sha256=replay.sha256,
        byte_length=replay.byte_length,
        row_count=replay.row_count,
        header_sha256=replay.header_sha256,
        parser_version=replay.parser_version,
        parser_contract_sha256=replay.parser_contract_sha256,
        price_semantics_sha256=replay.price_semantics_sha256,
        chain=replay.chain,
        pool_id=replay.pool_id,
        first_block=replay.first_block,
        last_block=replay.last_block,
        first_timestamp_ms=replay.first_timestamp_ms,
        last_timestamp_ms=replay.last_timestamp_ms,
        price_event_count=replay.price_event_count,
        price_events_sha256=replay.price_events_sha256,
    )
    action_records = _action_reconciliation_records(
        inputs.action_witnesses,
        inputs.actions,
    )
    ownership_records = _ownership_reconciliation_records(
        inputs.relevant_transfer_witnesses,
        inputs.ownership_events,
    )
    return RpcLedgerEvidence(
        rpc_provider_origin=identity.rpc_provider_origin,
        acquisition_policy=AcquisitionPolicyCoverage(
            max_blocks_per_log_query=_MAX_TRANSFER_LOG_QUERY_BLOCKS,
            retry_attempts=_TRANSFER_LOG_RETRY_ATTEMPTS,
            subdivision="sequential_binary",
            hidden_provider_retries="disabled",
            completeness="provider_conditioned",
        ),
        action_witnesses=action_witnesses,
        frozen_token_ids=frozen_tokens,
        full_transfer_scan=full_transfer,
        relevant_transfer_witnesses=relevant_witnesses,
        eligible_bundles=eligible,
        replay_input=replay_coverage,
        action_reconciliation=ReconciliationCoverage(
            status="exact_success",
            count=len(action_records),
            sha256=_coverage_digest(action_records),
        ),
        ownership_reconciliation=ReconciliationCoverage(
            status="exact_success",
            count=len(ownership_records),
            sha256=_coverage_digest(ownership_records),
        ),
    )


def _witness_set_coverage(
    witnesses: Sequence[DiscoveryWitness],
    transaction_hashes: Sequence[str],
) -> WitnessSetCoverage:
    witness_values = tuple(witnesses)
    hash_values = tuple(transaction_hashes)
    derived_hashes = tuple(sorted({value.transaction_hash for value in witness_values}))
    if hash_values != derived_hashes:
        raise ValueError("witness transaction hashes do not match their witness set")
    return WitnessSetCoverage(
        witness_count=len(witness_values),
        witnesses_sha256=_coverage_digest(
            [asdict(witness) for witness in witness_values]
        ),
        transaction_count=len(hash_values),
        transactions_sha256=_coverage_digest(list(hash_values)),
        transaction_hashes=hash_values,
    )


def _action_reconciliation_records(
    witnesses: Sequence[DiscoveryWitness],
    actions: Sequence[DecodedLiquidityAction],
) -> list[dict[str, object]]:
    witness_by_key = {
        (witness.transaction_hash, witness.log_index): witness
        for witness in witnesses
    }
    if len(witness_by_key) != len(witnesses):
        raise ValueError("action witness identities are duplicated")
    represented: set[tuple[str, int]] = set()
    records: list[dict[str, object]] = []
    for action in actions:
        key = (action.tx_hash.lower(), action.log_index)
        witness = witness_by_key.get(key)
        if witness is None or action.block_number != witness.block_number:
            raise ValueError("decoded action does not reconcile to an action witness")
        if key in represented:
            raise ValueError("an action witness maps to multiple decoded actions")
        represented.add(key)
        records.append(
            {
                "witness": asdict(witness),
                "decoded_action": {
                    "block_number": action.block_number,
                    "log_index": action.log_index,
                    "event_order": action.event_order,
                    "transaction_hash": action.tx_hash.lower(),
                },
            }
        )
    if represented != set(witness_by_key):
        raise ValueError("an action witness has no decoded action")
    return records


def _ownership_reconciliation_records(
    witnesses: Sequence[DiscoveryWitness],
    owners: Sequence[OwnershipEvent],
) -> list[dict[str, object]]:
    owner_by_key = {
        (owner.block_number, owner.log_index): owner for owner in owners
    }
    if len(owner_by_key) != len(owners):
        raise ValueError("ownership event identities are duplicated")
    records: list[dict[str, object]] = []
    for witness in witnesses:
        owner = owner_by_key.get((witness.block_number, witness.log_index))
        if owner is None or len(witness.topics) != 4:
            raise ValueError("retained transfer has no ownership event")
        token_id = int(witness.topics[3], 16)
        previous_owner = _topic_owner(witness.topics[1])
        new_owner = _topic_owner(witness.topics[2])
        if (
            owner.token_id != token_id
            or _lower_optional_owner(owner.previous_owner) != previous_owner
            or _lower_optional_owner(owner.new_owner) != new_owner
        ):
            raise ValueError("ownership event does not reconcile to its transfer witness")
        records.append(
            {
                "witness": asdict(witness),
                "ownership_event": {
                    "block_number": owner.block_number,
                    "log_index": owner.log_index,
                    "event_order": owner.event_order,
                    "token_id": str(owner.token_id),
                    "previous_owner": previous_owner,
                    "new_owner": new_owner,
                },
            }
        )
    if len(records) != len(owners):
        raise ValueError("ownership event set differs from retained transfers")
    return records


def _topic_owner(topic: str) -> str | None:
    owner = f"0x{topic[-40:]}"
    return None if owner == "0x" + "0" * 40 else owner


def _lower_optional_owner(value: str | None) -> str | None:
    return None if value is None else value.lower()


def _coverage_digest(value: object) -> str:
    return hashlib.sha256(_canonical_rpc_json(value).encode("utf-8")).hexdigest()


def _replay_required_text(
    row: Mapping[str, str],
    field: str,
    row_number: int,
) -> str:
    value = row.get(field)
    if value is None or not value or value != value.strip():
        raise ValueError(f"frozen replay row {row_number} has no {field}")
    return value


def _replay_canonical_int(
    row: Mapping[str, str],
    field: str,
    row_number: int,
    *,
    positive: bool | None,
) -> int:
    raw = _replay_required_text(row, field, row_number)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"frozen replay row {row_number} has invalid {field}") from exc
    if str(value) != raw or positive is True and value <= 0 or positive is False and value < 0:
        raise ValueError(f"frozen replay row {row_number} has noncanonical {field}")
    return value


def _replay_timestamp_ms(value: str, row_number: int) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"frozen replay row {row_number} block time is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"frozen replay row {row_number} block time is not UTC")
    if parsed.microsecond % 1_000 != 0:
        raise ValueError(
            f"frozen replay row {row_number} block time must use millisecond precision"
        )
    elapsed = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
    timestamp_ms = (
        elapsed.days * 86_400_000
        + elapsed.seconds * 1_000
        + elapsed.microseconds // 1_000
    )
    if timestamp_ms <= 0:
        raise ValueError(f"frozen replay row {row_number} block time is invalid")
    return timestamp_ms


def _require_matching_action_decode_identity(
    identity: ActionDecodeIdentity,
    config: ExportPoolConfig,
) -> None:
    expected = (
        config.name,
        config.chain,
        config.pool_id.lower(),
        config.pool_manager.lower(),
        config.position_manager.lower(),
    )
    observed = (
        identity.pool,
        identity.chain,
        identity.pool_id.lower(),
        identity.pool_manager.lower(),
        identity.position_manager.lower(),
    )
    if observed != expected:
        raise ValueError("checkpoint action-decode identity does not match pool config")
    if identity.wrapper_entrypoint.lower() != ENTRYPOINT_V08_ADDRESS.lower():
        raise ValueError("checkpoint action-decode EntryPoint is unsupported")


def _canonical_bundle_mapping(payload_json: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(payload, dict) or _canonical_rpc_json(payload) != payload_json:
        raise ValueError(f"{label} is not a canonical object")
    return payload


def _load_or_fetch_decode_header(
    run: LPLedgerCheckpoint,
    w3: Web3,
    block_number: int,
    *,
    expected_hash: str | None = None,
) -> BlockHeader:
    cached = run.load_header(block_number)
    if cached is not None:
        if expected_hash is not None and cached.block_hash != expected_hash:
            raise ValueError("cached action-decode header hash conflicts with bundle")
        return cached
    block_hash, timestamp_ms = _coverage_block_header(w3, block_number)
    if expected_hash is not None and block_hash != expected_hash:
        raise ValueError("action-decode header hash conflicts with bundle")
    return BlockHeader(block_number, block_hash, timestamp_ms)


def _strict_position_state_from_chain(
    token_id: int,
    position_manager: Any,
    config: ExportPoolConfig,
    block_number: int,
) -> LedgerPositionState | None:
    call_kwargs = {"block_identifier": block_number}
    pool_key, info = position_manager.functions.getPoolAndPositionInfo(token_id).call(
        **call_kwargs
    )
    if not _ledger_pool_key_matches(tuple(pool_key), config):
        return None
    pool_prefix, tick_lower, tick_upper = _decode_position_info(info)
    if not _pool_id_prefix_matches(pool_prefix, config):
        raise ValueError("resolved position pool prefix conflicts with its PoolKey")
    liquidity = int(
        position_manager.functions.getPositionLiquidity(token_id).call(**call_kwargs)
    )
    if liquidity < 0:
        raise ValueError("resolved position liquidity is negative")
    return LedgerPositionState(
        pool_id=config.pool_id.lower(),
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_after=liquidity,
    )


def _decoder_state_changes(
    prior_state: Mapping[int, LedgerPositionState],
    current_state: Mapping[int, LedgerPositionState],
    actions: Sequence[DecodedLiquidityAction],
) -> tuple[tuple[DecoderStateUpsert, ...], tuple[int, ...]]:
    actions_by_token: dict[int, list[DecodedLiquidityAction]] = {}
    for action in actions:
        actions_by_token.setdefault(action.token_id, []).append(action)
    upserts: list[DecoderStateUpsert] = []
    for token_id in sorted(current_state):
        state = current_state[token_id]
        if prior_state.get(token_id) == state:
            continue
        token_actions = actions_by_token.get(token_id)
        if not token_actions:
            raise ValueError("decoder state changed without a represented action")
        final_action = max(
            token_actions,
            key=lambda action: (
                action.block_number,
                action.log_index,
                action.event_order,
            ),
        )
        upserts.append(
            DecoderStateUpsert(
                token_id=token_id,
                state=state,
                last_block_number=final_action.block_number,
                last_log_index=final_action.log_index,
                last_event_order=final_action.event_order,
            )
        )
    deletes = tuple(sorted(set(prior_state).difference(current_state)))
    return tuple(upserts), deletes


def _new_position_key_mappings(
    prior_keys: Mapping[int, PoolPositionKey],
    current_keys: Mapping[int, PoolPositionKey],
    actions: Sequence[DecodedLiquidityAction],
    receipt: dict[str, Any],
    config: ExportPoolConfig,
) -> tuple[PositionKeyMapping, ...]:
    created_token_ids = {
        event.token_id
        for event in decode_ownership_events_from_receipt(
            receipt,
            config.position_manager,
        )
        if event.previous_owner is None
    }
    mappings: list[PositionKeyMapping] = []
    for token_id in sorted(set(current_keys).difference(prior_keys)):
        if token_id not in created_token_ids:
            raise ValueError(
                "target-pool position is missing from the frozen inception action "
                "history"
            )
        mint_actions = [
            action
            for action in actions
            if action.token_id == token_id and action.action_type == "mint"
        ]
        if not mint_actions:
            raise ValueError(
                "target-pool position has creation evidence but no in-range mint action"
            )
        creation = min(
            mint_actions,
            key=lambda action: (
                action.block_number,
                action.log_index,
                action.event_order,
            ),
        )
        position_key = current_keys[token_id]
        mappings.append(
            PositionKeyMapping(
                token_id=token_id,
                pool_id=position_key.pool_id,
                tick_lower=position_key.tick_lower,
                tick_upper=position_key.tick_upper,
                salt=position_key.salt,
                mint_block_number=creation.block_number,
                mint_log_index=creation.log_index,
                mint_event_order=creation.event_order,
            )
        )
    return tuple(mappings)


def _fetch_and_validate_candidate_bundle(
    w3: Web3,
    raw_requested_hash: object,
    witnesses: Sequence[DiscoveryWitness],
    start_block: int,
    end_block: int,
) -> CandidateBundle:
    _validate_frozen_range(start_block, end_block)
    witness_values = tuple(witnesses)
    if not witness_values:
        raise ValueError("candidate bundle requires at least one discovery witness")
    requested_hash = _normalize_transaction_hash(
        raw_requested_hash,
        label="candidate transaction hash",
    )
    transaction_raw = w3.eth.get_transaction(as_hexstr(requested_hash))
    receipt_raw = w3.eth.get_transaction_receipt(as_hexstr(requested_hash))
    if not isinstance(transaction_raw, Mapping) or not isinstance(receipt_raw, Mapping):
        raise ValueError("candidate RPC responses must be mappings")
    transaction = dict(transaction_raw)
    receipt = dict(receipt_raw)
    _require_matching_response_hash(
        requested_hash,
        transaction.get("hash"),
        label="RPC transaction response",
    )
    _require_matching_response_hash(
        requested_hash,
        receipt.get("transactionHash"),
        label="RPC receipt response",
    )
    block_number, transaction_index = _require_matching_transaction_location(
        requested_hash,
        transaction,
        receipt,
    )
    if (
        _required_rpc_int_field(
            receipt,
            "status",
            label="RPC receipt response",
        )
        != 1
    ):
        raise ValueError("candidate transaction receipt is not successful")
    if not start_block <= block_number <= end_block:
        raise ValueError("candidate transaction is outside the frozen range")
    transaction_block_hash = _normalize_transaction_hash(
        transaction.get("blockHash"),
        label="RPC transaction block hash",
    )
    receipt_block_hash = _normalize_transaction_hash(
        receipt.get("blockHash"),
        label="RPC receipt block hash",
    )
    if transaction_block_hash != receipt_block_hash:
        raise ValueError("RPC transaction and receipt block hashes differ")

    normalized_transaction = _normalize_rpc_payload(transaction, label="transaction")
    normalized_receipt = _normalize_rpc_payload(receipt, label="receipt")
    if not isinstance(normalized_transaction, dict) or not isinstance(
        normalized_receipt,
        dict,
    ):
        raise ValueError("normalized candidate responses must be mappings")
    _validate_normalized_receipt_witnesses(
        requested_hash,
        normalized_receipt,
        witness_values,
    )
    transaction_json = _canonical_rpc_json(normalized_transaction)
    receipt_json = _canonical_rpc_json(normalized_receipt)
    return CandidateBundle(
        transaction_hash=requested_hash,
        block_number=block_number,
        block_hash=transaction_block_hash,
        transaction_index=transaction_index,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=candidate_payload_sha256(transaction_json, receipt_json),
    )


def _normalize_rpc_payload(
    value: object,
    *,
    label: str,
    field_name: str | None = None,
) -> object:
    if value is None:
        return None
    if field_name in _RPC_QUANTITY_FIELDS:
        try:
            quantity = _int_from_rpc_value(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candidate {label} field {field_name} is invalid") from exc
        if quantity < 0:
            raise ValueError(f"candidate {label} field {field_name} is negative")
        return str(quantity)
    if field_name == "topics":
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray, memoryview))
        ):
            raise ValueError(f"candidate {label} topics are invalid")
        return [
            _normalize_fixed_hex(topic, 32, f"candidate {label} topic")
            for topic in value
        ]
    if field_name in _RPC_HASH_FIELDS:
        return _normalize_fixed_hex(value, 32, f"candidate {label} {field_name}")
    if field_name in _RPC_ADDRESS_FIELDS:
        return _normalize_fixed_hex(value, 20, f"candidate {label} {field_name}")
    if field_name in _RPC_DATA_FIELDS:
        return _normalize_hex_data(value, f"candidate {label} {field_name}")
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"candidate {label} mapping key is not a string")
            normalized[key] = _normalize_rpc_payload(
                item,
                label=label,
                field_name=key,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    ):
        return [
            _normalize_rpc_payload(item, label=label)
            for item in value
        ]
    if isinstance(value, bool):
        return value
    if type(value) is int:
        if value < 0:
            raise ValueError(f"candidate {label} contains a negative quantity")
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _normalize_hex_data(value, f"candidate {label} bytes")
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return _normalize_hex_data(value, f"candidate {label} hex value")
        return value
    raise ValueError(f"candidate {label} contains an unsupported value")


def _validate_normalized_receipt_witnesses(
    requested_hash: str,
    receipt: dict[str, object],
    witnesses: tuple[DiscoveryWitness, ...],
) -> None:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise ValueError("candidate receipt logs are invalid")
    for log in logs:
        if not isinstance(log, dict):
            raise ValueError("candidate receipt log is invalid")
        removed = log.get("removed")
        if removed is not None and (type(removed) is not bool or removed):
            raise ValueError("candidate receipt contains a removed log")
    sources = {witness.source for witness in witnesses}
    if len(sources) != 1 or any(
        witness.transaction_hash != requested_hash for witness in witnesses
    ):
        raise ValueError("candidate discovery witnesses are inconsistent")
    source = next(iter(sources))
    if source == "position_transfer":
        transfer_target = (witnesses[0].address, witnesses[0].topics[0])
        if any(
            len(witness.topics) != 4
            or witness.data != "0x"
            or (witness.address, witness.topics[0]) != transfer_target
            for witness in witnesses
        ):
            raise ValueError("candidate transfer witnesses are inconsistent")
        for witness in witnesses:
            matches = [
                log
                for log in logs
                if isinstance(log, dict)
                and _normalized_log_matches_witness(log, witness)
            ]
            if len(matches) != 1:
                raise ValueError(
                    "candidate receipt does not exactly match its transfer witness"
                )
        return
    if source != "pool_modify" or any(
        len(witness.topics) < 2 for witness in witnesses
    ):
        raise ValueError("candidate discovery witness topics are incomplete")
    action_target = (
        witnesses[0].address,
        witnesses[0].topics[0],
        witnesses[0].topics[1],
    )
    if any(
        (witness.address, witness.topics[0], witness.topics[1]) != action_target
        for witness in witnesses
    ):
        raise ValueError("candidate discovery witnesses are inconsistent")
    target_logs = [
        log
        for log in logs
        if isinstance(log, dict)
        and isinstance(log.get("topics"), list)
        and len(log["topics"]) >= 2
        and (
            log.get("address"),
            log["topics"][0],
            log["topics"][1],
        )
        == action_target
    ]
    for witness in witnesses:
        matches = [
            log
            for log in target_logs
            if _normalized_log_matches_witness(log, witness)
        ]
        if len(matches) != 1:
            raise ValueError("candidate receipt does not exactly match its action witness")
    if len(target_logs) != len(witnesses):
        raise ValueError("candidate receipt contains an unattested target action log")


def _normalized_log_matches_witness(
    log: dict[str, object],
    witness: DiscoveryWitness,
) -> bool:
    return (
        log.get("address") == witness.address
        and log.get("blockNumber") == str(witness.block_number)
        and log.get("blockHash") == witness.block_hash
        and log.get("transactionHash") == witness.transaction_hash
        and log.get("transactionIndex") == str(witness.transaction_index)
        and log.get("logIndex") == str(witness.log_index)
        and log.get("topics") == list(witness.topics)
        and log.get("data") == witness.data
    )


def _discovery_witness_sort_key(
    witness: DiscoveryWitness,
) -> tuple[int, int, int, str]:
    return (
        witness.block_number,
        witness.transaction_index,
        witness.log_index,
        witness.transaction_hash,
    )


def _validate_frozen_range(start_block: int, end_block: int) -> None:
    if (
        type(start_block) is not int
        or type(end_block) is not int
        or start_block < 0
        or end_block < start_block
    ):
        raise ValueError("frozen block range is invalid")


def _normalize_fixed_hex(
    value: object,
    byte_length: int,
    label: str,
) -> str:
    normalized = _normalize_hex_data(value, label)
    if len(normalized) != 2 + byte_length * 2:
        raise ValueError(f"{label} must be {byte_length} bytes")
    return normalized


def _normalize_hex_data(value: object, label: str) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)) and not bytes(value):
        return "0x"
    normalized = str(coerce_hex_str(value))
    if not normalized.startswith("0x"):
        raise ValueError(f"{label} must be hex data")
    body = normalized[2:]
    if len(body) % 2:
        raise ValueError(f"{label} must have an even number of hex digits")
    try:
        bytes.fromhex(body)
    except ValueError as exc:
        raise ValueError(f"{label} must be hex data") from exc
    return normalized.lower()


def _canonical_rpc_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


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
    paths = CheckpointPaths.from_output(output_path)
    with acquire_run_lock(paths):
        validate_output_namespace(paths, include_checkpoint=False)
        with _sigterm_as_interrupt():
            return _export_fixture_lp_ledger_locked(
                pool,
                start_block,
                end_block,
                decoded_actions_path,
                ownership_events_path,
                price_events_path,
                paths.output,
            )


def _export_fixture_lp_ledger_locked(
    pool: str,
    start_block: int,
    end_block: int,
    decoded_actions_path: Path,
    ownership_events_path: Path,
    price_events_path: Path,
    output_path: Path,
) -> int:
    phase: Phase = "action_decode"
    try:
        rows = build_lp_ledger_rows(
            decoded_actions=[
                _decoded_action_from_json(item)
                for item in _read_json_list(decoded_actions_path)
            ],
            ownership_events=[
                OwnershipEvent(**item)
                for item in _read_json_list(ownership_events_path)
            ],
            price_events=[
                ReplayedEvent(**item) for item in _read_json_list(price_events_path)
            ],
        )
        phase = "build"
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
        phase = "publish"
        _publish_ledger_pair(pool, output_path, ledger_bytes, coverage_bytes)
        write_progress_atomically(
            CheckpointPaths.from_output(output_path),
            terminal_noncheckpoint_progress(
                pool=pool,
                mode="fixture",
                start_block=start_block,
                end_block=end_block,
                output_filename=output_path.name,
                ledger_row_count=len(rows),
                candidate_transaction_count=0,
                now=datetime.now(timezone.utc),
            ),
        )
        return len(rows)
    except KeyboardInterrupt as exc:
        _record_noncheckpoint_failure(
            pool,
            "fixture",
            start_block,
            end_block,
            output_path,
            phase,
            "interrupted",
        )
        raise _CheckpointExportError(
            phase=phase,
            error_code="interrupted",
        ) from exc
    except _CheckpointExportError:
        raise
    except Exception as exc:
        _record_noncheckpoint_failure(
            pool,
            "fixture",
            start_block,
            end_block,
            output_path,
            phase,
            _checkpoint_failure_code(phase, exc),
        )
        raise


def _checkpoint_endpoint_snapshot(
    start_block: int,
    end_block: int,
    endpoint: _RpcCoverageEndpointSnapshot,
) -> EndpointSnapshot:
    return EndpointSnapshot(
        start=BlockHeader(
            block_number=start_block,
            block_hash=endpoint.start_block_hash,
            timestamp_ms=endpoint.start_timestamp_ms,
        ),
        end=BlockHeader(
            block_number=end_block,
            block_hash=endpoint.end_block_hash,
            timestamp_ms=endpoint.end_timestamp_ms,
        ),
    )


def _checkpoint_source_sha256() -> tuple[tuple[str, str], ...]:
    return tuple(
        (relative_path, _sha256_path(REPO_ROOT / relative_path))
        for relative_path in CHECKPOINT_SOURCE_PATHS
    )


def _checkpoint_run_identity(
    config: ExportPoolConfig,
    chain_id: int,
    start_block: int,
    end_block: int,
    output_path: Path,
    replay_input: ReplayInputEvidence,
    endpoint: _RpcCoverageEndpointSnapshot,
) -> RunIdentity:
    return RunIdentity(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        exporter_version=_CHECKPOINT_EXPORTER_VERSION,
        verification_mode="rpc_verified",
        pool=config.name,
        chain=config.chain,
        chain_id=chain_id,
        pool_id=config.pool_id.lower(),
        pool_manager=config.pool_manager.lower(),
        position_manager=config.position_manager.lower(),
        wrapper_entrypoint=ENTRYPOINT_V08_ADDRESS.lower(),
        token0_address=config.token0_address.lower(),
        token1_address=config.token1_address.lower(),
        token0_decimals=config.token0_decimals,
        token1_decimals=config.token1_decimals,
        fee_rate=format(Decimal(str(config.fee_rate)), "f"),
        invert_price=config.invert_price,
        start_block=start_block,
        end_block=end_block,
        chunk_size=config.chunk_size,
        action_topic=V4_MODIFY_LIQUIDITY_TOPIC.lower(),
        transfer_topic=_POSITION_MANAGER_TRANSFER_TOPIC,
        rpc_provider_origin=_rpc_provider_origin(config.rpc_url),
        replay_input=replay_input,
        output_path=str(output_path),
        endpoint=_checkpoint_endpoint_snapshot(start_block, end_block, endpoint),
        python_version=platform.python_version(),
        web3_version=importlib.metadata.version("web3"),
        source_sha256=_checkpoint_source_sha256(),
    )


def _require_matching_endpoint(
    expected: _RpcCoverageEndpointSnapshot,
    observed: _RpcCoverageEndpointSnapshot,
) -> None:
    if observed != expected:
        raise CrossPoolContractError(
            "RPC coverage endpoint snapshot changed during LP ledger export"
        )


def _require_publication_hashes(
    publication: PublicationState,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
) -> None:
    if (
        publication.expected_ledger_sha256 != hashlib.sha256(ledger_bytes).hexdigest()
        or publication.expected_sidecar_sha256
        != hashlib.sha256(coverage_bytes).hexdigest()
    ):
        raise CrossPoolContractError(
            "checkpoint publication hashes do not match the rebuilt ledger pair"
        )


def _publish_checkpoint_pair(
    run: LPLedgerCheckpoint,
    progress: _CheckpointProgressWriter,
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
) -> PublicationState:
    with _publication_lock(ledger_path):
        publication = run.load_publication_state()
        if publication.state == "not_started":
            _require_safe_publication_start_locked(
                pool,
                ledger_path,
                ledger_bytes,
                coverage_bytes,
            )
            progress.emit(
                run.begin_publication(
                    hashlib.sha256(ledger_bytes).hexdigest(),
                    hashlib.sha256(coverage_bytes).hexdigest(),
                )
            )
            publication = run.load_publication_state()
        _require_publication_hashes(publication, ledger_bytes, coverage_bytes)
        _publish_or_reconcile_ledger_pair_locked(
            pool,
            ledger_path,
            ledger_bytes,
            coverage_bytes,
            publication,
        )
        return publication


def _require_safe_publication_start_locked(
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
) -> None:
    sidecar_path = ledger_coverage_path(ledger_path)
    observed_ledger_sha256 = _optional_path_sha256(ledger_path)
    observed_sidecar_sha256 = _optional_path_sha256(sidecar_path)
    ledger_exact = observed_ledger_sha256 == hashlib.sha256(ledger_bytes).hexdigest()
    sidecar_exact = observed_sidecar_sha256 == hashlib.sha256(coverage_bytes).hexdigest()
    if ledger_exact != sidecar_exact:
        raise CrossPoolContractError(
            "refusing to authorize an ambiguous partial LP ledger pair"
        )
    ledger_exists = observed_ledger_sha256 is not None
    sidecar_exists = observed_sidecar_sha256 is not None
    if ledger_exists != sidecar_exists:
        raise CrossPoolContractError(
            "refusing to authorize an ambiguous partial LP ledger pair"
        )
    if not ledger_exists:
        return
    if ledger_exact:
        _fsync_directory(ledger_path.parent)
        load_ledger_coverage(pool, ledger_path)
        return
    try:
        load_ledger_coverage(pool, ledger_path)
    except Exception as exc:
        raise CrossPoolContractError(
            "refusing to authorize an ambiguous LP ledger pair"
        ) from exc


def _finish_checkpoint_wal(
    run: LPLedgerCheckpoint,
    progress: _CheckpointProgressWriter,
) -> None:
    try:
        run.checkpoint_terminal_wal()
    except Exception as exc:
        snapshot = run.snapshot()
        if snapshot.status == "succeeded":
            snapshot = run.mark_checkpoint_maintenance_failed()
        progress.emit(snapshot, force=True)
        raise _CheckpointExportError(
            phase="succeeded",
            error_code="checkpoint_maintenance_error",
        ) from exc


def _run_checkpointed_rpc_export(
    run: LPLedgerCheckpoint,
    w3: Web3,
    config: ExportPoolConfig,
    identity: RunIdentity,
    initial_endpoint: _RpcCoverageEndpointSnapshot,
    progress: _CheckpointProgressWriter,
) -> int:
    while True:
        snapshot = run.snapshot()
        phase = snapshot.phase
        if phase == "succeeded":
            if snapshot.status not in ("succeeded", "failed") or (
                snapshot.status == "failed"
                and snapshot.error_code != "checkpoint_maintenance_error"
            ):
                raise CrossPoolContractError(
                    "published LP ledger checkpoint has an invalid terminal state"
                )
            ledger_bytes, coverage_bytes, row_count = (
                _build_rpc_ledger_pair_from_checkpoint(run, identity, config)
            )
            _require_matching_endpoint(
                initial_endpoint,
                _capture_rpc_coverage_endpoint(
                    w3,
                    identity.start_block,
                    identity.end_block,
                ),
            )
            _publish_checkpoint_pair(
                run,
                progress,
                identity.pool,
                run.paths.output,
                ledger_bytes,
                coverage_bytes,
            )
            if snapshot.status == "failed":
                progress.emit(
                    run.mark_checkpoint_maintenance_recovered(),
                    force=True,
                )
            _finish_checkpoint_wal(run, progress)
            return row_count
        if snapshot.status != "running":
            raise CrossPoolContractError("LP ledger checkpoint is not runnable")
        if phase == "preflight":
            progress.emit(run.complete_phase("preflight"))
        elif phase == "action_discovery":
            _stage_action_discovery(
                run,
                w3,
                config,
                identity.start_block,
                identity.end_block,
                on_progress=progress.emit,
            )
        elif phase == "action_fetch":
            _stage_action_bundles(
                run,
                w3,
                identity.start_block,
                identity.end_block,
                on_progress=progress.emit,
            )
        elif phase == "action_decode":
            _stage_action_decode(run, w3, config, on_progress=progress.emit)
        elif phase == "token_set_freeze":
            progress.emit(_freeze_action_token_set(run))
        elif phase == "full_transfer_scan":
            _stage_full_transfer_scan(
                run,
                w3,
                config,
                identity.start_block,
                identity.end_block,
                on_progress=progress.emit,
            )
        elif phase == "relevant_transfer_fetch":
            _stage_relevant_transfer_bundles(
                run,
                w3,
                identity.start_block,
                identity.end_block,
                on_progress=progress.emit,
            )
        elif phase == "relevant_transfer_decode":
            _stage_relevant_transfer_decode(
                run,
                config,
                on_progress=progress.emit,
            )
        elif phase == "replay_input_bind":
            progress.emit(
                _bind_frozen_replay_input(run, config, identity.replay_input)
            )
        elif phase == "price_replay":
            progress.emit(_stage_price_replay(run, config))
        elif phase in ("build", "publish"):
            ledger_bytes, coverage_bytes, row_count = (
                _build_rpc_ledger_pair_from_checkpoint(run, identity, config)
            )
            _require_matching_endpoint(
                initial_endpoint,
                _capture_rpc_coverage_endpoint(
                    w3,
                    identity.start_block,
                    identity.end_block,
                ),
            )
            publication = _publish_checkpoint_pair(
                run,
                progress,
                identity.pool,
                run.paths.output,
                ledger_bytes,
                coverage_bytes,
            )
            if publication.state == "started":
                progress.emit(run.mark_published(), force=True)
            _finish_checkpoint_wal(run, progress)
            return row_count
        else:
            raise AssertionError(f"unsupported LP ledger checkpoint phase: {phase}")


def _export_checkpointed_rpc_lp_ledger(
    pool: str,
    start_block: int,
    end_block: int,
    output_path: Path,
    *,
    fresh: bool,
) -> int:
    config = POOL_CONFIGS[pool]
    _validate_frozen_range(start_block, end_block)
    if start_block != config.default_start_block:
        raise CrossPoolContractError(
            "verified RPC LP-ledger export must start at the configured pool inception"
        )
    replay_path = _FROZEN_REPLAY_PATHS[pool]
    frozen = _load_frozen_replay_input(replay_path, config)
    if (
        frozen.evidence.first_block != start_block
        or frozen.evidence.last_block != end_block
    ):
        raise CrossPoolContractError(
            "verified RPC LP-ledger range must equal the frozen replay range"
        )
    paths = CheckpointPaths.from_output(output_path)
    with acquire_run_lock(paths):
        validate_output_namespace(paths, include_checkpoint=True)
        with _sigterm_as_interrupt():
            w3 = _make_web3(config)
            chain_id = _coverage_chain_id(w3, pool)
            initial_endpoint = _capture_rpc_coverage_endpoint(w3, start_block, end_block)
            identity = _checkpoint_run_identity(
                config,
                chain_id,
                start_block,
                end_block,
                paths.output,
                frozen.evidence,
                initial_endpoint,
            )
            with LPLedgerCheckpoint.create_or_resume(
                paths,
                identity,
                fresh=fresh,
            ) as run:
                progress = _CheckpointProgressWriter(paths)
                try:
                    progress.emit(run.snapshot(), force=True)
                    return _run_checkpointed_rpc_export(
                        run,
                        w3,
                        config,
                        identity,
                        initial_endpoint,
                        progress,
                    )
                except KeyboardInterrupt as exc:
                    snapshot = run.snapshot()
                    if snapshot.status == "running":
                        snapshot = run.mark_attempt_failed("interrupted")
                        progress.emit(snapshot, force=True)
                    raise _CheckpointExportError(
                        phase=snapshot.phase,
                        error_code="interrupted",
                    ) from exc
                except _CheckpointExportError:
                    raise
                except Exception as exc:
                    snapshot = run.snapshot()
                    error_code = _checkpoint_failure_code(snapshot.phase, exc)
                    if snapshot.status == "running":
                        snapshot = run.mark_attempt_failed(error_code)
                        progress.emit(snapshot, force=True)
                    raise _CheckpointExportError(
                        phase=snapshot.phase,
                        error_code=error_code,
                    ) from exc


def export_rpc_lp_ledger(
    pool: str,
    start_block: int,
    end_block: int,
    output_path: Path,
    candidate_tx_hashes: Sequence[str] | None = None,
    *,
    fresh: bool = False,
) -> int:
    ledger_coverage_path(output_path)
    if candidate_tx_hashes is None:
        return _export_checkpointed_rpc_lp_ledger(
            pool,
            start_block,
            end_block,
            output_path,
            fresh=fresh,
        )
    if fresh:
        raise CrossPoolContractError(
            "--fresh is available only for verified full-RPC LP-ledger exports"
        )
    paths = CheckpointPaths.from_output(output_path)
    with acquire_run_lock(paths):
        validate_output_namespace(paths, include_checkpoint=False)
        with _sigterm_as_interrupt():
            return _export_candidate_list_lp_ledger_locked(
                pool,
                start_block,
                end_block,
                paths.output,
                candidate_tx_hashes,
            )


def export_candidate_csv_lp_ledger(
    pool: str,
    start_block: int,
    end_block: int,
    output_path: Path,
    candidate_tx_csv: Path,
) -> int:
    ledger_coverage_path(output_path)
    paths = CheckpointPaths.from_output(output_path)
    with acquire_run_lock(paths):
        validate_output_namespace(paths, include_checkpoint=False)
        with _sigterm_as_interrupt():
            try:
                candidate_tx_hashes = _candidate_tx_hashes_from_csv(
                    candidate_tx_csv,
                    start_block,
                    end_block,
                )
            except KeyboardInterrupt as exc:
                _record_noncheckpoint_failure(
                    pool,
                    "candidate_list_unverified",
                    start_block,
                    end_block,
                    paths.output,
                    "action_discovery",
                    "interrupted",
                )
                raise _CheckpointExportError(
                    phase="action_discovery",
                    error_code="interrupted",
                ) from exc
            except Exception as exc:
                _record_noncheckpoint_failure(
                    pool,
                    "candidate_list_unverified",
                    start_block,
                    end_block,
                    paths.output,
                    "action_discovery",
                    "decode_error",
                )
                raise _CheckpointExportError(
                    phase="action_discovery",
                    error_code="decode_error",
                ) from exc
            return _export_candidate_list_lp_ledger_locked(
                pool,
                start_block,
                end_block,
                paths.output,
                candidate_tx_hashes,
            )


def _export_candidate_list_lp_ledger_locked(
    pool: str,
    start_block: int,
    end_block: int,
    output_path: Path,
    candidate_tx_hashes: Sequence[str],
) -> int:
    config = POOL_CONFIGS[pool]
    phase: Phase = "preflight"
    try:
        w3 = _make_web3(config)
        chain_id = _coverage_chain_id(w3, pool)
        initial_endpoint = _capture_rpc_coverage_endpoint(
            w3,
            start_block,
            end_block,
        )
        phase = "action_fetch"
        resolved_candidate_hashes = tuple(candidate_tx_hashes)
        required_action_hashes: frozenset[str] = frozenset()
        decoded_actions, ownership_events = _decode_rpc_lp_inputs(
            w3,
            config,
            resolved_candidate_hashes,
            required_action_tx_hashes=required_action_hashes,
        )
        phase = "price_replay"
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
        phase = "build"
        ledger_bytes = _render_ledger_rows(rows)
        coverage_bytes = build_candidate_list_ledger_coverage_bytes(
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
        )
        phase = "publish"
        _publish_ledger_pair(pool, output_path, ledger_bytes, coverage_bytes)
        write_progress_atomically(
            CheckpointPaths.from_output(output_path),
            terminal_noncheckpoint_progress(
                pool=pool,
                mode="candidate_list_unverified",
                start_block=start_block,
                end_block=end_block,
                output_filename=output_path.name,
                ledger_row_count=len(rows),
                candidate_transaction_count=len(resolved_candidate_hashes),
                now=datetime.now(timezone.utc),
            ),
        )
        return len(rows)
    except KeyboardInterrupt as exc:
        _record_noncheckpoint_failure(
            pool,
            "candidate_list_unverified",
            start_block,
            end_block,
            output_path,
            phase,
            "interrupted",
        )
        raise _CheckpointExportError(
            phase=phase,
            error_code="interrupted",
        ) from exc
    except _CheckpointExportError:
        raise
    except Exception as exc:
        _record_noncheckpoint_failure(
            pool,
            "candidate_list_unverified",
            start_block,
            end_block,
            output_path,
            phase,
            _checkpoint_failure_code(phase, exc),
        )
        raise


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
    position_keys: dict[int, PoolPositionKey] = {}
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
        tx = dict(w3.eth.get_transaction(as_hexstr(requested_hash)))
        receipt = dict(w3.eth.get_transaction_receipt(as_hexstr(requested_hash)))
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
            position_keys,
            wrapper_entrypoint=ENTRYPOINT_V08_ADDRESS,
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
        transaction_hash = str(coerce_hex_str(value)).lower()
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
    return int(_block_timestamp_from_raw(_raw_get_block(w3, block_number, False)))


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
    return DecodedLiquidityAction(**cast(Any, normalized))


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


def _publish_or_reconcile_ledger_pair(
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
    publication: PublicationState,
) -> None:
    _require_publication_hashes(publication, ledger_bytes, coverage_bytes)
    with _publication_lock(ledger_path):
        _publish_or_reconcile_ledger_pair_locked(
            pool,
            ledger_path,
            ledger_bytes,
            coverage_bytes,
            publication,
        )


def _publish_or_reconcile_ledger_pair_locked(
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
    publication: PublicationState,
) -> None:
    sidecar_path = ledger_coverage_path(ledger_path)
    expected_ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
    expected_sidecar_sha256 = hashlib.sha256(coverage_bytes).hexdigest()
    observed_ledger_sha256 = _optional_path_sha256(ledger_path)
    observed_sidecar_sha256 = _optional_path_sha256(sidecar_path)
    ledger_exact = observed_ledger_sha256 == expected_ledger_sha256
    sidecar_exact = observed_sidecar_sha256 == expected_sidecar_sha256

    if publication.state == "published":
        if not ledger_exact or not sidecar_exact:
            raise CrossPoolContractError(
                "published LP ledger final pair is not the exact checkpoint pair"
            )
        _fsync_directory(ledger_path.parent)
        load_ledger_coverage(pool, ledger_path)
        return
    if publication.state != "started":
        raise CrossPoolContractError(
            "LP ledger publication must be durably started before final reconciliation"
        )
    if ledger_exact and sidecar_exact:
        _fsync_directory(ledger_path.parent)
        load_ledger_coverage(pool, ledger_path)
        return
    if ledger_exact != sidecar_exact:
        destination = sidecar_path if ledger_exact else ledger_path
        payload = coverage_bytes if ledger_exact else ledger_bytes
        staged = _temporary_output_path(destination, "reconcile")
        try:
            _write_durable_bytes(staged, payload)
            _replace_path(staged, destination)
            load_ledger_coverage(pool, ledger_path)
        finally:
            _cleanup_temporary_path(staged)
        return

    ledger_exists = observed_ledger_sha256 is not None
    sidecar_exists = observed_sidecar_sha256 is not None
    if ledger_exists != sidecar_exists:
        raise CrossPoolContractError(
            "refusing to reconcile an ambiguous partial LP ledger pair"
        )
    if ledger_exists:
        try:
            load_ledger_coverage(pool, ledger_path)
        except Exception as exc:
            raise CrossPoolContractError(
                "refusing to replace an ambiguous LP ledger pair"
            ) from exc
    _publish_ledger_pair_locked(pool, ledger_path, ledger_bytes, coverage_bytes)


def _optional_path_sha256(path: Path) -> str | None:
    if not validate_regular_leaf(path, allow_missing=True):
        return None
    return hashlib.sha256(_read_regular_bytes(path)).hexdigest()


@contextmanager
def _publication_lock(ledger_path: Path) -> Iterator[None]:
    paths = CheckpointPaths.from_output(ledger_path)
    validate_output_namespace(paths, include_checkpoint=False)
    descriptor = open_regular_leaf(
        paths.publish_lock,
        os.O_RDWR | os.O_CREAT,
        mode=0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CrossPoolContractError(
                f"LP ledger publication is already active: {ledger_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _publish_ledger_pair_locked(
    pool: str,
    ledger_path: Path,
    ledger_bytes: bytes,
    coverage_bytes: bytes,
) -> None:
    sidecar_path = ledger_coverage_path(ledger_path)
    ledger_exists = validate_regular_leaf(ledger_path, allow_missing=True)
    sidecar_exists = validate_regular_leaf(sidecar_path, allow_missing=True)
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
            _write_durable_bytes(backup_ledger, _read_regular_bytes(ledger_path))
            _write_durable_bytes(backup_sidecar, _read_regular_bytes(sidecar_path))
            load_ledger_coverage(pool, backup_ledger)

        # Publishing the ledger first makes every intermediate state fail closed
        # against the old or missing sidecar until the matching sidecar is visible.
        ledger_published = True
        _replace_path(staged_ledger, ledger_path)
        sidecar_published = True
        _replace_path(staged_sidecar, sidecar_path)
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
                    _unlink_path(ledger_path)
                if sidecar_published:
                    _unlink_path(sidecar_path)
        except BaseException as rollback_error:
            preserve_backups = True
            retained_backups = tuple(
                path
                for path in (backup_ledger, backup_sidecar)
                if path is not None
                and validate_regular_leaf(path, allow_missing=True)
            )
            retained_text = ", ".join(str(path) for path in retained_backups)
            raise RuntimeError(
                "LP ledger publication rollback failed; recover from retained "
                f"backup files: {retained_text}"
            ) from rollback_error
        raise
    finally:
        _cleanup_temporary_path(staged_ledger)
        _cleanup_temporary_path(staged_sidecar)
        if not preserve_backups:
            if backup_ledger is not None:
                _cleanup_temporary_path(backup_ledger)
            if backup_sidecar is not None:
                _cleanup_temporary_path(backup_sidecar)


def _temporary_ledger_path(ledger_path: Path, purpose: str) -> Path:
    validate_output_namespace(
        CheckpointPaths.from_output(ledger_path),
        include_checkpoint=False,
    )
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{ledger_path.stem}.{purpose}.",
        suffix=".csv",
        dir=ledger_path.parent,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    path = Path(raw_path)
    validate_regular_leaf(path, allow_missing=False)
    return path


def _temporary_output_path(destination: Path, purpose: str) -> Path:
    validate_regular_leaf(destination, allow_missing=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.{purpose}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    path = Path(raw_path)
    validate_regular_leaf(path, allow_missing=False)
    return path


def _write_durable_bytes(path: Path, payload: bytes) -> None:
    exists = validate_regular_leaf(path, allow_missing=True)
    flags = os.O_WRONLY | (os.O_TRUNC if exists else os.O_CREAT | os.O_EXCL)
    descriptor = open_regular_leaf(path, flags, mode=0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("LP ledger durable write returned no bytes")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_bytes(path: Path) -> bytes:
    descriptor = open_regular_leaf(path, os.O_RDONLY, mode=0o600)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _replace_path(source: Path, destination: Path) -> None:
    validate_regular_leaf(source, allow_missing=False)
    validate_regular_leaf(destination, allow_missing=True)
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _unlink_path(path: Path) -> None:
    if not validate_regular_leaf(path, allow_missing=True):
        return
    os.unlink(path)
    _fsync_directory(path.parent)


def _cleanup_temporary_path(path: Path) -> None:
    if validate_regular_leaf(path, allow_missing=True):
        os.unlink(path)


def _fsync_directory(path: Path) -> None:
    fsync_directory(path)


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
    parser.add_argument("--fresh", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        fixture_paths = (args.decoded_actions, args.ownership_events, args.price_events)
        if args.fresh and (
            args.candidate_tx_csv is not None
            or any(path is not None for path in fixture_paths)
        ):
            raise ValueError(
                "--fresh is available only for verified full-RPC LP-ledger exports"
            )
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
        if args.candidate_tx_csv is not None:
            count = export_candidate_csv_lp_ledger(
                args.pool,
                args.start_block,
                args.end_block,
                args.out,
                args.candidate_tx_csv,
            )
        else:
            count = export_rpc_lp_ledger(
                args.pool,
                args.start_block,
                args.end_block,
                args.out,
                candidate_tx_hashes=None,
                fresh=args.fresh,
            )
    except Exception as exc:
        phase: Phase = "preflight"
        error_code: ErrorCode = "unknown_error"
        if isinstance(exc, _CheckpointExportError):
            phase = exc.phase
            error_code = exc.error_code
        elif isinstance(exc, CheckpointContractError):
            error_code = "checkpoint_incompatible"
        print(
            render_safe_failure(
                pool=args.pool,
                phase=phase,
                error_code=error_code,
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
