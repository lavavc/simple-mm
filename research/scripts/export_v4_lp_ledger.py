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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.web3_utils import as_hexstr, coerce_hex_str  # noqa: E402
from research.backtester.lp_ledger_attribution import (  # noqa: E402
    build_fixture_ledger_coverage_bytes,
    build_rpc_ledger_coverage_bytes,
    ledger_coverage_path,
    load_ledger_coverage,
    pool_attribution_orientation,
)
from research.backtester.lp_ledger_checkpoint import (  # noqa: E402
    ActionDecodeIdentity,
    BlockHeader,
    CandidateBundle,
    CheckpointSnapshot,
    DecoderStateUpsert,
    DiscoveryWitness,
    LPLedgerCheckpoint,
    PositionKeyMapping,
    PositionResolution,
    candidate_payload_sha256,
    render_safe_failure,
)
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


def _stage_action_discovery(
    run: LPLedgerCheckpoint,
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
) -> CheckpointSnapshot:
    _validate_frozen_range(start_block, end_block)
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
        run.commit_action_chunk(block_range, witnesses)
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
    if (
        not pending_hashes
        and snapshot.phase == "action_fetch"
        and not run.action_candidate_hashes()
    ):
        snapshot = run.complete_phase("action_fetch")
    return snapshot


def _stage_action_decode(
    run: LPLedgerCheckpoint,
    w3: Web3,
    config: ExportPoolConfig,
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
    if (
        not pending_bundles
        and snapshot.phase == "action_decode"
        and not run.undecoded_action_bundles()
    ):
        snapshot = run.complete_phase("action_decode")
    return snapshot


def _freeze_action_token_set(run: LPLedgerCheckpoint) -> CheckpointSnapshot:
    snapshot = run.snapshot()
    if snapshot.phase != "token_set_freeze":
        raise ValueError("action token set cannot freeze before action decode completes")
    if run.undecoded_action_bundles():
        raise ValueError("action token set cannot freeze with undecoded bundles")
    return run.freeze_action_token_set()


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
    if any(len(witness.topics) < 2 for witness in witnesses):
        raise ValueError("candidate discovery witness topics are incomplete")
    expected_target = (
        witnesses[0].address,
        witnesses[0].topics[0],
        witnesses[0].topics[1],
    )
    if any(
        witness.transaction_hash != requested_hash
        or len(witness.topics) < 2
        or (witness.address, witness.topics[0], witness.topics[1]) != expected_target
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
        == expected_target
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
    if candidate_tx_hashes is None:
        raise CrossPoolContractError(
            "verified RPC LP-ledger export is disabled until the checkpointed "
            "action-first runner is wired"
        )
    config = POOL_CONFIGS[pool]
    w3 = _make_web3(config)
    chain_id = _coverage_chain_id(w3, pool)
    initial_endpoint = _capture_rpc_coverage_endpoint(
        w3,
        start_block,
        end_block,
    )
    resolved_candidate_hashes = tuple(candidate_tx_hashes)
    required_action_hashes: frozenset[str] = frozenset()
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
        verification_mode="candidate_list_unverified",
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
