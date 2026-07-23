import copy
import csv
import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode  # type: ignore[attr-defined]
from requests import ConnectionError as RequestsConnectionError
from web3 import Web3

import research.backtester.lp_ledger_attribution as ledger_attribution
import research.scripts.export_v4_lp_ledger as lp_ledger_export
from engine.lp.types import (
    _V4_LP_BURN_POSITION,
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_INCREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_SETTLE_PAIR,
    _V4_LP_TAKE_PAIR,
)
from research.backtester.lp_ledger_attribution import (
    AcquisitionPolicyCoverage,
    BundleDigestCoverage,
    EligibleBundleCoverage,
    FROZEN_REPLAY_PARSER_VERSION,
    FrozenTokenCoverage,
    FullTransferCoverage,
    ReconciliationCoverage,
    ReplayCoverage,
    RpcLedgerEvidence,
    TransferChunkCoverage,
    WitnessSetCoverage,
    build_rpc_ledger_coverage_bytes,
    frozen_replay_header_sha256,
    frozen_replay_parser_contract_sha256,
    frozen_replay_price_semantics_sha256,
    ledger_coverage_path,
    load_ledger_coverage,
)
from research.backtester.lp_ledger_checkpoint import (
    CHECKPOINT_SOURCE_PATHS,
    BlockHeader,
    BlockRange,
    CandidateBundle,
    CheckpointPaths,
    DecoderStateUpsert,
    DiscoveryWitness,
    EndpointSnapshot,
    LPLedgerCheckpoint,
    PositionKeyMapping,
    ReplayInputEvidence,
    RunIdentity,
    acquire_run_lock,
)
from research.backtester.v4_event_replay import ReplayedEvent
from research.backtester.v4_export import (
    POOL_CONFIGS,
    V4_MODIFY_LIQUIDITY_TOPIC,
    _make_web3,
)
from research.backtester.v4_lp_ledger import (
    ACTION_RECIPIENT_MSG_SENDER,
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
    PoolPositionKey,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
    position_manager_calls_for_transaction,
)
from research.cross_pool.contracts import CrossPoolContractError


def test_lp_ledger_cli_boundary_redacts_uncaught_rpc_credentials(
    monkeypatch,
    capsys,
) -> None:
    secret = "fixture-cli-secret"
    endpoint = f"https://base-mainnet.g.alchemy.com/v2/{secret}"

    def _failed_export(*_args, **_kwargs):
        raise RuntimeError(f"provider failed at {endpoint}")

    monkeypatch.setattr(lp_ledger_export, "export_rpc_lp_ledger", _failed_export)

    exit_code = lp_ledger_export.main(
        [
            "--pool",
            "uni-base",
            "--start-block",
            "1",
            "--end-block",
            "2",
            "--out",
            "unused.csv",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.err
    assert captured.err == "[lp-ledger][uni-base] phase=preflight error=unknown_error\n"


def test_pool_config_repr_omits_rpc_url() -> None:
    config = replace(
        POOL_CONFIGS["uni-base"],
        rpc_url="https://base-mainnet.g.alchemy.com/v2/fixture-secret",
    )

    rendered = repr(config)

    assert "rpc_url=" not in rendered
    assert "fixture-secret" not in rendered


def test_rpc_provider_origin_excludes_credentials_and_endpoint_path() -> None:
    secret = "fixture-provider-secret"
    origin = lp_ledger_export._rpc_provider_origin(
        "https://user:password@base-mainnet.g.alchemy.com/"
        f"v2/{secret}?api_key={secret}#fragment"
    )

    assert origin == "https://base-mainnet.g.alchemy.com"
    assert secret not in origin
    assert "password" not in origin


def test_lp_ledger_cli_boundary_never_renders_bare_exception_secrets(
    monkeypatch,
    capsys,
) -> None:
    secret = "bare-current-alchemy-key"

    def _failed_export(*_args, **_kwargs):
        raise RuntimeError(f"provider rejected {secret}")

    monkeypatch.setattr(lp_ledger_export, "export_rpc_lp_ledger", _failed_export)

    exit_code = lp_ledger_export.main(
        [
            "--pool",
            "uni-base",
            "--start-block",
            "1",
            "--end-block",
            "2",
            "--out",
            "unused.csv",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.err
    assert captured.err == "[lp-ledger][uni-base] phase=preflight error=unknown_error\n"


def test_research_rpc_provider_uses_bounded_read_retries() -> None:
    w3 = _make_web3(
        SimpleNamespace(
            rpc_url="http://127.0.0.1:1",
            chain="base",
        )
    )

    retry = w3.provider.exception_retry_configuration
    assert retry.retries == 8
    assert retry.backoff_factor == 0.5
    assert RequestsConnectionError in retry.errors
    assert set(retry.method_allowlist) == {
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_getBlockByNumber",
        "eth_call",
        "eth_chainId",
    }


def test_research_rpc_provider_retries_the_observed_transport_error(
    monkeypatch,
) -> None:
    w3 = _make_web3(
        SimpleNamespace(
            rpc_url="http://127.0.0.1:1",
            chain="base",
        )
    )
    attempts = 0
    sleeps = []

    def post(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RequestsConnectionError("remote closed the connection")
        return b"ok"

    monkeypatch.setattr(
        w3.provider._request_session_manager,
        "make_post_request",
        post,
    )
    monkeypatch.setattr("web3.providers.rpc.rpc.time.sleep", sleeps.append)

    assert w3.provider._make_request("eth_chainId", b"request") == b"ok"
    assert attempts == 2
    assert sleeps == [0.5]


def test_historical_position_lookup_propagates_transport_failures() -> None:
    config = POOL_CONFIGS["uni-base"]

    class FailingCall:
        def call(self, **_kwargs):
            raise RequestsConnectionError("remote closed the connection")

    position_manager = SimpleNamespace(
        functions=SimpleNamespace(
            getPoolAndPositionInfo=lambda _token_id: FailingCall(),
        )
    )

    with pytest.raises(RequestsConnectionError):
        lp_ledger_export._position_state_from_chain(
            1,
            position_manager,
            config,
            100,
        )

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class _ActionDiscoveryRun:
    def __init__(self, chunks: tuple[BlockRange, ...]) -> None:
        self.chunks = chunks
        self.commits: list[tuple[BlockRange, tuple[DiscoveryWitness, ...]]] = []

    def incomplete_action_chunks(self) -> tuple[BlockRange, ...]:
        return self.chunks

    def commit_action_chunk(
        self,
        block_range: BlockRange,
        witnesses: tuple[DiscoveryWitness, ...],
    ) -> SimpleNamespace:
        self.commits.append((block_range, witnesses))
        return SimpleNamespace(phase="action_discovery")

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(phase="action_fetch")


class _ActionBundleRun:
    def __init__(
        self,
        witnesses: dict[str, tuple[DiscoveryWitness, ...]],
        *,
        phase: str = "action_fetch",
        pending_hashes: tuple[str, ...] | None = None,
    ) -> None:
        self.witnesses = witnesses
        self.phase = phase
        self.pending_hashes = pending_hashes
        self.commits = []
        self.completed: list[str] = []

    def unfetched_action_hashes(self) -> tuple[str, ...]:
        return (
            tuple(self.witnesses)
            if self.pending_hashes is None
            else self.pending_hashes
        )

    def action_witnesses(self, transaction_hash: str) -> tuple[DiscoveryWitness, ...]:
        return self.witnesses[transaction_hash]

    def action_candidate_hashes(self) -> tuple[str, ...]:
        return tuple(self.witnesses)

    def commit_action_bundle(self, bundle):
        self.commits.append(bundle)
        return SimpleNamespace(phase="action_decode")

    def complete_phase(self, phase: str) -> SimpleNamespace:
        self.completed.append(phase)
        self.phase = "action_decode"
        return SimpleNamespace(phase=self.phase)

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(phase=self.phase)


class _ActionDecodeRun:
    def __init__(
        self,
        config,
        bundles: tuple[CandidateBundle, ...],
        *,
        phase: str = "action_decode",
    ) -> None:
        self.config = config
        self.bundles = bundles
        self.phase = phase
        self.commits: list[dict[str, object]] = []
        self.completed: list[str] = []
        self.freeze_calls = 0

    def action_decode_identity(self) -> SimpleNamespace:
        return SimpleNamespace(
            pool=self.config.name,
            chain=self.config.chain,
            pool_id=self.config.pool_id.lower(),
            pool_manager=self.config.pool_manager.lower(),
            position_manager=self.config.position_manager.lower(),
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    def undecoded_action_bundles(self) -> tuple[CandidateBundle, ...]:
        return self.bundles

    def load_decoder_state(self) -> dict[int, LedgerPositionState]:
        return {}

    def load_position_keys(self) -> tuple[PositionKeyMapping, ...]:
        return ()

    def load_header(self, _block_number: int) -> None:
        return None

    def load_position_resolution(self, _token_id: int, _query_block: int) -> None:
        return None

    def commit_decoded_action_transaction(
        self,
        bundle: CandidateBundle,
        headers,
        resolutions,
        state_upserts,
        state_deletes,
        actions,
        position_keys,
    ) -> SimpleNamespace:
        self.commits.append(
            {
                "bundle": bundle,
                "headers": tuple(headers),
                "resolutions": tuple(resolutions),
                "state_upserts": tuple(state_upserts),
                "state_deletes": tuple(state_deletes),
                "actions": tuple(actions),
                "position_keys": tuple(position_keys),
            }
        )
        self.phase = "token_set_freeze"
        return SimpleNamespace(phase=self.phase)

    def complete_phase(self, phase: str) -> SimpleNamespace:
        self.completed.append(phase)
        self.phase = "token_set_freeze"
        return SimpleNamespace(phase=self.phase)

    def freeze_action_token_set(self) -> SimpleNamespace:
        self.freeze_calls += 1
        self.phase = "full_transfer_scan"
        return SimpleNamespace(phase=self.phase)

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(phase=self.phase)


class _TransferScanRun:
    def __init__(
        self,
        chunks: tuple[BlockRange, ...],
        frozen_token_ids: tuple[int, ...],
    ) -> None:
        self.chunks = chunks
        self.tokens = frozen_token_ids
        self.commits: list[
            tuple[BlockRange, int, str, tuple[DiscoveryWitness, ...]]
        ] = []

    def incomplete_transfer_chunks(self) -> tuple[BlockRange, ...]:
        return self.chunks

    def frozen_token_ids(self) -> tuple[int, ...]:
        return self.tokens

    def commit_transfer_chunk(
        self,
        block_range: BlockRange,
        *,
        unfiltered_count: int,
        unfiltered_sha256: str,
        relevant_witnesses: tuple[DiscoveryWitness, ...],
    ) -> SimpleNamespace:
        self.commits.append(
            (
                block_range,
                unfiltered_count,
                unfiltered_sha256,
                relevant_witnesses,
            )
        )
        return SimpleNamespace(phase="relevant_transfer_fetch")

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(
            phase="full_transfer_scan" if not self.commits else "relevant_transfer_fetch"
        )


class _RelevantTransferBundleRun:
    def __init__(
        self,
        witnesses: dict[str, tuple[DiscoveryWitness, ...]],
        cached_hashes: frozenset[str] = frozenset(),
        *,
        phase: str = "relevant_transfer_fetch",
    ) -> None:
        self.witnesses = witnesses
        self.cached_hashes = cached_hashes
        self.phase = phase
        self.reuse_calls: list[str] = []
        self.commits: list[CandidateBundle] = []
        self.completed: list[str] = []

    def unfetched_relevant_transfer_hashes(self) -> tuple[str, ...]:
        return tuple(self.witnesses)

    def relevant_transfer_transaction_hashes(self) -> tuple[str, ...]:
        return tuple(self.witnesses)

    def relevant_transfer_witnesses(
        self,
        transaction_hash: str,
    ) -> tuple[DiscoveryWitness, ...]:
        return self.witnesses[transaction_hash]

    def reuse_staged_bundle_for_relevant_transfer(
        self,
        transaction_hash: str,
    ) -> SimpleNamespace | None:
        self.reuse_calls.append(transaction_hash)
        if transaction_hash not in self.cached_hashes:
            return None
        return SimpleNamespace(phase=self.phase)

    def commit_relevant_transfer_bundle(
        self,
        bundle: CandidateBundle,
    ) -> SimpleNamespace:
        self.commits.append(bundle)
        self.phase = "relevant_transfer_decode"
        return SimpleNamespace(phase=self.phase)

    def complete_phase(self, phase: str) -> SimpleNamespace:
        self.completed.append(phase)
        self.phase = "relevant_transfer_decode"
        return SimpleNamespace(phase=self.phase)

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(phase=self.phase)


class _RelevantTransferDecodeRun:
    def __init__(
        self,
        bundles: tuple[CandidateBundle, ...],
        frozen_token_ids: tuple[int, ...],
        *,
        phase: str = "relevant_transfer_decode",
    ) -> None:
        self.bundles = bundles
        self.tokens = frozen_token_ids
        self.phase = phase
        self.commits: list[tuple[CandidateBundle, tuple[OwnershipEvent, ...]]] = []
        self.completed: list[str] = []

    def undecoded_relevant_transfer_bundles(self) -> tuple[CandidateBundle, ...]:
        return self.bundles

    def frozen_token_ids(self) -> tuple[int, ...]:
        return self.tokens

    def relevant_transfer_transaction_hashes(self) -> tuple[str, ...]:
        return tuple(bundle.transaction_hash for bundle in self.bundles)

    def commit_decoded_relevant_transfer_transaction(
        self,
        bundle: CandidateBundle,
        *,
        owners: tuple[OwnershipEvent, ...],
    ) -> SimpleNamespace:
        self.commits.append((bundle, owners))
        self.phase = "replay_input_bind"
        return SimpleNamespace(phase=self.phase)

    def complete_phase(self, phase: str) -> SimpleNamespace:
        self.completed.append(phase)
        self.phase = "replay_input_bind"
        return SimpleNamespace(phase=self.phase)

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(phase=self.phase)


class _ReplayRun:
    def __init__(
        self,
        expected: ReplayInputEvidence,
        actions: tuple[DecodedLiquidityAction, ...],
    ) -> None:
        self.expected = expected
        self.actions = actions
        self.phase = "replay_input_bind"
        self.bound_inputs: list[ReplayInputEvidence] = []
        self.binding_commits: list[tuple[object, ...]] = []

    def commit_replay_input(self, evidence: ReplayInputEvidence) -> SimpleNamespace:
        self.bound_inputs.append(evidence)
        self.phase = "price_replay"
        return SimpleNamespace(phase=self.phase)

    def load_replay_input(self) -> ReplayInputEvidence:
        return self.expected

    def load_decoded_actions(self) -> tuple[DecodedLiquidityAction, ...]:
        return self.actions

    def commit_action_price_bindings(self, bindings) -> SimpleNamespace:
        self.binding_commits.append(tuple(bindings))
        self.phase = "build"
        return SimpleNamespace(phase=self.phase)

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(phase=self.phase)


def _action_decode_checkpoint_identity(output: Path, config) -> RunIdentity:
    return RunIdentity(
        schema_version=2,
        exporter_version="lp-ledger-checkpoint-v2",
        verification_mode="rpc_verified",
        pool=config.name,
        chain=config.chain,
        chain_id=8453,
        pool_id=config.pool_id.lower(),
        pool_manager=config.pool_manager.lower(),
        position_manager=config.position_manager.lower(),
        wrapper_entrypoint=_ENTRYPOINT_V08,
        token0_address=config.token0_address.lower(),
        token1_address=config.token1_address.lower(),
        token0_decimals=config.token0_decimals,
        token1_decimals=config.token1_decimals,
        fee_rate=str(config.fee_rate),
        invert_price=config.invert_price,
        start_block=100,
        end_block=109,
        chunk_size=5,
        action_topic=V4_MODIFY_LIQUIDITY_TOPIC.lower(),
        transfer_topic=("0x" + TRANSFER_TOPIC.removeprefix("0x")).lower(),
        rpc_provider_origin="https://base-mainnet.g.alchemy.com",
        replay_input=ReplayInputEvidence(
            path=str(output.with_name("replay.csv").resolve()),
            sha256="b" * 64,
            byte_length=1_000,
            row_count=10,
            header_sha256="c" * 64,
            parser_version="pool-history-replay-v1",
            parser_contract_sha256=frozen_replay_parser_contract_sha256(),
            price_semantics_sha256="d" * 64,
            chain=config.chain,
            pool_id=config.pool_id.lower(),
            first_block=100,
            last_block=109,
            first_timestamp_ms=100_000,
            last_timestamp_ms=109_000,
            price_event_count=4,
            price_events_sha256="e" * 64,
        ),
        output_path=str(output.resolve()),
        endpoint=EndpointSnapshot(
            start=BlockHeader(100, "0x" + "10" * 32, 100_000),
            end=BlockHeader(109, "0x" + "19" * 32, 109_000),
        ),
        python_version="3.12.0",
        web3_version="7.0.0",
        source_sha256=tuple(
            (path, "a" * 64) for path in CHECKPOINT_SOURCE_PATHS
        ),
    )


def _raw_action_log(
    *,
    block_number: int = 101,
    transaction_hash: str = "0x" + "33" * 32,
    transaction_index: int = 5,
    log_index: int = 11,
) -> dict[str, object]:
    config = POOL_CONFIGS["uni-base"]
    return {
        "address": Web3.to_checksum_address(config.pool_manager),
        "blockNumber": hex(block_number),
        "blockHash": bytes.fromhex("44" * 32),
        "transactionHash": transaction_hash.upper().replace("0X", "0x"),
        "transactionIndex": hex(transaction_index),
        "logIndex": hex(log_index),
        "topics": (
            bytes.fromhex(V4_MODIFY_LIQUIDITY_TOPIC[2:]),
            config.pool_id.upper().replace("0X", "0x"),
            bytes.fromhex("55" * 32),
        ),
        "data": b"\x01\x02",
    }


def _raw_position_transfer_log(
    *,
    block_number: int = 101,
    transaction_hash: str = "0x" + "33" * 32,
    transaction_index: int = 5,
    log_index: int = 12,
    token_id: int = 77,
    from_address: str = ZERO_ADDRESS,
    to_address: str = "0x" + "aa" * 20,
) -> dict[str, object]:
    config = POOL_CONFIGS["uni-base"]
    block_hash_byte = block_number % 256
    return {
        "address": Web3.to_checksum_address(config.position_manager),
        "blockNumber": hex(block_number),
        "blockHash": bytes([block_hash_byte]) * 32,
        "transactionHash": transaction_hash.upper().replace("0X", "0x"),
        "transactionIndex": hex(transaction_index),
        "logIndex": hex(log_index),
        "topics": (
            bytes.fromhex(TRANSFER_TOPIC.removeprefix("0x")),
            bytes.fromhex(_topic_address(from_address)[2:]),
            bytes.fromhex(_topic_address(to_address)[2:]),
            token_id.to_bytes(32, "big"),
        ),
        "data": b"",
        "removed": False,
    }


def _expected_position_transfer_witness(
    raw: dict[str, object],
) -> DiscoveryWitness:
    config = POOL_CONFIGS["uni-base"]
    topics = tuple(
        "0x" + bytes(topic).hex() for topic in raw["topics"]  # type: ignore[arg-type]
    )
    return DiscoveryWitness(
        source="position_transfer",
        block_number=int(str(raw["blockNumber"]), 16),
        block_hash="0x" + bytes(raw["blockHash"]).hex(),
        transaction_hash=str(raw["transactionHash"]).lower(),
        transaction_index=int(str(raw["transactionIndex"]), 16),
        log_index=int(str(raw["logIndex"]), 16),
        address=config.position_manager.lower(),
        topics=topics,
        data="0x",
    )


def _expected_action_witness(raw: dict[str, object]) -> DiscoveryWitness:
    config = POOL_CONFIGS["uni-base"]
    return DiscoveryWitness(
        source="pool_modify",
        block_number=int(str(raw["blockNumber"]), 16),
        block_hash="0x" + "44" * 32,
        transaction_hash=str(raw["transactionHash"]).lower(),
        transaction_index=int(str(raw["transactionIndex"]), 16),
        log_index=int(str(raw["logIndex"]), 16),
        address=config.pool_manager.lower(),
        topics=(
            V4_MODIFY_LIQUIDITY_TOPIC,
            config.pool_id,
            "0x" + "55" * 32,
        ),
        data="0x0102",
    )


def _minimal_coverage_pair_bytes(block_number: int) -> tuple[bytes, bytes]:
    config = POOL_CONFIGS["uni-base"]
    ledger_bytes = (
        "chain,pool_id,block_number\n"
        f"base,{config.pool_id},{block_number}\n"
    ).encode()
    coverage_bytes = build_rpc_ledger_coverage_bytes(
        "uni-base",
        ledger_bytes,
        chain_id=8453,
        covered_start_block=100,
        covered_start_block_hash="0x" + "11" * 32,
        covered_start_timestamp_ms=1_700_000_000_000,
        covered_end_block=200,
        covered_end_block_hash="0x" + "22" * 32,
        covered_end_timestamp_ms=1_700_001_000_000,
        evidence=_minimal_rpc_evidence(
            start_block=100,
            end_block=200,
            start_timestamp_ms=1_700_000_000_000,
            end_timestamp_ms=1_700_001_000_000,
        ),
    )
    return ledger_bytes, coverage_bytes


def _minimal_rpc_evidence(
    *,
    start_block: int,
    end_block: int,
    start_timestamp_ms: int,
    end_timestamp_ms: int,
) -> RpcLedgerEvidence:
    transaction_hash = "0x" + "33" * 32
    hashes = (transaction_hash,)
    hashes_sha256 = hashlib.sha256(
        json.dumps(
            list(hashes),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    witness_set = WitnessSetCoverage(
        witness_count=1,
        witnesses_sha256="1" * 64,
        transaction_count=1,
        transactions_sha256=hashes_sha256,
        transaction_hashes=hashes,
    )
    token_ids = ("1",)
    token_sha256 = hashlib.sha256(b'["1"]').hexdigest()
    chunk = TransferChunkCoverage(
        index=0,
        start_block=start_block,
        end_block=end_block,
        unfiltered_count=1,
        unfiltered_sha256="2" * 64,
    )
    chunks_sha256 = hashlib.sha256(
        json.dumps(
            [asdict(chunk)],
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    bundle = BundleDigestCoverage(
        transaction_hash=transaction_hash,
        payload_sha256="3" * 64,
    )
    bundles_sha256 = hashlib.sha256(
        json.dumps(
            [asdict(bundle)],
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return RpcLedgerEvidence(
        rpc_provider_origin="https://base-mainnet.g.alchemy.com",
        acquisition_policy=AcquisitionPolicyCoverage(
            max_blocks_per_log_query=2_000,
            retry_attempts=3,
            subdivision="sequential_binary",
            hidden_provider_retries="disabled",
            completeness="provider_conditioned",
        ),
        action_witnesses=witness_set,
        frozen_token_ids=FrozenTokenCoverage(
            token_count=1,
            token_ids_sha256=token_sha256,
            token_ids=token_ids,
        ),
        full_transfer_scan=FullTransferCoverage(
            position_manager=POOL_CONFIGS["uni-base"].position_manager.lower(),
            transfer_topic="0x" + TRANSFER_TOPIC.removeprefix("0x"),
            start_block=start_block,
            end_block=end_block,
            log_count=1,
            chunks_sha256=chunks_sha256,
            chunks=(chunk,),
        ),
        relevant_transfer_witnesses=witness_set,
        eligible_bundles=EligibleBundleCoverage(
            transaction_count=1,
            transactions_sha256=hashes_sha256,
            bundles_sha256=bundles_sha256,
            transaction_hashes=hashes,
            bundles=(bundle,),
        ),
        replay_input=ReplayCoverage(
            sha256="5" * 64,
            byte_length=1_000,
            row_count=10,
            header_sha256=frozen_replay_header_sha256(),
            parser_version=FROZEN_REPLAY_PARSER_VERSION,
            parser_contract_sha256=frozen_replay_parser_contract_sha256(),
            price_semantics_sha256=frozen_replay_price_semantics_sha256(),
            chain="base",
            pool_id=POOL_CONFIGS["uni-base"].pool_id.lower(),
            first_block=start_block,
            last_block=end_block,
            first_timestamp_ms=start_timestamp_ms,
            last_timestamp_ms=end_timestamp_ms,
            price_event_count=4,
            price_events_sha256="8" * 64,
        ),
        action_reconciliation=ReconciliationCoverage(
            status="exact_success",
            count=1,
            sha256="9" * 64,
        ),
        ownership_reconciliation=ReconciliationCoverage(
            status="exact_success",
            count=1,
            sha256="a" * 64,
        ),
    )


def test_verified_rpc_coverage_v2_binds_named_checkpoint_evidence(
    tmp_path: Path,
) -> None:
    ledger_bytes, coverage_bytes = _minimal_coverage_pair_bytes(100)
    ledger_path = tmp_path / "ledger.csv"
    ledger_path.write_bytes(ledger_bytes)
    sidecar = ledger_coverage_path(ledger_path)
    sidecar.write_bytes(coverage_bytes)

    loaded = load_ledger_coverage("uni-base", ledger_path)
    payload = json.loads(coverage_bytes)

    assert loaded.schema_version == "2.0.0"
    assert loaded.verification_mode == "rpc_verified"
    assert payload["evidence"]["rpc_provider_origin"] == (
        "https://base-mainnet.g.alchemy.com"
    )
    assert set(payload["evidence"]) == {
        "rpc_provider_origin",
        "acquisition_policy",
        "action_witnesses",
        "frozen_token_ids",
        "full_transfer_scan",
        "relevant_transfer_witnesses",
        "eligible_bundles",
        "replay_input",
        "action_reconciliation",
        "ownership_reconciliation",
    }
    assert "candidate_scan_source" not in payload

    payload["evidence"]["replay_input"]["row_count"] += 1
    sidecar.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    with pytest.raises(CrossPoolContractError, match="attestation"):
        load_ledger_coverage("uni-base", ledger_path)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("evidence", "action_witnesses", "witnesses_sha256"), "0" * 64),
        (("evidence", "frozen_token_ids", "token_count"), 2),
        (
            ("evidence", "full_transfer_scan", "position_manager"),
            "0x" + "ff" * 20,
        ),
        (
            ("evidence", "relevant_transfer_witnesses", "witnesses_sha256"),
            "0" * 64,
        ),
        (("evidence", "eligible_bundles", "bundles_sha256"), "0" * 64),
        (("evidence", "replay_input", "sha256"), "0" * 64),
        (("evidence", "action_reconciliation", "sha256"), "0" * 64),
        (("evidence", "ownership_reconciliation", "sha256"), "0" * 64),
        (("evidence", "acquisition_policy", "max_blocks_per_log_query"), 1_999),
        (("evidence", "rpc_provider_origin"), "https://fixture.example/secret"),
        (("covered_end_block_hash",), "0x" + "ff" * 32),
        (("ledger_sha256",), "0" * 64),
    ),
)
def test_verified_rpc_coverage_v2_rejects_each_attested_group_mutation(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    ledger_bytes, coverage_bytes = _minimal_coverage_pair_bytes(100)
    ledger_path = tmp_path / "ledger.csv"
    ledger_path.write_bytes(ledger_bytes)
    sidecar = ledger_coverage_path(ledger_path)
    payload = json.loads(coverage_bytes)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    sidecar.write_text(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )

    with pytest.raises(CrossPoolContractError):
        load_ledger_coverage("uni-base", ledger_path)


def test_archived_replay_profile_loading_is_independent_of_live_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = _minimal_rpc_evidence(
        start_block=100,
        end_block=200,
        start_timestamp_ms=1_700_000_000_000,
        end_timestamp_ms=1_700_001_000_000,
    ).replay_input
    monkeypatch.setattr(
        "research.backtester.lp_ledger_attribution."
        "_active_frozen_replay_price_semantics_sha256",
        lambda: "0" * 64,
    )
    ledger_attribution._active_frozen_replay_profile.cache_clear()

    assert ReplayCoverage(**asdict(replay)) == replay
    with pytest.raises(CrossPoolContractError, match="active frozen replay profile"):
        frozen_replay_price_semantics_sha256()


def test_replay_coverage_rejects_parser_contract_drift() -> None:
    replay = _minimal_rpc_evidence(
        start_block=100,
        end_block=200,
        start_timestamp_ms=1_700_000_000_000,
        end_timestamp_ms=1_700_001_000_000,
    ).replay_input

    with pytest.raises(CrossPoolContractError, match="parser contract"):
        replace(replay, parser_contract_sha256="0" * 64)


def test_verified_coverage_distinguishes_parent_chunks_from_rpc_query_limit() -> None:
    config = POOL_CONFIGS["uni-base"]
    ledger_bytes = (
        "chain,pool_id,block_number\n"
        f"base,{config.pool_id},100\n"
    ).encode()
    evidence = _minimal_rpc_evidence(
        start_block=100,
        end_block=5_099,
        start_timestamp_ms=1_700_000_000_000,
        end_timestamp_ms=1_700_001_000_000,
    )

    coverage_bytes = build_rpc_ledger_coverage_bytes(
        "uni-base",
        ledger_bytes,
        chain_id=8453,
        covered_start_block=100,
        covered_start_block_hash="0x" + "11" * 32,
        covered_start_timestamp_ms=1_700_000_000_000,
        covered_end_block=5_099,
        covered_end_block_hash="0x" + "22" * 32,
        covered_end_timestamp_ms=1_700_001_000_000,
        evidence=evidence,
    )

    assert b'"max_blocks_per_log_query":2000' in coverage_bytes
    assert b'"end_block":5099' in coverage_bytes


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("empty_tokens", "frozen token set"),
        ("relevant_exceeds_full", "relevant transfer witnesses"),
    ),
)
def test_verified_coverage_rejects_impossible_candidate_relations(
    mutation: str,
    match: str,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    ledger_bytes = (
        "chain,pool_id,block_number\n"
        f"base,{config.pool_id},100\n"
    ).encode()
    evidence = _minimal_rpc_evidence(
        start_block=100,
        end_block=200,
        start_timestamp_ms=1_700_000_000_000,
        end_timestamp_ms=1_700_001_000_000,
    )
    if mutation == "empty_tokens":
        evidence = replace(
            evidence,
            frozen_token_ids=FrozenTokenCoverage(
                token_count=0,
                token_ids_sha256=hashlib.sha256(b"[]").hexdigest(),
                token_ids=(),
            ),
        )
    else:
        chunk = replace(
            evidence.full_transfer_scan.chunks[0],
            unfiltered_count=0,
        )
        chunks_sha256 = hashlib.sha256(
            json.dumps(
                [asdict(chunk)],
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        evidence = replace(
            evidence,
            full_transfer_scan=replace(
                evidence.full_transfer_scan,
                log_count=0,
                chunks_sha256=chunks_sha256,
                chunks=(chunk,),
            ),
        )

    with pytest.raises(CrossPoolContractError, match=match):
        build_rpc_ledger_coverage_bytes(
            "uni-base",
            ledger_bytes,
            chain_id=8453,
            covered_start_block=100,
            covered_start_block_hash="0x" + "11" * 32,
            covered_start_timestamp_ms=1_700_000_000_000,
            covered_end_block=200,
            covered_end_block_hash="0x" + "22" * 32,
            covered_end_timestamp_ms=1_700_001_000_000,
            evidence=evidence,
        )


def test_active_replay_profile_rejects_parser_implementation_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ledger_attribution,
        "_active_frozen_replay_parser_contract_sha256",
        lambda: "0" * 64,
    )
    ledger_attribution._active_frozen_replay_profile.cache_clear()

    with pytest.raises(CrossPoolContractError, match="active frozen replay profile"):
        ledger_attribution.frozen_replay_parser_contract_sha256()


def test_frozen_replay_parser_contract_seals_event_time_replay() -> None:
    assert set(
        ledger_attribution._FROZEN_REPLAY_PARSER_SOURCE_SYMBOLS[
            "research/backtester/v4_event_replay.py"
        ]
    ) == {
        "PoolStateSnapshot",
        "ReplayEvent",
        "ReplayedEvent",
        "_PRICE_EVENTS",
        "_CARRIED_STATE_EVENTS",
        "attach_event_time_state",
    }
    assert "_stage_price_replay" in (
        ledger_attribution._FROZEN_REPLAY_PARSER_SOURCE_SYMBOLS[
            "research/scripts/export_v4_lp_ledger.py"
        ]
    )


def test_action_reconciliation_rejects_multiple_actions_per_witness() -> None:
    config = POOL_CONFIGS["uni-base"]
    transaction_hash = "0x" + "33" * 32
    witness = DiscoveryWitness(
        source="pool_modify",
        block_number=100,
        block_hash="0x" + "11" * 32,
        transaction_hash=transaction_hash,
        transaction_index=0,
        log_index=5,
        address=config.pool_manager.lower(),
        topics=(V4_MODIFY_LIQUIDITY_TOPIC, config.pool_id, "0x" + "22" * 32),
        data="0x",
    )
    action = DecodedLiquidityAction(
        action_type="mint",
        block_number=100,
        log_index=5,
        event_order=0,
        token_id=1,
        lp_owner=None,
        tick_lower=-10,
        tick_upper=10,
        liquidity_delta=1,
        amount0=0,
        amount1=0,
        collect_amount0=0,
        tx_hash=transaction_hash,
    )

    with pytest.raises(ValueError, match="multiple decoded actions"):
        lp_ledger_export._action_reconciliation_records(
            (witness,),
            (action, replace(action, event_order=1)),
        )


_REPLAY_FIELDS = (
    "block_time",
    "chain",
    "pool_id",
    "event_type",
    "tx_hash",
    "log_index",
    "block_number",
    "sqrt_price_x96",
    "tick",
    "active_liquidity",
    "fee_rate",
    "amount0",
    "amount1",
    "amount_usd",
    "cngn_usd_price",
    "token0_symbol",
    "token1_symbol",
    "event_source",
    "sender",
    "recipient",
    "currency0",
    "currency1",
    "hooks",
    "tick_spacing",
    "tick_lower",
    "tick_upper",
    "liquidity_delta",
    "salt",
    "amount0_raw",
    "amount1_raw",
)


def _replay_row(
    *,
    event_type: str,
    block_number: int,
    log_index: int,
    sqrt_price_x96: int,
    tick: int,
    block_time: str,
    config=None,
) -> dict[str, str]:
    resolved = POOL_CONFIGS["uni-base"] if config is None else config
    row = {field: "" for field in _REPLAY_FIELDS}
    row.update(
        {
            "block_time": block_time,
            "chain": resolved.chain,
            "pool_id": resolved.pool_id,
            "event_type": event_type,
            "tx_hash": f"0x{block_number:064x}",
            "log_index": str(log_index),
            "block_number": str(block_number),
            "sqrt_price_x96": str(sqrt_price_x96),
            "tick": str(tick),
            "active_liquidity": "1",
            "fee_rate": str(resolved.fee_rate),
            "amount0": "1",
            "amount1": "1",
            "amount_usd": "1",
            "cngn_usd_price": "999",
            "token0_symbol": resolved.token0_symbol,
            "token1_symbol": resolved.token1_symbol,
            "event_source": f"pool_manager_{event_type}",
        }
    )
    return row


def _write_replay_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_REPLAY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_action_discovery_stages_complete_witnesses_and_quiet_chunks(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    later = _raw_action_log(
        block_number=102,
        transaction_hash="0x" + "22" * 32,
        transaction_index=2,
        log_index=7,
    )
    earlier = _raw_action_log(
        block_number=101,
        transaction_hash="0x" + "11" * 32,
        transaction_index=1,
        log_index=4,
    )
    calls = []

    def fake_fetch(_w3, params, *, context):
        calls.append((params, context))
        return [later, earlier] if params["fromBlock"] == 100 else []

    monkeypatch.setattr(lp_ledger_export, "_fetch_logs_with_debug", fake_fetch)
    run = _ActionDiscoveryRun(
        (
            BlockRange(index=0, start_block=100, end_block=104),
            BlockRange(index=1, start_block=105, end_block=109),
        )
    )

    snapshot = lp_ledger_export._stage_action_discovery(
        run,
        object(),
        config,
        100,
        109,
    )

    assert snapshot.phase == "action_fetch"
    assert len(calls) == 2
    assert all(
        call[0]["address"] == Web3.to_checksum_address(config.pool_manager)
        and call[0]["topics"] == [V4_MODIFY_LIQUIDITY_TOPIC, config.pool_id]
        for call in calls
    )
    assert [
        (call[0]["fromBlock"], call[0]["toBlock"])
        for call in calls
    ] == [(100, 104), (105, 109)]
    assert run.commits == [
        (
            BlockRange(index=0, start_block=100, end_block=104),
            (_expected_action_witness(earlier), _expected_action_witness(later)),
        ),
        (BlockRange(index=1, start_block=105, end_block=109), ()),
    ]


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    (
        ("blockNumber", None),
        ("blockHash", "0x12"),
        ("transactionHash", None),
        ("transactionIndex", -1),
        ("logIndex", None),
        ("address", "0x" + "99" * 20),
        ("topics", ("0x" + "99" * 32, "0x" + "aa" * 32)),
        ("data", "not-hex"),
        ("removed", True),
    ),
)
def test_action_discovery_rejects_missing_or_wrong_log_identity(
    monkeypatch,
    field_name: str,
    bad_value: object,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    raw = _raw_action_log()
    raw[field_name] = bad_value
    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        lambda *_args, **_kwargs: [raw],
    )
    run = _ActionDiscoveryRun((BlockRange(index=0, start_block=100, end_block=104),))

    with pytest.raises(ValueError):
        lp_ledger_export._stage_action_discovery(
            run,
            object(),
            config,
            100,
            104,
        )

    assert run.commits == []


def test_action_discovery_resume_queries_only_incomplete_chunks(monkeypatch) -> None:
    config = POOL_CONFIGS["uni-base"]
    calls = []
    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        lambda _w3, params, *, context: calls.append((params, context)) or [],
    )
    run = _ActionDiscoveryRun((BlockRange(index=1, start_block=105, end_block=109),))

    lp_ledger_export._stage_action_discovery(run, object(), config, 100, 109)

    assert [(call[0]["fromBlock"], call[0]["toBlock"]) for call in calls] == [
        (105, 109)
    ]


def test_full_transfer_scan_canonicalizes_all_logs_before_filtering(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    relevant_raw = _raw_position_transfer_log(
        block_number=101,
        transaction_hash="0x" + "11" * 32,
        transaction_index=1,
        log_index=4,
        token_id=77,
    )
    irrelevant_raw = _raw_position_transfer_log(
        block_number=102,
        transaction_hash="0x" + "22" * 32,
        transaction_index=2,
        log_index=7,
        token_id=2**255 + 9,
    )
    calls: list[dict[str, object]] = []

    def fake_fetch(_w3, params, *, context):
        del context
        calls.append(params)
        return [irrelevant_raw, relevant_raw] if params["fromBlock"] == 100 else []

    monkeypatch.setattr(lp_ledger_export, "_fetch_logs_with_debug", fake_fetch)
    run = _TransferScanRun(
        (
            BlockRange(index=0, start_block=100, end_block=104),
            BlockRange(index=1, start_block=105, end_block=109),
        ),
        (77,),
    )

    snapshot = lp_ledger_export._stage_full_transfer_scan(
        run,
        object(),
        config,
        100,
        109,
    )

    relevant = _expected_position_transfer_witness(relevant_raw)
    irrelevant = _expected_position_transfer_witness(irrelevant_raw)
    unfiltered = (relevant, irrelevant)
    digest_payload = json.dumps(
        [asdict(witness) for witness in unfiltered],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert snapshot.phase == "relevant_transfer_fetch"
    assert all(
        call["address"] == Web3.to_checksum_address(config.position_manager)
        and call["topics"] == [lp_ledger_export._POSITION_MANAGER_TRANSFER_TOPIC]
        and len(call["topics"]) == 1  # type: ignore[arg-type]
        for call in calls
    )
    assert [(call["fromBlock"], call["toBlock"]) for call in calls] == [
        (100, 104),
        (105, 109),
    ]
    assert run.commits == [
        (
            BlockRange(index=0, start_block=100, end_block=104),
            2,
            hashlib.sha256(digest_payload).hexdigest(),
            (relevant,),
        ),
        (
            BlockRange(index=1, start_block=105, end_block=109),
            0,
            hashlib.sha256(b"[]").hexdigest(),
            (),
        ),
    ]


def test_full_transfer_scan_bounds_queries_and_splits_after_three_failures(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    calls: list[tuple[int, int]] = []
    failed_attempts = 0

    def fake_fetch(_w3, params, *, context):
        nonlocal failed_attempts
        del context
        query = (params["fromBlock"], params["toBlock"])
        calls.append(query)
        if query == (100, 2099) and failed_attempts < 3:
            failed_attempts += 1
            raise RequestsConnectionError("fixture transport failure")
        return []

    monkeypatch.setattr(lp_ledger_export, "_fetch_logs_with_debug", fake_fetch)
    parent = BlockRange(index=0, start_block=100, end_block=2100)
    run = _TransferScanRun((parent,), ())

    lp_ledger_export._stage_full_transfer_scan(
        run,
        object(),
        config,
        100,
        2100,
    )

    assert calls == [
        (100, 2099),
        (100, 2099),
        (100, 2099),
        (100, 1099),
        (1100, 2099),
        (2100, 2100),
    ]
    assert all(end - start + 1 <= 2_000 for start, end in calls)
    assert run.commits == [
        (parent, 0, hashlib.sha256(b"[]").hexdigest(), ()),
    ]


def test_transfer_parent_digest_is_invariant_to_result_limit_subdivision(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    relevant = _raw_position_transfer_log(
        block_number=101,
        transaction_hash="0x" + "11" * 32,
        transaction_index=1,
        log_index=4,
        token_id=77,
    )
    irrelevant = _raw_position_transfer_log(
        block_number=2100,
        transaction_hash="0x" + "22" * 32,
        transaction_index=2,
        log_index=7,
        token_id=999,
    )
    parent = BlockRange(index=0, start_block=100, end_block=2100)

    def successful_fetch(_w3, params, *, context):
        del context
        return [
            log
            for log in (irrelevant, relevant)
            if params["fromBlock"] <= int(str(log["blockNumber"]), 16)
            <= params["toBlock"]
        ]

    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        successful_fetch,
    )
    direct = _TransferScanRun((parent,), (77,))
    lp_ledger_export._stage_full_transfer_scan(
        direct,
        object(),
        config,
        100,
        2100,
    )

    attempts = 0

    def subdivided_fetch(_w3, params, *, context):
        nonlocal attempts
        del context
        if (params["fromBlock"], params["toBlock"]) == (100, 2099):
            attempts += 1
            raise lp_ledger_export.Web3RPCError(
                "Log response size exceeded the provider result limit"
            )
        return successful_fetch(_w3, params, context="fixture")

    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        subdivided_fetch,
    )
    subdivided = _TransferScanRun((parent,), (77,))
    lp_ledger_export._stage_full_transfer_scan(
        subdivided,
        object(),
        config,
        100,
        2100,
    )

    assert attempts == 3
    assert subdivided.commits == direct.commits


def test_transfer_log_retry_classification_is_fail_closed() -> None:
    assert lp_ledger_export._is_retryable_transfer_log_error(
        lp_ledger_export.Web3RPCError(
            "Log response size exceeded the provider result limit"
        )
    )
    assert lp_ledger_export._is_retryable_transfer_log_error(
        RuntimeError("RPC log request failed status=429 reason=fixture")
    )
    assert lp_ledger_export._is_retryable_transfer_log_error(
        RuntimeError("RPC log request failed status=400 reason=fixture")
    )
    assert not lp_ledger_export._is_retryable_transfer_log_error(
        lp_ledger_export.Web3RPCError("execution reverted")
    )
    assert not lp_ledger_export._is_retryable_transfer_log_error(
        RuntimeError("RPC log request failed status=401 reason=fixture")
    )


def test_transfer_scan_uses_a_provider_without_hidden_retries() -> None:
    config = replace(
        POOL_CONFIGS["uni-base"],
        rpc_url="http://127.0.0.1:1",
    )
    retrying = _make_web3(config)

    transfer_w3 = lp_ledger_export._transfer_scan_web3(retrying, config)

    assert transfer_w3 is not retrying
    assert transfer_w3.provider.endpoint_uri == retrying.provider.endpoint_uri
    assert transfer_w3.provider.exception_retry_configuration is None
    assert retrying.provider.exception_retry_configuration is not None

    mismatched = _make_web3(
        replace(config, rpc_url="https://bsc-mainnet.g.alchemy.com/v2/fixture")
    )
    with pytest.raises(ValueError, match="provider origin"):
        lp_ledger_export._transfer_scan_web3(mismatched, config)


def test_transfer_scan_three_attempts_are_three_http_requests() -> None:
    request_count = 0

    class FailingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal request_count
            request_count += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        config = replace(
            POOL_CONFIGS["uni-base"],
            rpc_url=f"http://{host}:{port}",
        )
        transfer_w3 = lp_ledger_export._transfer_scan_web3(
            _make_web3(config),
            config,
        )

        with pytest.raises(RuntimeError, match="status=503"):
            lp_ledger_export._fetch_position_transfer_query_with_subdivision(
                transfer_w3,
                config,
                100,
                100,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert request_count == 3


@pytest.mark.parametrize("failure", ("truncated", "single_block_transport"))
def test_full_transfer_scan_never_commits_incomplete_responses(
    monkeypatch,
    failure: str,
) -> None:
    config = POOL_CONFIGS["uni-base"]

    def fake_fetch(*_args, **_kwargs):
        if failure == "truncated":
            return {"logs": [], "truncated": True}
        raise RequestsConnectionError("fixture transport failure")

    monkeypatch.setattr(lp_ledger_export, "_fetch_logs_with_debug", fake_fetch)
    run = _TransferScanRun(
        (BlockRange(index=0, start_block=100, end_block=100),),
        (),
    )

    with pytest.raises((ValueError, RequestsConnectionError)):
        lp_ledger_export._stage_full_transfer_scan(
            run,
            object(),
            config,
            100,
            100,
        )

    assert run.commits == []


@pytest.mark.parametrize(
    "mutation",
    ("duplicate", "out_of_range", "noncanonical_owner", "conflicting_tx_slot"),
)
def test_full_transfer_scan_rejects_invalid_unfiltered_evidence(
    monkeypatch,
    mutation: str,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    raw = _raw_position_transfer_log(
        block_number=101,
        transaction_hash="0x" + "11" * 32,
        transaction_index=5,
        log_index=4,
        token_id=77,
    )
    if mutation == "duplicate":
        response = [raw, copy.deepcopy(raw)]
    elif mutation == "out_of_range":
        response = [
            _raw_position_transfer_log(
                block_number=99,
                transaction_hash="0x" + "11" * 32,
                transaction_index=5,
                log_index=4,
                token_id=77,
            )
        ]
    elif mutation == "noncanonical_owner":
        malformed = copy.deepcopy(raw)
        topics = list(malformed["topics"])
        topics[1] = bytes.fromhex("01" + "00" * 31)
        malformed["topics"] = tuple(topics)
        response = [malformed]
    else:
        response = [
            raw,
            _raw_position_transfer_log(
                block_number=101,
                transaction_hash="0x" + "22" * 32,
                transaction_index=5,
                log_index=7,
                token_id=999,
            ),
        ]
    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        lambda *_args, **_kwargs: response,
    )
    run = _TransferScanRun(
        (BlockRange(index=0, start_block=100, end_block=104),),
        (77,),
    )

    with pytest.raises(ValueError):
        lp_ledger_export._stage_full_transfer_scan(
            run,
            object(),
            config,
            100,
            104,
        )

    assert run.commits == []


def _candidate_rpc_payloads(
    witness: DiscoveryWitness,
) -> tuple[dict[str, object], dict[str, object]]:
    transaction = {
        "hash": witness.transaction_hash.upper().replace("0X", "0x"),
        "blockNumber": hex(witness.block_number),
        "blockHash": bytes.fromhex(witness.block_hash[2:]),
        "transactionIndex": hex(witness.transaction_index),
        "from": "0x" + "66" * 20,
        "to": "0x" + "77" * 20,
        "input": b"\x12\x34",
        "nonce": "0x0",
        "value": 0,
    }
    receipt = {
        "transactionHash": witness.transaction_hash,
        "blockNumber": witness.block_number,
        "blockHash": witness.block_hash,
        "transactionIndex": witness.transaction_index,
        "status": "0x1",
        "l1Fee": "0x1",
        "l1FeeScalar": "1.0",
        "logs": [
            {
                "address": Web3.to_checksum_address(witness.address),
                "blockNumber": hex(witness.block_number),
                "blockHash": bytes.fromhex(witness.block_hash[2:]),
                "transactionHash": witness.transaction_hash,
                "transactionIndex": hex(witness.transaction_index),
                "logIndex": hex(witness.log_index),
                "topics": [bytes.fromhex(topic[2:]) for topic in witness.topics],
                "data": bytes.fromhex(witness.data[2:]),
                "removed": False,
            }
        ],
    }
    return transaction, receipt


def test_action_bundle_fetch_normalizes_and_validates_exact_receipt_witness() -> None:
    witness = _expected_action_witness(_raw_action_log())
    transaction, receipt = _candidate_rpc_payloads(witness)
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_transaction=lambda _hash: transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )

    bundle = lp_ledger_export._fetch_and_validate_candidate_bundle(
        w3,
        witness.transaction_hash,
        (witness,),
        100,
        109,
    )

    normalized_transaction = json.loads(bundle.transaction_json)
    normalized_receipt = json.loads(bundle.receipt_json)
    assert bundle.transaction_hash == witness.transaction_hash
    assert bundle.block_number == witness.block_number
    assert bundle.block_hash == witness.block_hash
    assert normalized_transaction["blockNumber"] == str(witness.block_number)
    assert normalized_transaction["nonce"] == "0"
    assert normalized_transaction["value"] == "0"
    assert normalized_receipt["status"] == "1"
    assert normalized_receipt["l1Fee"] == "1"
    assert normalized_receipt["l1FeeScalar"] == "1.0"
    assert normalized_receipt["logs"][0]["topics"] == list(witness.topics)
    assert normalized_receipt["logs"][0]["removed"] is False
    assert bundle.payload_sha256 == lp_ledger_export.candidate_payload_sha256(
        bundle.transaction_json,
        bundle.receipt_json,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "transaction_hash",
        "receipt_hash",
        "block_hash",
        "transaction_index",
        "out_of_range",
        "missing_witness",
        "float_payload",
        "non_string_key",
        "failed_status",
        "removed_log",
        "unattested_action",
    ),
)
def test_action_bundle_fetch_rejects_response_disagreement(mutation: str) -> None:
    witness = _expected_action_witness(_raw_action_log())
    transaction, receipt = _candidate_rpc_payloads(witness)
    transaction = copy.deepcopy(transaction)
    receipt = copy.deepcopy(receipt)
    if mutation == "transaction_hash":
        transaction["hash"] = "0x" + "99" * 32
    elif mutation == "receipt_hash":
        receipt["transactionHash"] = "0x" + "99" * 32
    elif mutation == "block_hash":
        receipt["blockHash"] = "0x" + "99" * 32
    elif mutation == "transaction_index":
        receipt["transactionIndex"] = 6
    elif mutation == "out_of_range":
        transaction["blockNumber"] = 99
        receipt["blockNumber"] = 99
    elif mutation == "missing_witness":
        receipt["logs"] = []
    elif mutation == "float_payload":
        transaction["gas"] = 1.5
    elif mutation == "non_string_key":
        transaction[1] = "unsupported key"
    elif mutation == "failed_status":
        receipt["status"] = "0x0"
    elif mutation == "removed_log":
        receipt["logs"][0]["removed"] = True
    else:
        unattested_log = copy.deepcopy(receipt["logs"][0])
        unattested_log["logIndex"] = hex(witness.log_index + 1)
        receipt["logs"].append(unattested_log)
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_transaction=lambda _hash: transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )

    with pytest.raises(ValueError):
        lp_ledger_export._fetch_and_validate_candidate_bundle(
            w3,
            witness.transaction_hash,
            (witness,),
            100,
            109,
        )


def test_action_bundle_staging_fetches_only_pending_and_completes_zero_work() -> None:
    witness = _expected_action_witness(_raw_action_log())
    transaction, receipt = _candidate_rpc_payloads(witness)
    requested = []
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_transaction=lambda transaction_hash: requested.append(transaction_hash)
            or transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )
    run = _ActionBundleRun({witness.transaction_hash: (witness,)})

    snapshot = lp_ledger_export._stage_action_bundles(run, w3, 100, 109)

    assert snapshot.phase == "action_decode"
    assert requested == [witness.transaction_hash]
    assert [bundle.transaction_hash for bundle in run.commits] == [
        witness.transaction_hash
    ]
    assert run.completed == []

    zero_run = _ActionBundleRun({})
    zero_snapshot = lp_ledger_export._stage_action_bundles(
        zero_run,
        SimpleNamespace(eth=SimpleNamespace()),
        100,
        109,
    )
    assert zero_snapshot.phase == "action_decode"
    assert zero_run.completed == ["action_fetch"]

    reused_run = _ActionBundleRun(
        {witness.transaction_hash: (witness,)},
        phase="action_decode",
        pending_hashes=(),
    )
    reused_snapshot = lp_ledger_export._stage_action_bundles(
        reused_run,
        SimpleNamespace(eth=SimpleNamespace()),
        100,
        109,
    )
    assert reused_snapshot.phase == "action_decode"
    assert reused_run.commits == []
    assert reused_run.completed == []


def test_relevant_transfer_bundles_reuse_cache_and_fetch_only_missing_hashes() -> None:
    cached_witness = _expected_position_transfer_witness(
        _raw_position_transfer_log(
            block_number=101,
            transaction_hash="0x" + "11" * 32,
            transaction_index=1,
            log_index=4,
            token_id=77,
        )
    )
    missing_witness = _expected_position_transfer_witness(
        _raw_position_transfer_log(
            block_number=102,
            transaction_hash="0x" + "22" * 32,
            transaction_index=2,
            log_index=7,
            token_id=77,
        )
    )
    transaction, receipt = _candidate_rpc_payloads(missing_witness)
    requested: list[tuple[str, str]] = []
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_transaction=lambda transaction_hash: requested.append(
                ("transaction", str(transaction_hash))
            )
            or transaction,
            get_transaction_receipt=lambda transaction_hash: requested.append(
                ("receipt", str(transaction_hash))
            )
            or receipt,
        )
    )
    run = _RelevantTransferBundleRun(
        {
            cached_witness.transaction_hash: (cached_witness,),
            missing_witness.transaction_hash: (missing_witness,),
        },
        frozenset({cached_witness.transaction_hash}),
    )

    snapshot = lp_ledger_export._stage_relevant_transfer_bundles(
        run,
        w3,
        100,
        109,
    )

    assert snapshot.phase == "relevant_transfer_decode"
    assert run.reuse_calls == [
        cached_witness.transaction_hash,
        missing_witness.transaction_hash,
    ]
    assert requested == [
        ("transaction", missing_witness.transaction_hash),
        ("receipt", missing_witness.transaction_hash),
    ]
    assert [bundle.transaction_hash for bundle in run.commits] == [
        missing_witness.transaction_hash
    ]


def test_relevant_transfer_decode_filters_and_orders_frozen_ownership() -> None:
    config = POOL_CONFIGS["uni-base"]
    owner = "0x" + "aa" * 20
    receipt = {
        "blockNumber": "101",
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=owner,
                to_address=ZERO_ADDRESS,
                token_or_amount=77,
                log_index=9,
            ),
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=owner,
                token_or_amount=999,
                log_index=7,
            ),
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=owner,
                token_or_amount=77,
                log_index=5,
            ),
        ],
    }
    transaction_json = lp_ledger_export._canonical_rpc_json({})
    normalized_receipt = lp_ledger_export._normalize_rpc_payload(
        receipt,
        label="receipt",
    )
    assert isinstance(normalized_receipt, dict)
    receipt_json = lp_ledger_export._canonical_rpc_json(normalized_receipt)
    bundle = CandidateBundle(
        transaction_hash="0x" + "33" * 32,
        block_number=101,
        block_hash="0x" + "44" * 32,
        transaction_index=5,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=lp_ledger_export.candidate_payload_sha256(
            transaction_json,
            receipt_json,
        ),
    )
    run = _RelevantTransferDecodeRun((bundle,), (77,))

    snapshot = lp_ledger_export._stage_relevant_transfer_decode(run, config)

    checksum_owner = Web3.to_checksum_address(owner)
    assert snapshot.phase == "replay_input_bind"
    assert run.commits == [
        (
            bundle,
            (
                OwnershipEvent(101, 5, 77, None, checksum_owner),
                OwnershipEvent(101, 9, 77, checksum_owner, None),
            ),
        )
    ]


def test_empty_frozen_set_attests_scan_and_completes_transfer_zero_work(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    unrelated = _raw_position_transfer_log(token_id=999)
    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        lambda *_args, **_kwargs: [unrelated],
    )
    scan_run = _TransferScanRun(
        (BlockRange(index=0, start_block=100, end_block=109),),
        (),
    )

    lp_ledger_export._stage_full_transfer_scan(
        scan_run,
        object(),
        config,
        100,
        109,
    )
    bundle_run = _RelevantTransferBundleRun({})
    bundle_snapshot = lp_ledger_export._stage_relevant_transfer_bundles(
        bundle_run,
        SimpleNamespace(eth=SimpleNamespace()),
        100,
        109,
    )
    decode_run = _RelevantTransferDecodeRun((), ())
    decode_snapshot = lp_ledger_export._stage_relevant_transfer_decode(
        decode_run,
        config,
    )

    assert scan_run.commits[0][1] == 1
    assert scan_run.commits[0][3] == ()
    assert bundle_snapshot.phase == "relevant_transfer_decode"
    assert bundle_run.completed == ["relevant_transfer_fetch"]
    assert decode_snapshot.phase == "replay_input_bind"
    assert decode_run.completed == ["relevant_transfer_decode"]


def test_frozen_replay_binding_reuses_exact_local_sqrt_evidence(tmp_path: Path) -> None:
    config = POOL_CONFIGS["uni-base"]
    replay_path = tmp_path / "replay.csv"
    old_state = 2**96
    new_state = 2**96 + 1_000
    _write_replay_csv(
        replay_path,
        [
            _replay_row(
                event_type="initialize",
                block_number=100,
                log_index=1,
                sqrt_price_x96=old_state,
                tick=0,
                block_time="2026-01-01T00:00:00+00:00",
            ),
            _replay_row(
                event_type="swap",
                block_number=101,
                log_index=10,
                sqrt_price_x96=new_state,
                tick=1,
                block_time="2026-01-01T00:00:01+00:00",
            ),
        ],
    )
    frozen = lp_ledger_export._load_frozen_replay_input(replay_path, config)
    run = _ReplayRun(frozen.evidence, ())

    snapshot = lp_ledger_export._bind_frozen_replay_input(
        run,
        config,
        frozen.evidence,
    )

    assert snapshot.phase == "price_replay"
    assert run.bound_inputs == [frozen.evidence]
    assert frozen.evidence.path == str(replay_path.resolve())
    assert frozen.evidence.row_count == 2
    assert frozen.evidence.price_event_count == 2
    assert frozen.evidence.first_block == 100
    assert frozen.evidence.last_block == 101
    assert tuple(event.sqrt_price_x96 for event in frozen.price_events) == (
        old_state,
        new_state,
    )


def test_frozen_replay_parser_rejects_noncanonical_pool_id(
    tmp_path: Path,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    replay_path = tmp_path / "replay.csv"
    row = _replay_row(
        event_type="initialize",
        block_number=100,
        log_index=1,
        sqrt_price_x96=2**96,
        tick=0,
        block_time="2026-01-01T00:00:00+00:00",
    )
    row["pool_id"] = config.pool_id.upper()
    _write_replay_csv(replay_path, [row])

    with pytest.raises(ValueError, match="pool identity"):
        lp_ledger_export._load_frozen_replay_input(replay_path, config)


def test_frozen_replay_non_price_rows_do_not_require_price_state_fields(
    tmp_path: Path,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    replay_path = tmp_path / "replay.csv"
    initialize = _replay_row(
        event_type="initialize",
        block_number=100,
        log_index=1,
        sqrt_price_x96=2**96,
        tick=0,
        block_time="2026-01-01T00:00:00+00:00",
    )
    mint = _replay_row(
        event_type="mint",
        block_number=101,
        log_index=2,
        sqrt_price_x96=1,
        tick=0,
        block_time="2026-01-01T00:00:01+00:00",
    )
    mint["event_source"] = "pool_manager_modify_liquidity"
    mint["sqrt_price_x96"] = ""
    mint["tick"] = ""
    _write_replay_csv(replay_path, [initialize, mint])

    frozen = lp_ledger_export._load_frozen_replay_input(replay_path, config)

    assert frozen.evidence.row_count == 2
    assert frozen.evidence.price_event_count == 1
    assert len(frozen.price_events) == 1


def test_staged_local_price_replay_respects_same_block_log_order(
    tmp_path: Path,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    replay_path = tmp_path / "replay.csv"
    old_state = 2**96
    new_state = 2**96 + 1_000
    _write_replay_csv(
        replay_path,
        [
            _replay_row(
                event_type="initialize",
                block_number=100,
                log_index=1,
                sqrt_price_x96=old_state,
                tick=0,
                block_time="2026-01-01T00:00:00+00:00",
            ),
            _replay_row(
                event_type="swap",
                block_number=101,
                log_index=10,
                sqrt_price_x96=new_state,
                tick=1,
                block_time="2026-01-01T00:00:01+00:00",
            ),
        ],
    )
    frozen = lp_ledger_export._load_frozen_replay_input(replay_path, config)
    actions = (
        DecodedLiquidityAction(
            action_type="mint",
            block_number=101,
            log_index=5,
            event_order=0,
            token_id=1,
            lp_owner=None,
            tick_lower=-10,
            tick_upper=10,
            liquidity_delta=1,
            amount0=0,
            amount1=0,
            collect_amount0=0,
        ),
        DecodedLiquidityAction(
            action_type="burn",
            block_number=101,
            log_index=15,
            event_order=0,
            token_id=1,
            lp_owner=None,
            tick_lower=-10,
            tick_upper=10,
            liquidity_delta=-1,
            amount0=0,
            amount1=0,
            collect_amount0=0,
        ),
    )
    run = _ReplayRun(frozen.evidence, actions)
    run.phase = "price_replay"

    snapshot = lp_ledger_export._stage_price_replay(run, config)

    assert snapshot.phase == "build"
    assert len(run.binding_commits) == 1
    before, after = run.binding_commits[0]
    assert before.event_time_sqrt_price_x96 == old_state
    assert before.event_time_tick == 0
    assert before.event_time_state_source == "prior_event"
    assert after.event_time_sqrt_price_x96 == new_state
    assert after.event_time_tick == 1
    assert after.event_time_state_source == "same_block_prior_event"


@pytest.mark.parametrize(
    "mutation",
    (
        "changed_bytes",
        "crossed_order",
        "duplicate_order",
        "wrong_chain",
        "zero_sqrt",
        "submillisecond_time",
    ),
)
def test_frozen_replay_binding_rejects_drift_and_malformed_rows(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    replay_path = tmp_path / "replay.csv"
    rows = [
        _replay_row(
            event_type="initialize",
            block_number=100,
            log_index=1,
            sqrt_price_x96=2**96,
            tick=0,
            block_time="2026-01-01T00:00:00+00:00",
        ),
        _replay_row(
            event_type="swap",
            block_number=101,
            log_index=10,
            sqrt_price_x96=2**96 + 1_000,
            tick=1,
            block_time="2026-01-01T00:00:01+00:00",
        ),
    ]
    _write_replay_csv(replay_path, rows)
    expected = lp_ledger_export._load_frozen_replay_input(
        replay_path,
        config,
    ).evidence
    if mutation == "changed_bytes":
        rows[1]["cngn_usd_price"] = "998"
    elif mutation == "crossed_order":
        rows.reverse()
    elif mutation == "duplicate_order":
        rows[1]["block_number"] = rows[0]["block_number"]
        rows[1]["log_index"] = rows[0]["log_index"]
    elif mutation == "wrong_chain":
        rows[1]["chain"] = "bsc"
    elif mutation == "zero_sqrt":
        rows[1]["sqrt_price_x96"] = "0"
    else:
        rows[1]["block_time"] = "2026-01-01T00:00:01.000001+00:00"
    _write_replay_csv(replay_path, rows)
    run = _ReplayRun(expected, ())

    with pytest.raises(ValueError):
        lp_ledger_export._bind_frozen_replay_input(run, config, expected)

    assert run.bound_inputs == []


def test_action_decode_stages_state_key_header_and_reconciled_action(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    transaction_hash = "0x" + "33" * 32
    block_hash = "0x" + "44" * 32
    transaction = {
        "to": config.position_manager,
        "from": recipient,
        "hash": transaction_hash,
        "blockNumber": "101",
        "transactionIndex": "5",
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION]),
            [
                _build_mint_param(
                    config,
                    tick_lower=-120,
                    tick_upper=120,
                    liquidity_delta=999,
                    recipient=recipient,
                )
            ],
        ),
    }
    receipt = {
        "blockNumber": "101",
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(11),
        ]
    }
    transaction_json = lp_ledger_export._canonical_rpc_json(
        lp_ledger_export._normalize_rpc_payload(transaction, label="transaction")
    )
    receipt_json = lp_ledger_export._canonical_rpc_json(
        lp_ledger_export._normalize_rpc_payload(receipt, label="receipt")
    )
    bundle = CandidateBundle(
        transaction_hash=transaction_hash,
        block_number=101,
        block_hash=block_hash,
        transaction_index=5,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=lp_ledger_export.candidate_payload_sha256(
            transaction_json,
            receipt_json,
        ),
    )
    run = _ActionDecodeRun(config, (bundle,))
    monkeypatch.setattr(
        lp_ledger_export,
        "_coverage_block_header",
        lambda _w3, block_number: (block_hash, block_number * 1_000),
    )
    w3 = SimpleNamespace(
        eth=SimpleNamespace(contract=lambda **_kwargs: object())
    )

    snapshot = lp_ledger_export._stage_action_decode(run, w3, config)

    assert snapshot.phase == "token_set_freeze"
    assert len(run.commits) == 1
    commit = run.commits[0]
    assert commit["headers"] == (BlockHeader(101, block_hash, 101_000),)
    assert commit["resolutions"] == ()
    assert commit["state_upserts"] == (
        DecoderStateUpsert(
            77,
            LedgerPositionState(config.pool_id, -120, 120, 999),
            101,
            11,
            0,
        ),
    )
    assert commit["state_deletes"] == ()
    actions = commit["actions"]
    assert [(action.token_id, action.log_index) for action in actions] == [(77, 11)]
    assert commit["position_keys"] == (
        PositionKeyMapping(
            token_id=77,
            pool_id=config.pool_id,
            tick_lower=-120,
            tick_upper=120,
            salt="0x" + f"{77:064x}",
            mint_block_number=101,
            mint_log_index=11,
            mint_event_order=0,
        ),
    )


def test_action_decode_real_checkpoint_resumes_without_duplicate_work(
    tmp_path,
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    transaction_hash = "0x" + "71" * 32
    block_hash = "0x" + "72" * 32
    modify_log = _modify_liquidity_log(11)
    modify_log.update(
        {
            "blockNumber": "101",
            "blockHash": block_hash,
            "transactionHash": transaction_hash,
            "transactionIndex": "5",
            "removed": False,
        }
    )
    transaction = {
        "to": config.position_manager,
        "from": recipient,
        "hash": transaction_hash,
        "blockNumber": "101",
        "blockHash": block_hash,
        "transactionIndex": "5",
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION]),
            [
                _build_mint_param(
                    config,
                    tick_lower=-120,
                    tick_upper=120,
                    liquidity_delta=999,
                    recipient=recipient,
                )
            ],
        ),
    }
    receipt = {
        "transactionHash": transaction_hash,
        "blockNumber": "101",
        "blockHash": block_hash,
        "transactionIndex": "5",
        "status": "1",
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            modify_log,
        ],
    }
    transaction_json = lp_ledger_export._canonical_rpc_json(
        lp_ledger_export._normalize_rpc_payload(transaction, label="transaction")
    )
    receipt_json = lp_ledger_export._canonical_rpc_json(
        lp_ledger_export._normalize_rpc_payload(receipt, label="receipt")
    )
    bundle = CandidateBundle(
        transaction_hash=transaction_hash,
        block_number=101,
        block_hash=block_hash,
        transaction_index=5,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=lp_ledger_export.candidate_payload_sha256(
            transaction_json,
            receipt_json,
        ),
    )
    witness = DiscoveryWitness(
        source="pool_modify",
        block_number=101,
        block_hash=block_hash,
        transaction_hash=transaction_hash,
        transaction_index=5,
        log_index=11,
        address=config.pool_manager.lower(),
        topics=tuple(str(topic).lower() for topic in modify_log["topics"]),
        data=str(modify_log["data"]).lower(),
    )
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _action_decode_checkpoint_identity(paths.output, config)
    header_calls: list[int] = []

    def header(_w3, block_number: int) -> tuple[str, int]:
        header_calls.append(block_number)
        assert block_number == 101
        return block_hash, 101_000

    monkeypatch.setattr(lp_ledger_export, "_coverage_block_header", header)
    w3 = SimpleNamespace(
        eth=SimpleNamespace(contract=lambda **_kwargs: object())
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            identity,
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(
                    chunk,
                    (witness,) if chunk.start_block <= 101 <= chunk.end_block else (),
                )
            run.commit_action_bundle(bundle)
            first = lp_ledger_export._stage_action_decode(run, w3, config)

            assert first.phase == "token_set_freeze"
            assert run.load_decoder_state() == {
                77: LedgerPositionState(config.pool_id, -120, 120, 999)
            }
            assert [action.token_id for action in run.load_decoded_actions()] == [77]

        with LPLedgerCheckpoint.create_or_resume(
            paths,
            identity,
            fresh=False,
        ) as resumed:
            second = lp_ledger_export._stage_action_decode(resumed, w3, config)

            assert second.phase == "token_set_freeze"
            assert resumed.undecoded_action_bundles() == ()
            assert len(resumed.load_decoded_actions()) == 1
            frozen = lp_ledger_export._freeze_action_token_set(resumed)
            assert frozen.phase == "full_transfer_scan"
            assert resumed.frozen_token_ids() == (77,)

    assert header_calls == [101]


def test_action_decode_completes_empty_phase_and_freezes_only_after_decode() -> None:
    config = POOL_CONFIGS["uni-base"]
    run = _ActionDecodeRun(config, ())

    decoded = lp_ledger_export._stage_action_decode(
        run,
        SimpleNamespace(eth=SimpleNamespace(contract=lambda **_kwargs: object())),
        config,
    )
    frozen = lp_ledger_export._freeze_action_token_set(run)

    assert decoded.phase == "token_set_freeze"
    assert run.completed == ["action_decode"]
    assert frozen.phase == "full_transfer_scan"
    assert run.freeze_calls == 1


def test_action_decode_identity_rejects_a_different_wrapper_entrypoint() -> None:
    config = POOL_CONFIGS["uni-base"]
    identity = SimpleNamespace(
        pool=config.name,
        chain=config.chain,
        pool_id=config.pool_id,
        pool_manager=config.pool_manager,
        position_manager=config.position_manager,
        wrapper_entrypoint="0x" + "99" * 20,
    )

    with pytest.raises(ValueError, match="EntryPoint"):
        lp_ledger_export._require_matching_action_decode_identity(identity, config)


def test_action_decode_fetches_each_resolution_header_once(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    transaction_hash = "0x" + "33" * 32
    block_hash = "0x" + "44" * 32
    transaction_json = lp_ledger_export._canonical_rpc_json({})
    receipt_json = lp_ledger_export._canonical_rpc_json(
        {"blockNumber": "101", "logs": []}
    )
    bundle = CandidateBundle(
        transaction_hash=transaction_hash,
        block_number=101,
        block_hash=block_hash,
        transaction_index=5,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=lp_ledger_export.candidate_payload_sha256(
            transaction_json,
            receipt_json,
        ),
    )
    run = _ActionDecodeRun(config, (bundle,))
    header_calls: list[int] = []

    def fetch_header(_w3, block_number: int) -> tuple[str, int]:
        header_calls.append(block_number)
        return (
            block_hash if block_number == 101 else "0x" + "55" * 32,
            block_number * 1_000,
        )

    def decode_with_two_resolutions(
        *_args,
        position_resolver,
        **_kwargs,
    ):
        assert position_resolver(55, 100) is None
        assert position_resolver(66, 100) is None
        return []

    monkeypatch.setattr(lp_ledger_export, "_coverage_block_header", fetch_header)
    monkeypatch.setattr(
        lp_ledger_export,
        "_strict_position_state_from_chain",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        lp_ledger_export,
        "decode_liquidity_actions_for_tx",
        decode_with_two_resolutions,
    )

    lp_ledger_export._stage_action_decode(
        run,
        SimpleNamespace(
            eth=SimpleNamespace(contract=lambda **_kwargs: object()),
        ),
        config,
    )

    assert header_calls == [101, 100]
    assert run.commits[0]["resolutions"] == (
        lp_ledger_export.PositionResolution(55, 100, False, None),
        lp_ledger_export.PositionResolution(66, 100, False, None),
    )


def test_action_decode_rejects_target_position_missing_its_inception_mint(
    monkeypatch,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    transaction_hash = "0x" + "61" * 32
    block_hash = "0x" + "62" * 32
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, "0x" + "bb" * 20],
    )
    transaction = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": transaction_hash,
        "blockNumber": "101",
        "transactionIndex": "5",
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease, take_pair],
        ),
    }
    receipt = {
        "blockNumber": "101",
        "logs": [
            _modify_liquidity_log(
                11,
                liquidity_delta=-400,
                salt_token_id=55,
            )
        ],
    }
    transaction_json = lp_ledger_export._canonical_rpc_json(
        lp_ledger_export._normalize_rpc_payload(transaction, label="transaction")
    )
    receipt_json = lp_ledger_export._canonical_rpc_json(
        lp_ledger_export._normalize_rpc_payload(receipt, label="receipt")
    )
    bundle = CandidateBundle(
        transaction_hash=transaction_hash,
        block_number=101,
        block_hash=block_hash,
        transaction_index=5,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=lp_ledger_export.candidate_payload_sha256(
            transaction_json,
            receipt_json,
        ),
    )
    run = _ActionDecodeRun(config, (bundle,))
    monkeypatch.setattr(
        lp_ledger_export,
        "_coverage_block_header",
        lambda _w3, block_number: (
            block_hash if block_number == 101 else "0x" + "63" * 32,
            block_number * 1_000,
        ),
    )
    monkeypatch.setattr(
        lp_ledger_export,
        "_strict_position_state_from_chain",
        lambda *_args, **_kwargs: LedgerPositionState(
            config.pool_id,
            -120,
            120,
            1_000,
        ),
    )

    with pytest.raises(ValueError, match="inception action history"):
        lp_ledger_export._stage_action_decode(
            run,
            SimpleNamespace(
                eth=SimpleNamespace(contract=lambda **_kwargs: object()),
            ),
            config,
        )

    assert run.commits == []


def test_verified_rpc_entrypoint_fails_closed_before_legacy_scan(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        lp_ledger_export,
        "_make_web3",
        lambda _config: (_ for _ in ()).throw(AssertionError("RPC must not start")),
    )

    with pytest.raises(CrossPoolContractError, match="action-first"):
        lp_ledger_export.export_rpc_lp_ledger(
            "uni-base",
            100,
            109,
            tmp_path / "ledger.csv",
        )


def test_verified_decode_rejects_an_unrepresentable_target_pool_action(
    monkeypatch,
):
    config = POOL_CONFIGS["uni-base"]
    transaction_hash = "0x" + "44" * 32
    transaction = {
        "hash": transaction_hash,
        "input": "0xdeadbeef",
        "to": config.position_manager,
        "from": "0x" + "11" * 20,
        "blockNumber": 100,
        "transactionIndex": 0,
    }
    receipt = {
        "transactionHash": transaction_hash,
        "blockNumber": 100,
        "transactionIndex": 0,
        "logs": [_modify_liquidity_log(1)],
    }
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda _hash: transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )
    monkeypatch.setattr(
        lp_ledger_export,
        "_block_timestamp_for_number",
        lambda *_args: 1_700_000_000,
    )

    with pytest.raises(ValueError, match="ModifyLiquidity"):
        lp_ledger_export._decode_rpc_lp_inputs(
            fake_w3,
            config,
            (transaction_hash,),
            required_action_tx_hashes=frozenset((transaction_hash,)),
        )


@pytest.mark.parametrize(
    "mismatch_source",
    ("transaction", "receipt", "missing_receipt"),
)
def test_verified_decode_binds_rpc_responses_to_the_requested_hash(
    monkeypatch,
    mismatch_source,
):
    config = POOL_CONFIGS["uni-base"]
    requested_hash = "0x" + "44" * 32
    other_hash = "0x" + "55" * 32
    transaction = {
        "hash": other_hash if mismatch_source == "transaction" else requested_hash,
        "input": "0x",
        "blockNumber": 100,
        "transactionIndex": 0,
    }
    receipt = {
        "blockNumber": 100,
        "transactionIndex": 0,
        "logs": [],
    }
    if mismatch_source != "missing_receipt":
        receipt["transactionHash"] = other_hash
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda _hash: transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )

    with pytest.raises(ValueError, match="requested candidate hash"):
        lp_ledger_export._decode_rpc_lp_inputs(
            fake_w3,
            config,
            (requested_hash,),
            required_action_tx_hashes=frozenset(),
        )


@pytest.mark.parametrize(
    ("field_name", "failure_mode"),
    (
        ("blockNumber", "mismatch"),
        ("transactionIndex", "mismatch"),
        ("blockNumber", "missing_transaction"),
        ("blockNumber", "missing_receipt"),
        ("blockNumber", "none_transaction"),
        ("blockNumber", "none_receipt"),
        ("transactionIndex", "missing_transaction"),
        ("transactionIndex", "missing_receipt"),
        ("transactionIndex", "none_transaction"),
        ("transactionIndex", "none_receipt"),
        ("blockNumber", "boolean_both"),
        ("transactionIndex", "float_both"),
    ),
)
def test_verified_decode_requires_one_transaction_location(
    monkeypatch,
    field_name,
    failure_mode,
):
    config = POOL_CONFIGS["uni-base"]
    requested_hash = "0x" + "44" * 32
    transaction = {
        "hash": requested_hash,
        "input": "0x",
        "blockNumber": 100,
        "transactionIndex": 0,
    }
    receipt = {
        "transactionHash": requested_hash,
        "blockNumber": 100,
        "transactionIndex": 0,
        "logs": [],
    }
    if failure_mode == "mismatch":
        receipt[field_name] += 1
    elif failure_mode == "missing_transaction":
        del transaction[field_name]
    elif failure_mode == "missing_receipt":
        del receipt[field_name]
    elif failure_mode == "none_transaction":
        transaction[field_name] = None
    elif failure_mode == "none_receipt":
        receipt[field_name] = None
    elif failure_mode == "boolean_both":
        transaction[field_name] = True
        receipt[field_name] = True
    else:
        transaction[field_name] = 1.5
        receipt[field_name] = 1.5
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda _hash: transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )
    monkeypatch.setattr(
        lp_ledger_export,
        "_block_timestamp_for_number",
        lambda *_args: 1_700_000_000,
    )

    with pytest.raises(ValueError, match=field_name) as exc_info:
        lp_ledger_export._decode_rpc_lp_inputs(
            fake_w3,
            config,
            (requested_hash,),
            required_action_tx_hashes=frozenset(),
        )
    assert requested_hash in str(exc_info.value)


def test_verified_decode_requires_one_action_per_target_pool_modify_log(
    monkeypatch,
):
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    mint_param = encode(
        [
            "(address,address,uint24,int24,address)",
            "int24",
            "int24",
            "uint256",
            "uint128",
            "uint128",
            "address",
            "bytes",
        ],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                ZERO_ADDRESS,
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    transaction_hash = "0x" + "66" * 32
    transaction = {
        "hash": transaction_hash,
        "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION]), [mint_param]),
        "to": config.position_manager,
        "from": "0x" + "11" * 20,
        "blockNumber": 100,
        "transactionIndex": 0,
    }
    receipt = {
        "transactionHash": transaction_hash,
        "blockNumber": 100,
        "transactionIndex": 0,
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(8),
            _modify_liquidity_log(9),
        ],
    }
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda _hash: transaction,
            get_transaction_receipt=lambda _hash: receipt,
        )
    )
    monkeypatch.setattr(
        lp_ledger_export,
        "_block_timestamp_for_number",
        lambda *_args: 1_700_000_000,
    )

    with pytest.raises(ValueError, match="ModifyLiquidity"):
        lp_ledger_export._decode_rpc_lp_inputs(
            fake_w3,
            config,
            (transaction_hash,),
            required_action_tx_hashes=frozenset((transaction_hash,)),
        )


def test_fixture_export_rejects_non_csv_output_before_reading_inputs(tmp_path):
    output = tmp_path / "ledger.txt"

    with pytest.raises(CrossPoolContractError, match="requires a .csv"):
        lp_ledger_export.export_fixture_lp_ledger(
            "uni-base",
            100,
            100,
            tmp_path / "missing-actions.json",
            tmp_path / "missing-ownership.json",
            tmp_path / "missing-prices.json",
            output,
        )

    assert not output.exists()


@pytest.mark.parametrize("preexisting_pair", (False, True))
def test_pair_publication_rolls_back_when_sidecar_replace_fails(
    tmp_path,
    monkeypatch,
    preexisting_pair,
):
    output = tmp_path / "ledger.csv"
    sidecar = ledger_coverage_path(output)
    old_ledger, old_coverage = _minimal_coverage_pair_bytes(100)
    new_ledger, new_coverage = _minimal_coverage_pair_bytes(101)
    if preexisting_pair:
        lp_ledger_export._publish_ledger_pair(
            "uni-base",
            output,
            old_ledger,
            old_coverage,
        )
    original_replace = lp_ledger_export._replace_path
    failed = False

    def fail_first_sidecar_replace(source: Path, destination: Path) -> None:
        nonlocal failed
        if destination == sidecar and not failed:
            failed = True
            raise OSError("injected sidecar publication failure")
        original_replace(source, destination)

    monkeypatch.setattr(
        lp_ledger_export,
        "_replace_path",
        fail_first_sidecar_replace,
    )

    with pytest.raises(OSError, match="injected sidecar"):
        lp_ledger_export._publish_ledger_pair(
            "uni-base",
            output,
            new_ledger,
            new_coverage,
        )

    if preexisting_pair:
        assert output.read_bytes() == old_ledger
        assert sidecar.read_bytes() == old_coverage
        assert load_ledger_coverage("uni-base", output).ledger_first_block == 100
    else:
        assert not output.exists()
        assert not sidecar.exists()


def test_pair_publication_rejects_a_concurrent_writer(tmp_path) -> None:
    output = tmp_path / "ledger.csv"
    ledger_bytes, coverage_bytes = _minimal_coverage_pair_bytes(100)

    with lp_ledger_export._publication_lock(output):
        with pytest.raises(CrossPoolContractError, match="already active"):
            lp_ledger_export._publish_ledger_pair(
                "uni-base",
                output,
                ledger_bytes,
                coverage_bytes,
            )

    assert not output.exists()
    assert not ledger_coverage_path(output).exists()


def test_pair_publication_lock_serializes_exporter_processes(tmp_path) -> None:
    output = tmp_path / "ledger.csv"
    ledger_bytes, coverage_bytes = _minimal_coverage_pair_bytes(100)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from research.scripts.export_v4_lp_ledger import _publication_lock\n"
        "with _publication_lock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(output)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(CrossPoolContractError, match="already active"):
            lp_ledger_export._publish_ledger_pair(
                "uni-base",
                output,
                ledger_bytes,
                coverage_bytes,
            )
        assert not output.exists()
        assert not ledger_coverage_path(output).exists()

        process.stdin.write("\n")
        process.stdin.flush()
        return_code = process.wait(timeout=10)
        assert return_code == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    lp_ledger_export._publish_ledger_pair(
        "uni-base",
        output,
        ledger_bytes,
        coverage_bytes,
    )
    assert load_ledger_coverage("uni-base", output).ledger_first_block == 100


@pytest.mark.parametrize("existing_state", ("partial", "mismatched"))
def test_pair_publication_refuses_invalid_existing_outputs(
    tmp_path,
    existing_state,
):
    output = tmp_path / "ledger.csv"
    sidecar = ledger_coverage_path(output)
    old_ledger, old_coverage = _minimal_coverage_pair_bytes(100)
    new_ledger, new_coverage = _minimal_coverage_pair_bytes(101)
    output.write_bytes(old_ledger)
    if existing_state == "mismatched":
        sidecar.write_bytes(new_coverage)
    original_sidecar = sidecar.read_bytes() if sidecar.exists() else None

    with pytest.raises(CrossPoolContractError):
        lp_ledger_export._publish_ledger_pair(
            "uni-base",
            output,
            new_ledger,
            new_coverage,
        )

    assert output.read_bytes() == old_ledger
    if original_sidecar is None:
        assert not sidecar.exists()
    else:
        assert sidecar.read_bytes() == original_sidecar


def test_pair_publication_preserves_recovery_sidecar_when_rollback_fails(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "ledger.csv"
    sidecar = ledger_coverage_path(output)
    old_ledger, old_coverage = _minimal_coverage_pair_bytes(100)
    new_ledger, new_coverage = _minimal_coverage_pair_bytes(101)
    lp_ledger_export._publish_ledger_pair(
        "uni-base",
        output,
        old_ledger,
        old_coverage,
    )
    original_load = lp_ledger_export.load_ledger_coverage
    original_replace = lp_ledger_export._replace_path
    sidecar_replacements = 0

    def fail_final_validation(pool: str, path: Path):
        if path == output:
            raise CrossPoolContractError("injected final validation failure")
        return original_load(pool, path)

    def fail_sidecar_rollback(source: Path, destination: Path) -> None:
        nonlocal sidecar_replacements
        if destination == sidecar:
            sidecar_replacements += 1
            if sidecar_replacements == 2:
                raise OSError("injected sidecar rollback failure")
        original_replace(source, destination)

    monkeypatch.setattr(
        lp_ledger_export,
        "load_ledger_coverage",
        fail_final_validation,
    )
    monkeypatch.setattr(
        lp_ledger_export,
        "_replace_path",
        fail_sidecar_rollback,
    )

    with pytest.raises(RuntimeError, match="rollback failed.*backup"):
        lp_ledger_export._publish_ledger_pair(
            "uni-base",
            output,
            new_ledger,
            new_coverage,
        )

    assert output.read_bytes() == old_ledger
    assert sidecar.read_bytes() == new_coverage
    with pytest.raises(CrossPoolContractError, match="SHA-256"):
        original_load("uni-base", output)
    recovery_sidecars = tuple(tmp_path.glob(".ledger.backup.*.csv.coverage.json"))
    assert len(recovery_sidecars) == 1
    assert recovery_sidecars[0].read_bytes() == old_coverage


def test_rpc_header_failure_leaves_no_published_ledger_pair(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "ledger.csv"
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            chain_id=8453,
            contract=lambda **_kwargs: object(),
        )
    )
    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(
        lp_ledger_export,
        "_coverage_block_header",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("endpoint RPC failed")),
    )

    with pytest.raises(RuntimeError, match="endpoint RPC failed"):
        lp_ledger_export.export_rpc_lp_ledger(
            "uni-base",
            100,
            100,
            output,
            candidate_tx_hashes=(),
        )

    assert not output.exists()
    assert not ledger_coverage_path(output).exists()


def test_rpc_chain_identity_must_match_the_frozen_pool() -> None:
    fake_w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=56))

    with pytest.raises(ValueError, match="does not match 8453"):
        lp_ledger_export._coverage_chain_id(fake_w3, "uni-base")


def test_candidate_list_export_rejects_the_wrong_rpc_chain(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "ledger.csv"
    fake_w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=56))

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)

    with pytest.raises(ValueError, match="does not match 8453"):
        lp_ledger_export.export_rpc_lp_ledger(
            "uni-base",
            100,
            100,
            output,
            candidate_tx_hashes=(),
        )

    assert not output.exists()
    assert not ledger_coverage_path(output).exists()


def test_rpc_export_brackets_data_reads_with_endpoint_snapshots(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "ledger.csv"
    events: list[str] = []
    fake_w3 = object()

    def header(_w3, block_number):
        events.append(f"header:{block_number}")
        return f"0x{block_number:064x}", 1_700_000_000_000 + block_number

    def decode(*_args, **_kwargs):
        events.append("decode")
        return [], []

    def replay(*_args):
        events.append("replay")
        return []

    def build_coverage(*_args, **_kwargs):
        events.append("coverage")
        assert _kwargs["covered_start_block_hash"] == f"0x{100:064x}"
        assert _kwargs["covered_end_block_hash"] == f"0x{102:064x}"
        return b"coverage"

    def publish(*_args):
        events.append("publish")

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(lp_ledger_export, "_coverage_chain_id", lambda *_args: 8453)
    monkeypatch.setattr(lp_ledger_export, "_coverage_block_header", header)
    monkeypatch.setattr(lp_ledger_export, "_decode_rpc_lp_inputs", decode)
    monkeypatch.setattr(lp_ledger_export, "_replayed_price_events_for_actions", replay)
    monkeypatch.setattr(
        lp_ledger_export,
        "build_candidate_list_ledger_coverage_bytes",
        build_coverage,
    )
    monkeypatch.setattr(lp_ledger_export, "_publish_ledger_pair", publish)

    count = lp_ledger_export.export_rpc_lp_ledger(
        "uni-base",
        100,
        102,
        output,
        candidate_tx_hashes=(),
    )

    assert count == 0
    assert events == [
        "header:100",
        "header:102",
        "decode",
        "replay",
        "header:100",
        "header:102",
        "coverage",
        "publish",
    ]


@pytest.mark.parametrize(
    ("changed_endpoint", "changed_field"),
    (
        (100, "hash"),
        (102, "hash"),
        (100, "timestamp"),
        (102, "timestamp"),
    ),
)
def test_rpc_export_rejects_changed_endpoint_snapshot(
    tmp_path,
    monkeypatch,
    changed_endpoint,
    changed_field,
) -> None:
    output = tmp_path / "ledger.csv"
    header_reads = {100: 0, 102: 0}
    built_coverage = False
    published = False

    def header(_w3, block_number):
        header_reads[block_number] += 1
        block_hash = f"0x{block_number:064x}"
        timestamp_ms = 1_700_000_000_000 + block_number
        if block_number == changed_endpoint and header_reads[block_number] == 2:
            if changed_field == "hash":
                block_hash = "0x" + "ff" * 32
            else:
                timestamp_ms += 1
        return block_hash, timestamp_ms

    def build_coverage(*_args, **_kwargs):
        nonlocal built_coverage
        built_coverage = True
        return b"coverage"

    def publish(*_args):
        nonlocal published
        published = True

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: object())
    monkeypatch.setattr(lp_ledger_export, "_coverage_chain_id", lambda *_args: 8453)
    monkeypatch.setattr(lp_ledger_export, "_coverage_block_header", header)
    monkeypatch.setattr(lp_ledger_export, "_decode_rpc_lp_inputs", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(lp_ledger_export, "_replayed_price_events_for_actions", lambda *_args: [])
    monkeypatch.setattr(
        lp_ledger_export,
        "build_candidate_list_ledger_coverage_bytes",
        build_coverage,
    )
    monkeypatch.setattr(lp_ledger_export, "_publish_ledger_pair", publish)

    with pytest.raises(CrossPoolContractError, match="endpoint.*changed"):
        lp_ledger_export.export_rpc_lp_ledger(
            "uni-base",
            100,
            102,
            output,
            candidate_tx_hashes=(),
        )

    assert not built_coverage
    assert not published
    assert not output.exists()
    assert not ledger_coverage_path(output).exists()


def test_coverage_header_rejects_a_different_block_height() -> None:
    fake_w3 = SimpleNamespace(
        provider=SimpleNamespace(
            make_request=lambda *_args: {
                "result": {
                    "number": hex(101),
                    "hash": "0x" + "11" * 32,
                    "timestamp": hex(1_700_000_000),
                }
            }
        )
    )

    with pytest.raises(ValueError, match="requested block 100"):
        lp_ledger_export._coverage_block_header(fake_w3, 100)


def _price_event(
    event_type: str,
    block_number: int,
    log_index: int,
    event_order: int,
    sqrt_price_x96: int,
    tick: int,
) -> ReplayedEvent:
    return ReplayedEvent(
        block_number=block_number,
        log_index=log_index,
        event_order=event_order,
        event_type=event_type,
        sqrt_price_x96=None,
        tick=None,
        event_time_sqrt_price_x96=sqrt_price_x96,
        event_time_tick=tick,
        event_time_state_source="same_block_prior_event",
    )


def _build_modify_input(actions: bytes, params: list[bytes], deadline: int = 123) -> str:
    selector = Web3.keccak(text="modifyLiquidities(bytes,uint256)")[:4]
    unlock_data = encode(["bytes", "bytes[]"], [actions, params])
    calldata = selector + encode(["bytes", "uint256"], [unlock_data, deadline])
    return "0x" + calldata.hex()


def _build_multicall_input(calls: list[bytes]) -> str:
    selector = Web3.keccak(text="multicall(bytes[])")[:4]
    calldata = selector + encode(["bytes[]"], [calls])
    return "0x" + calldata.hex()


def _build_mint_param(
    config,
    *,
    tick_lower: int,
    tick_upper: int,
    liquidity_delta: int,
    recipient: str,
    fee: int | None = None,
) -> bytes:
    return encode(
        [
            "(address,address,uint24,int24,address)",
            "int24",
            "int24",
            "uint256",
            "uint128",
            "uint128",
            "address",
            "bytes",
        ],
        [
            (
                config.token0_address,
                config.token1_address,
                int(config.fee_rate * 1_000_000) if fee is None else fee,
                30,
                ZERO_ADDRESS,
            ),
            tick_lower,
            tick_upper,
            liquidity_delta,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )


_ENTRYPOINT_V08 = "0x4337084d9e255ff0702461cf8895ce9e3b5ff108"
_USER_OPERATION_TYPE = (
    "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[]"
)


def _build_wrapped_position_manager_input(
    *,
    sender: str,
    position_manager_calls: list[tuple[str, int, bytes]],
    mode: bytes = b"\x01" + b"\x00" * 31,
    user_operation_count: int = 1,
) -> str:
    execution_data = encode(
        ["(address,uint256,bytes)[]"],
        [position_manager_calls],
    )
    execute_call = (
        Web3.keccak(text="execute(bytes32,bytes)")[:4]
        + encode(["bytes32", "bytes"], [mode, execution_data])
    )
    user_operation = (
        sender,
        7,
        b"",
        execute_call,
        b"\x00" * 32,
        100_000,
        b"\x00" * 32,
        b"",
        b"fixture-signature",
    )
    handle_ops = Web3.keccak(
        text=(
            "handleOps((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[],"
            "address)"
        )
    )[:4]
    calldata = handle_ops + encode(
        [_USER_OPERATION_TYPE, "address"],
        [[user_operation] * user_operation_count, "0x" + "88" * 20],
    )
    return "0x" + calldata.hex()


def _wrapped_base_burn_manifest() -> dict[str, object]:
    path = (
        REPO_ROOT
        / "research"
        / "tests"
        / "fixtures"
        / "v4_lp_wrapped_base_burns.json"
    )
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise AssertionError("wrapped-burn fixture must be a JSON object")
    return payload


def test_wrapped_base_burn_manifest_is_exact_and_explicitly_synthetic() -> None:
    manifest = _wrapped_base_burn_manifest()
    cases = manifest["cases"]
    assert isinstance(cases, list)
    assert manifest["fixture_kind"] == "synthetic_wrapper_representability"
    assert manifest["historical_calldata_captured"] is False
    assert len(cases) == manifest["expected_wrapped_count"] == 43
    assert [case["case_id"] for case in cases] == [
        f"wrapped_burn_{index:03d}" for index in range(1, 44)
    ]
    assert [(case["block_number"], case["log_index"]) for case in cases] == sorted(
        (case["block_number"], case["log_index"]) for case in cases
    )
    counts = {
        str(token_id): sum(case["token_id"] == token_id for case in cases)
        for token_id in (2087350, 2094074, 2128952)
    }
    assert counts == manifest["expected_token_counts"]
    assert all(
        int(case["salt"], 16) == case["token_id"]
        and case["liquidity_delta"] < 0
        for case in cases
    )
    semantic_fields = manifest["semantic_vector_fields"]
    assert isinstance(semantic_fields, list)
    semantic_ndjson = "".join(
        json.dumps(
            {field: case[field] for field in semantic_fields},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        )
        + "\n"
        for case in cases
    ).encode()
    assert hashlib.sha256(semantic_ndjson).hexdigest() == manifest[
        "semantic_vector_sha256"
    ]
    assert manifest["direct_control"] not in cases


def test_all_43_wrapped_base_burn_vectors_are_structurally_representable() -> None:
    config = POOL_CONFIGS["uni-base"]
    manifest = _wrapped_base_burn_manifest()
    cases = manifest["cases"]
    assert isinstance(cases, list)
    user_operation_sender = "0x" + "11" * 20
    bundler = "0x" + "22" * 20

    for index, case in enumerate(cases, start=1):
        token_id = int(case["token_id"])
        burn = encode(
            ["uint256", "uint128", "uint128", "bytes"],
            [token_id, 0, 0, b""],
        )
        take_pair = encode(
            ["address", "address", "address"],
            [config.token0_address, config.token1_address, user_operation_sender],
        )
        modify_call = bytes.fromhex(
            _build_modify_input(
                bytes([_V4_LP_BURN_POSITION, _V4_LP_TAKE_PAIR]),
                [burn, take_pair],
            )[2:]
        )
        position_manager_call = bytes.fromhex(
            _build_multicall_input([modify_call])[2:]
        )
        transaction_hash = "0x" + index.to_bytes(32, "big").hex()
        tx = {
            "to": _ENTRYPOINT_V08,
            "from": bundler,
            "hash": transaction_hash,
            "blockNumber": case["block_number"],
            "input": _build_wrapped_position_manager_input(
                sender=user_operation_sender,
                position_manager_calls=[
                    (config.position_manager, 0, position_manager_call)
                ],
            ),
        }
        receipt = {
            "logs": [
                _modify_liquidity_log(
                    int(case["log_index"]),
                    tick_lower=int(case["tick_lower"]),
                    tick_upper=int(case["tick_upper"]),
                    liquidity_delta=int(case["liquidity_delta"]),
                    salt_token_id=token_id,
                )
            ]
        }
        contexts = position_manager_calls_for_transaction(
            tx,
            config.position_manager,
            _ENTRYPOINT_V08,
        )
        token_state = {
            token_id: LedgerPositionState(
                config.pool_id,
                int(case["tick_lower"]),
                int(case["tick_upper"]),
                -int(case["liquidity_delta"]),
            )
        }
        position_keys = {
            token_id: _position_key(
                config,
                token_id,
                tick_lower=int(case["tick_lower"]),
                tick_upper=int(case["tick_upper"]),
            )
        }

        actions = decode_liquidity_actions_for_tx(
            tx,
            receipt,
            1_700_000_000,
            config,
            token_state,
            position_keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

        assert len(contexts) == 1
        assert contexts[0].wrapper_source == "entrypoint_handle_ops"
        assert contexts[0].effective_sender == Web3.to_checksum_address(
            user_operation_sender
        )
        assert len(actions) == 1
        assert (
            actions[0].token_id,
            actions[0].tick_lower,
            actions[0].tick_upper,
            actions[0].liquidity_delta,
            actions[0].log_index,
        ) == (
            token_id,
            case["tick_lower"],
            case["tick_upper"],
            case["liquidity_delta"],
            case["log_index"],
        )
        assert token_state == {}


def test_wrapped_burn_manifest_direct_control_stays_outside_wrapper_count() -> None:
    config = POOL_CONFIGS["uni-base"]
    manifest = _wrapped_base_burn_manifest()
    control = manifest["direct_control"]
    assert isinstance(control, dict)
    token_id = int(control["token_id"])
    burn = encode(
        ["uint256", "uint128", "uint128", "bytes"],
        [token_id, 0, 0, b""],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "11" * 20,
        "hash": "0x" + "ff" * 32,
        "blockNumber": control["block_number"],
        "input": _build_modify_input(bytes([_V4_LP_BURN_POSITION]), [burn]),
    }
    contexts = position_manager_calls_for_transaction(
        tx,
        config.position_manager,
        _ENTRYPOINT_V08,
    )

    assert len(contexts) == 1
    assert contexts[0].wrapper_source == control["expected_wrapper_source"] == "direct"
    assert control["case_id"] not in {
        case["case_id"] for case in manifest["cases"]
    }


def test_wrapped_action_uses_user_operation_sender_and_exact_pm_multicall() -> None:
    config = POOL_CONFIGS["uni-base"]
    user_operation_sender = "0x" + "11" * 20
    bundler = "0x" + "22" * 20
    account = "0x" + "33" * 20
    pm_multicall = bytes.fromhex(_build_multicall_input([b"\x12\x34"])[2:])
    transaction_hash = "0x" + "44" * 32
    tx = {
        "to": _ENTRYPOINT_V08,
        "from": bundler,
        "hash": transaction_hash,
        "input": _build_wrapped_position_manager_input(
            sender=user_operation_sender,
            position_manager_calls=[
                (account, 0, b"unrelated"),
                (config.position_manager, 0, pm_multicall),
            ],
        ),
    }

    calls = position_manager_calls_for_transaction(
        tx,
        config.position_manager,
        _ENTRYPOINT_V08,
    )

    assert len(calls) == 1
    assert calls[0].input_data == "0x" + pm_multicall.hex()
    assert calls[0].effective_sender == Web3.to_checksum_address(
        user_operation_sender
    )
    assert calls[0].wrapper_source == "entrypoint_handle_ops"
    assert calls[0].user_operation_index == 0
    assert calls[0].batch_index == 1
    assert calls[0].outer_transaction_hash == transaction_hash


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_entrypoint",
        "unsupported_mode",
        "zero_pm_calls",
        "multiple_pm_calls",
        "multiple_user_operations",
        "trailing_bytes",
    ),
)
def test_wrapped_action_rejects_ambiguous_or_noncanonical_shapes(
    mutation: str,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    pm_multicall = bytes.fromhex(_build_multicall_input([b"\x12\x34"])[2:])
    calls = [(config.position_manager, 0, pm_multicall)]
    mode = b"\x01" + b"\x00" * 31
    user_operation_count = 1
    outer_target = _ENTRYPOINT_V08
    trailing = ""
    if mutation == "wrong_entrypoint":
        outer_target = "0x" + "99" * 20
    elif mutation == "unsupported_mode":
        mode = b"\x00" * 32
    elif mutation == "zero_pm_calls":
        calls = [("0x" + "77" * 20, 0, b"unrelated")]
    elif mutation == "multiple_pm_calls":
        calls = calls * 2
    elif mutation == "multiple_user_operations":
        user_operation_count = 2
    else:
        trailing = "00"
    tx = {
        "to": outer_target,
        "from": "0x" + "22" * 20,
        "hash": "0x" + "44" * 32,
        "input": _build_wrapped_position_manager_input(
            sender="0x" + "11" * 20,
            position_manager_calls=calls,
            mode=mode,
            user_operation_count=user_operation_count,
        )
        + trailing,
    }

    with pytest.raises(ValueError):
        position_manager_calls_for_transaction(
            tx,
            config.position_manager,
            _ENTRYPOINT_V08,
        )


def _topic_address(address: str) -> str:
    return "0x" + "00" * 12 + address[2:].lower()


def _transfer_log(
    *,
    address: str,
    from_address: str,
    to_address: str,
    token_or_amount: int,
    log_index: int,
) -> dict[str, object]:
    return {
        "address": address,
        "topics": [
            TRANSFER_TOPIC,
            _topic_address(from_address),
            _topic_address(to_address),
            "0x" + token_or_amount.to_bytes(32, "big").hex(),
        ],
        "data": "0x",
        "logIndex": log_index,
    }


def _erc20_transfer_log(
    *,
    token: str,
    from_address: str,
    to_address: str,
    amount_raw: int,
    log_index: int,
) -> dict[str, object]:
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            _topic_address(from_address),
            _topic_address(to_address),
        ],
        "data": hex(amount_raw),
        "logIndex": log_index,
    }


def _position_key(
    config,
    token_id: int,
    *,
    tick_lower: int = -120,
    tick_upper: int = 120,
) -> PoolPositionKey:
    return PoolPositionKey(
        pool_id=config.pool_id,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        salt="0x" + token_id.to_bytes(32, "big").hex(),
    )


def _modify_liquidity_log(
    log_index: int,
    *,
    config=None,
    pool_id: str | None = None,
    sender: str | None = None,
    tick_lower: int = -120,
    tick_upper: int = 120,
    liquidity_delta: int = 999,
    salt_token_id: int = 77,
) -> dict[str, object]:
    config = POOL_CONFIGS["uni-base"] if config is None else config
    salt = salt_token_id.to_bytes(32, "big")
    return {
        "address": config.pool_manager,
        "topics": [
            V4_MODIFY_LIQUIDITY_TOPIC,
            config.pool_id if pool_id is None else pool_id,
            _topic_address(config.position_manager if sender is None else sender),
        ],
        "data": "0x"
        + encode(
            ["int24", "int24", "int256", "bytes32"],
            [tick_lower, tick_upper, liquidity_delta, salt],
        ).hex(),
        "logIndex": log_index,
    }


def test_multi_mint_pairs_distinct_token_transfers_and_position_keys() -> None:
    config = POOL_CONFIGS["uni-base"]
    sender = "0x" + "bb" * 20
    recipient = "0x" + "aa" * 20
    mint0 = _build_mint_param(
        config,
        tick_lower=-120,
        tick_upper=120,
        liquidity_delta=999,
        recipient=recipient,
    )
    mint1 = _build_mint_param(
        config,
        tick_lower=-240,
        tick_upper=240,
        liquidity_delta=555,
        recipient=recipient,
    )
    tx = {
        "to": config.position_manager,
        "from": sender,
        "hash": "0x" + "31" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION, _V4_LP_MINT_POSITION]),
            [mint0, mint1],
        ),
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=101,
                log_index=5,
            ),
            _modify_liquidity_log(
                8,
                liquidity_delta=999,
                salt_token_id=101,
            ),
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=202,
                log_index=9,
            ),
            _modify_liquidity_log(
                12,
                tick_lower=-240,
                tick_upper=240,
                liquidity_delta=555,
                salt_token_id=202,
            ),
        ]
    }
    token_state: dict[int, LedgerPositionState] = {}
    position_keys: dict[int, PoolPositionKey] = {}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert [(action.token_id, action.log_index) for action in actions] == [
        (101, 8),
        (202, 12),
    ]
    assert token_state == {
        101: LedgerPositionState(config.pool_id, -120, 120, 999),
        202: LedgerPositionState(config.pool_id, -240, 240, 555),
    }
    assert position_keys == {
        101: _position_key(config, 101),
        202: _position_key(config, 202, tick_lower=-240, tick_upper=240),
    }


def test_mixed_pool_mints_preserve_global_transfer_pairing_order() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    foreign_mint = _build_mint_param(
        config,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=111,
        recipient=recipient,
        fee=3_000,
    )
    target_mint = _build_mint_param(
        config,
        tick_lower=-120,
        tick_upper=120,
        liquidity_delta=999,
        recipient=recipient,
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "46" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION, _V4_LP_MINT_POSITION]),
            [foreign_mint, target_mint],
        ),
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=101,
                log_index=5,
            ),
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=202,
                log_index=9,
            ),
            _modify_liquidity_log(12, salt_token_id=202),
        ]
    }
    token_state: dict[int, LedgerPositionState] = {}
    position_keys: dict[int, PoolPositionKey] = {}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert [(action.token_id, action.log_index) for action in actions] == [(202, 12)]
    assert token_state == {
        202: LedgerPositionState(config.pool_id, -120, 120, 999)
    }
    assert position_keys == {202: _position_key(config, 202)}


def test_mint_recipient_must_match_creation_transfer() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "47" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION]),
            [
                _build_mint_param(
                    config,
                    tick_lower=-120,
                    tick_upper=120,
                    liquidity_delta=999,
                    recipient=recipient,
                )
            ],
        ),
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address="0x" + "cc" * 20,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(8),
        ]
    }

    with pytest.raises(ValueError, match="recipient"):
        decode_liquidity_actions_for_tx(
            tx,
            receipt,
            1_700_000_000,
            config,
            {},
            {},
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )


def test_mint_resolves_address_this_recipient_to_position_manager() -> None:
    config = POOL_CONFIGS["uni-base"]
    address_this = "0x0000000000000000000000000000000000000002"
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "48" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION]),
            [
                _build_mint_param(
                    config,
                    tick_lower=-120,
                    tick_upper=120,
                    liquidity_delta=999,
                    recipient=address_this,
                )
            ],
        ),
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=config.position_manager,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(8, salt_token_id=77),
        ]
    }
    token_state: dict[int, LedgerPositionState] = {}
    position_keys: dict[int, PoolPositionKey] = {}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert [(action.token_id, action.log_index) for action in actions] == [(77, 8)]
    assert token_state == {
        77: LedgerPositionState(config.pool_id, -120, 120, 999)
    }
    assert position_keys == {77: _position_key(config, 77)}


@pytest.mark.parametrize(
    "witness_change",
    ("pool", "sender", "ticks", "salt", "delta"),
)
def test_action_decode_rejects_semantic_witness_mismatch_transactionally(
    witness_change: str,
) -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "32" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION]),
            [
                _build_mint_param(
                    config,
                    tick_lower=-120,
                    tick_upper=120,
                    liquidity_delta=999,
                    recipient=recipient,
                )
            ],
        ),
    }
    log_kwargs: dict[str, object] = {}
    if witness_change == "pool":
        log_kwargs["pool_id"] = "0x" + "ab" * 32
    elif witness_change == "sender":
        log_kwargs["sender"] = "0x" + "cd" * 20
    elif witness_change == "ticks":
        log_kwargs["tick_lower"] = -121
    elif witness_change == "salt":
        log_kwargs["salt_token_id"] = 78
    else:
        log_kwargs["liquidity_delta"] = 998
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(8, **log_kwargs),
        ]
    }
    original_state = {
        9: LedgerPositionState(config.pool_id, -30, 30, 10),
    }
    original_keys = {
        9: _position_key(config, 9, tick_lower=-30, tick_upper=30),
    }
    token_state = dict(original_state)
    position_keys = dict(original_keys)

    with pytest.raises(ValueError, match="ModifyLiquidity"):
        decode_liquidity_actions_for_tx(
            tx,
            receipt,
            1_700_000_000,
            config,
            token_state,
            position_keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert token_state == original_state
    assert position_keys == original_keys


def test_action_decode_rejects_negative_liquidity_without_mutating_state() -> None:
    config = POOL_CONFIGS["uni-base"]
    sender = "0x" + "bb" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 1_001, 0, 0, b""],
    )
    tx = {
        "to": config.position_manager,
        "from": sender,
        "hash": "0x" + "33" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY]),
            [decrease],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-1_001,
                salt_token_id=55,
            )
        ]
    }
    original_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    original_keys = {55: _position_key(config, 55)}
    token_state = dict(original_state)
    position_keys = dict(original_keys)

    with pytest.raises(ValueError, match="below zero"):
        decode_liquidity_actions_for_tx(
            tx,
            receipt,
            1_700_000_000,
            config,
            token_state,
            position_keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert token_state == original_state
    assert position_keys == original_keys


def test_burn_position_removes_remaining_liquidity_and_collects() -> None:
    config = POOL_CONFIGS["uni-base"]
    sender = "0x" + "bb" * 20
    recipient = "0x" + "aa" * 20
    burn = encode(
        ["uint256", "uint128", "uint128", "bytes"],
        [55, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": sender,
        "hash": "0x" + "41" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_BURN_POSITION, _V4_LP_TAKE_PAIR]),
            [burn, take_pair],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-1_000,
                salt_token_id=55,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address="0x" + "11" * 20,
                to_address=recipient,
                amount_raw=9_000_000,
                log_index=9,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=config.pool_manager,
                to_address=recipient,
                amount_raw=2_500_000,
                log_index=10,
            ),
        ]
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    position_keys = {55: _position_key(config, 55)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].liquidity_delta == -1_000
    assert actions[0].log_index == 8
    assert actions[0].collect_amount0 == Decimal("2.5")


def test_increase_does_not_discard_pending_take_attribution() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    increase = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [66, 100, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "46" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes(
                [
                    _V4_LP_DECREASE_LIQUIDITY,
                    _V4_LP_INCREASE_LIQUIDITY,
                    _V4_LP_TAKE_PAIR,
                ]
            ),
            [decrease, increase, take_pair],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-400,
                salt_token_id=55,
            ),
            _modify_liquidity_log(
                9,
                tick_lower=-240,
                tick_upper=240,
                liquidity_delta=100,
                salt_token_id=66,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=config.pool_manager,
                to_address=recipient,
                amount_raw=2_500_000,
                log_index=10,
            ),
        ]
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
        66: LedgerPositionState(config.pool_id, -240, 240, 900),
    }
    position_keys = {
        55: _position_key(config, 55),
        66: _position_key(config, 66, tick_lower=-240, tick_upper=240),
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert [action.action_type for action in actions] == ["burn_collect", "mint"]
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert actions[1].amount_attribution_status == "ambiguous_missing_settle_pair"
    assert token_state == {
        55: LedgerPositionState(config.pool_id, -120, 120, 600),
        66: LedgerPositionState(config.pool_id, -240, 240, 1_000),
    }
    assert position_keys == {
        55: _position_key(config, 55),
        66: _position_key(config, 66, tick_lower=-240, tick_upper=240),
    }


def test_burn_position_with_zero_liquidity_emits_no_modify_action() -> None:
    config = POOL_CONFIGS["uni-base"]
    burn = encode(
        ["uint256", "uint128", "uint128", "bytes"],
        [55, 0, 0, b""],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "42" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(bytes([_V4_LP_BURN_POSITION]), [burn]),
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 0),
    }
    position_keys = {55: _position_key(config, 55)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        {"logs": []},
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert actions == []
    assert token_state == {}
    assert position_keys == {55: _position_key(config, 55)}


def test_full_decrease_then_burn_emits_only_the_decrease_modify_action() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 1_000, 0, 0, b""],
    )
    burn = encode(
        ["uint256", "uint128", "uint128", "bytes"],
        [55, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "43" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes(
                [
                    _V4_LP_DECREASE_LIQUIDITY,
                    _V4_LP_BURN_POSITION,
                    _V4_LP_TAKE_PAIR,
                ]
            ),
            [decrease, burn, take_pair],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-1_000,
                salt_token_id=55,
            )
        ]
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    position_keys = {55: _position_key(config, 55)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert [(action.action_type, action.liquidity_delta) for action in actions] == [
        ("burn_collect", -1_000)
    ]
    assert token_state == {}


def test_multiple_target_take_pairs_fail_closed() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "44" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes(
                [
                    _V4_LP_DECREASE_LIQUIDITY,
                    _V4_LP_TAKE_PAIR,
                    _V4_LP_TAKE_PAIR,
                ]
            ),
            [decrease, take_pair, take_pair],
        ),
    }
    state = {55: LedgerPositionState(config.pool_id, -120, 120, 1_000)}
    keys = {55: _position_key(config, 55)}

    with pytest.raises(ValueError, match="multiple target-pool TAKE_PAIR"):
        decode_liquidity_actions_for_tx(
            tx,
            {
                "logs": [
                    _modify_liquidity_log(
                        8,
                        liquidity_delta=-400,
                        salt_token_id=55,
                    )
                ]
            },
            1_700_000_000,
            config,
            state,
            keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert state[55].liquidity_after == 1_000


def test_overlapping_non_target_take_pair_fails_closed() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    foreign_token = "0x" + "cc" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    target_take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    overlapping_take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, foreign_token, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "50" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes(
                [
                    _V4_LP_DECREASE_LIQUIDITY,
                    _V4_LP_TAKE_PAIR,
                    _V4_LP_TAKE_PAIR,
                ]
            ),
            [decrease, target_take_pair, overlapping_take_pair],
        ),
    }
    state = {55: LedgerPositionState(config.pool_id, -120, 120, 1_000)}
    keys = {55: _position_key(config, 55)}

    with pytest.raises(ValueError, match="TAKE_PAIR receipt attribution is ambiguous"):
        decode_liquidity_actions_for_tx(
            tx,
            {
                "logs": [
                    _modify_liquidity_log(
                        8,
                        liquidity_delta=-400,
                        salt_token_id=55,
                    ),
                    _erc20_transfer_log(
                        token=config.token0_address,
                        from_address=config.pool_manager,
                        to_address=recipient,
                        amount_raw=2_500_000,
                        log_index=9,
                    ),
                    _erc20_transfer_log(
                        token=config.token0_address,
                        from_address=config.pool_manager,
                        to_address=recipient,
                        amount_raw=9_000_000,
                        log_index=10,
                    ),
                ]
            },
            1_700_000_000,
            config,
            state,
            keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert state[55].liquidity_after == 1_000


def test_duplicate_pool_manager_transfers_for_one_take_pair_fail_closed() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "53" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease, take_pair],
        ),
    }
    original_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    state = dict(original_state)

    with pytest.raises(ValueError, match="TAKE_PAIR receipt attribution is ambiguous"):
        decode_liquidity_actions_for_tx(
            tx,
            {
                "logs": [
                    _modify_liquidity_log(
                        8,
                        liquidity_delta=-400,
                        salt_token_id=55,
                    ),
                    _erc20_transfer_log(
                        token=config.token0_address,
                        from_address=config.pool_manager,
                        to_address=recipient,
                        amount_raw=2_000_000,
                        log_index=9,
                    ),
                    _erc20_transfer_log(
                        token=config.token0_address,
                        from_address=config.pool_manager,
                        to_address=recipient,
                        amount_raw=500_000,
                        log_index=10,
                    ),
                ]
            },
            1_700_000_000,
            config,
            state,
            {55: _position_key(config, 55)},
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert state == original_state


def test_multiple_unresolved_decreases_before_take_fail_closed() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    decrease0 = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    decrease1 = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [66, 300, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "45" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes(
                [
                    _V4_LP_DECREASE_LIQUIDITY,
                    _V4_LP_DECREASE_LIQUIDITY,
                    _V4_LP_TAKE_PAIR,
                ]
            ),
            [decrease0, decrease1, take_pair],
        ),
    }
    state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
        66: LedgerPositionState(config.pool_id, -240, 240, 900),
    }
    keys = {
        55: _position_key(config, 55),
        66: _position_key(config, 66, tick_lower=-240, tick_upper=240),
    }

    with pytest.raises(ValueError, match="multiple unresolved target-pool"):
        decode_liquidity_actions_for_tx(
            tx,
            {
                "logs": [
                    _modify_liquidity_log(
                        8,
                        liquidity_delta=-400,
                        salt_token_id=55,
                    ),
                    _modify_liquidity_log(
                        9,
                        tick_lower=-240,
                        tick_upper=240,
                        liquidity_delta=-300,
                        salt_token_id=66,
                    ),
                ]
            },
            1_700_000_000,
            config,
            state,
            keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert state == {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
        66: LedgerPositionState(config.pool_id, -240, 240, 900),
    }


def test_reversed_take_pair_currency_order_is_supported() -> None:
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x" + "aa" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token1_address, config.token0_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "51" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease, take_pair],
        ),
    }
    state = {55: LedgerPositionState(config.pool_id, -120, 120, 1_000)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        {
            "logs": [
                _modify_liquidity_log(
                    8,
                    liquidity_delta=-400,
                    salt_token_id=55,
                ),
                _erc20_transfer_log(
                    token=config.token0_address,
                    from_address=config.pool_manager,
                    to_address=recipient,
                    amount_raw=2_500_000,
                    log_index=9,
                ),
                _erc20_transfer_log(
                    token=config.token1_address,
                    from_address=config.pool_manager,
                    to_address=recipient,
                    amount_raw=1_250_000,
                    log_index=10,
                ),
            ]
        },
        1_700_000_000,
        config,
        state,
        {55: _position_key(config, 55)},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert actions[0].collect_amount1 == Decimal("1.25")


def test_unresolved_target_withdrawal_fails_closed() -> None:
    config = POOL_CONFIGS["uni-base"]
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    unsupported_take = encode(
        ["address", "address", "uint256"],
        [config.token0_address, "0x" + "aa" * 20, 2_500_000],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "52" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, 0x0E]),
            [decrease, unsupported_take],
        ),
    }
    original_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    state = dict(original_state)
    original_keys = {55: _position_key(config, 55)}
    keys = dict(original_keys)

    with pytest.raises(ValueError, match="no supported TAKE_PAIR attribution"):
        decode_liquidity_actions_for_tx(
            tx,
            {
                "logs": [
                    _modify_liquidity_log(
                        8,
                        liquidity_delta=-400,
                        salt_token_id=55,
                    )
                ]
            },
            1_700_000_000,
            config,
            state,
            keys,
            wrapper_entrypoint=_ENTRYPOINT_V08,
        )

    assert state == original_state
    assert keys == original_keys


def test_zero_delta_decrease_take_pair_is_one_collect_modify_action() -> None:
    config = POOL_CONFIGS["uni-base"]
    sender = "0x" + "bb" * 20
    recipient = "0x" + "aa" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 0, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, recipient],
    )
    tx = {
        "to": config.position_manager,
        "from": sender,
        "hash": "0x" + "34" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease, take_pair],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=0,
                salt_token_id=55,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=config.pool_manager,
                to_address=recipient,
                amount_raw=2_500_000,
                log_index=9,
            ),
        ]
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    position_keys = {55: _position_key(config, 55)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "collect"
    assert actions[0].liquidity_delta == 0
    assert actions[0].log_index == 8
    assert actions[0].collect_amount0 == Decimal("2.5")


def test_wrapped_decode_uses_user_operation_sender_for_msg_sender_collect() -> None:
    config = POOL_CONFIGS["uni-base"]
    user_operation_sender = "0x" + "11" * 20
    bundler = "0x" + "22" * 20
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, ACTION_RECIPIENT_MSG_SENDER],
    )
    modify_call = bytes.fromhex(
        _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease, take_pair],
        )[2:]
    )
    position_manager_call = bytes.fromhex(_build_multicall_input([modify_call])[2:])
    tx = {
        "to": _ENTRYPOINT_V08,
        "from": bundler,
        "hash": "0x" + "35" * 32,
        "blockNumber": 100,
        "input": _build_wrapped_position_manager_input(
            sender=user_operation_sender,
            position_manager_calls=[(config.position_manager, 0, position_manager_call)],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-400,
                salt_token_id=55,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=config.pool_manager,
                to_address=user_operation_sender,
                amount_raw=2_500_000,
                log_index=9,
            ),
        ]
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    position_keys = {55: _position_key(config, 55)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert token_state[55].liquidity_after == 600


def test_take_pair_resolves_address_this_recipient_to_position_manager() -> None:
    config = POOL_CONFIGS["uni-base"]
    address_this = "0x0000000000000000000000000000000000000002"
    decrease = encode(
        ["uint256", "uint256", "uint128", "uint128", "bytes"],
        [55, 400, 0, 0, b""],
    )
    take_pair = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, address_this],
    )
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "hash": "0x" + "49" * 32,
        "blockNumber": 100,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease, take_pair],
        ),
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-400,
                salt_token_id=55,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=config.pool_manager,
                to_address=config.position_manager,
                amount_raw=2_500_000,
                log_index=9,
            ),
        ]
    }
    token_state = {
        55: LedgerPositionState(config.pool_id, -120, 120, 1_000),
    }
    position_keys = {55: _position_key(config, 55)}

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        position_keys,
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert token_state[55].liquidity_after == 600


def test_collect_row_carries_token_id_owner_range_and_event_price():
    rows = build_lp_ledger_rows(
        decoded_actions=[
            DecodedLiquidityAction(
                "mint",
                100,
                10,
                0,
                42,
                "0xowner",
                -120,
                120,
                1000,
                0,
                1,
                2,
                chain="base",
                amount0_actual=0,
                amount1_actual=1,
                amount0_attribution_source="fixture",
                amount1_attribution_source="fixture",
                amount_attribution_status="fixture_exact",
            ),
            DecodedLiquidityAction(
                "collect",
                100,
                20,
                1,
                42,
                "0xowner",
                -120,
                120,
                0,
                amount0=0,
                amount1=0,
                collect_amount0=3,
                collect_amount1=4,
                chain="base",
                amount0_actual=0,
                amount1_actual=0,
                amount0_attribution_source="not_applicable",
                amount1_attribution_source="not_applicable",
                amount_attribution_status="not_applicable",
            ),
        ],
        ownership_events=[OwnershipEvent(100, 9, 42, None, "0xowner")],
        price_events=[
            _price_event("mint", 100, 10, 0, 111, 1),
            _price_event("collect", 100, 20, 1, 222, 2),
        ],
    )

    assert rows[1].token_id == 42
    assert rows[1].lp_owner == "0xowner"
    assert rows[1].owner_source == "action"
    assert rows[1].tick_lower == -120
    assert rows[1].tick_upper == 120
    assert rows[1].collect_amount0 == Decimal("3")
    assert rows[1].collect_amount1 == Decimal("4")
    assert rows[1].sqrt_price_x96_at_event == 222
    assert rows[1].tick_at_event == 2


def test_owner_lookup_uses_latest_transfer_at_or_before_action():
    rows = build_lp_ledger_rows(
        decoded_actions=[
            DecodedLiquidityAction(
                "mint",
                100,
                10,
                0,
                42,
                None,
                -120,
                120,
                1000,
                0,
                1,
                2,
                chain="base",
                amount0_actual=0,
                amount1_actual=1,
                amount0_attribution_source="fixture",
                amount1_attribution_source="fixture",
                amount_attribution_status="fixture_exact",
            ),
            DecodedLiquidityAction(
                "burn",
                100,
                30,
                0,
                42,
                None,
                -120,
                120,
                -400,
                0,
                0,
                0,
                chain="base",
                amount0_actual=0,
                amount1_actual=0,
                amount0_attribution_source="not_applicable",
                amount1_attribution_source="not_applicable",
                amount_attribution_status="not_applicable",
            ),
        ],
        ownership_events=[
            OwnershipEvent(100, 9, 42, None, "0xfirst"),
            OwnershipEvent(100, 25, 42, "0xfirst", "0xsecond"),
        ],
        price_events=[
            _price_event("mint", 100, 10, 0, 111, 1),
            _price_event("burn", 100, 30, 0, 222, 2),
        ],
    )

    assert rows[0].lp_owner == "0xfirst"
    assert rows[0].owner_source == "transfer"
    assert rows[0].liquidity_after == 1000
    assert rows[1].lp_owner == "0xsecond"
    assert rows[1].owner_source == "transfer"
    assert rows[1].liquidity_after == 600


def test_decode_ownership_events_from_position_manager_transfers():
    receipt = {
        "blockNumber": 100,
        "logs": [
            _transfer_log(
                address=POOL_CONFIGS["uni-base"].position_manager,
                from_address=ZERO_ADDRESS,
                to_address="0x00000000000000000000000000000000000000AA",
                token_or_amount=42,
                log_index=7,
            ),
            _transfer_log(
                address=POOL_CONFIGS["uni-base"].token0_address,
                from_address=ZERO_ADDRESS,
                to_address="0x00000000000000000000000000000000000000AA",
                token_or_amount=999,
                log_index=8,
            ),
        ],
    }

    events = decode_ownership_events_from_receipt(
        receipt,
        POOL_CONFIGS["uni-base"].position_manager,
    )

    assert events == [
        OwnershipEvent(
            block_number=100,
            log_index=7,
            token_id=42,
            previous_owner=None,
            new_owner=Web3.to_checksum_address("0x00000000000000000000000000000000000000AA"),
        )
    ]


def test_decode_mint_action_uses_minted_token_transfer_and_pool_modify_log():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    sender = "0x00000000000000000000000000000000000000BB"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    tx = {
        "to": config.position_manager,
        "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
        "hash": "0x" + "11" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=700_000,
                log_index=6,
            ),
            _erc20_transfer_log(
                token=config.token1_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=1_500_000,
                log_index=7,
            ),
            _modify_liquidity_log(8),
        ],
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        {},
        {},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "mint"
    assert actions[0].token_id == 77
    assert actions[0].tick_lower == -120
    assert actions[0].tick_upper == 120
    assert actions[0].liquidity_delta == 999
    assert actions[0].amount0 == Decimal("0.7")
    assert actions[0].amount1 == Decimal("1.5")
    assert actions[0].amount0_actual == Decimal("0.7")
    assert actions[0].amount1_actual == Decimal("1.5")
    assert actions[0].amount0_raw == "700000"
    assert actions[0].amount1_raw == "1500000"
    assert actions[0].amount0_attribution_source == "erc20_transfer_to_settlement"
    assert actions[0].amount1_attribution_source == "erc20_transfer_to_settlement"
    assert actions[0].amount_attribution_status == "exact"
    assert actions[0].log_index == 8
    assert actions[0].timestamp_ms == 1_700_000_000_000


def test_decode_mint_action_inside_position_manager_multicall():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    sender = "0x00000000000000000000000000000000000000BB"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    modify_input = bytes.fromhex(
        _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param])[2:]
    )
    unrelated_call = bytes.fromhex("002a3e3a") + encode(["address"], [recipient])
    tx = {
        "to": config.position_manager,
        "input": _build_multicall_input([unrelated_call, modify_input]),
        "hash": "0x" + "16" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=700_000,
                log_index=6,
            ),
            _modify_liquidity_log(8),
        ],
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        {},
        {},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "mint"
    assert actions[0].token_id == 77
    assert actions[0].amount0 == Decimal("0.7")
    assert actions[0].amount_attribution_status == "exact"


def test_decode_multiple_add_actions_marks_opening_amounts_ambiguous():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    sender = "0x00000000000000000000000000000000000000BB"
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    increase_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [77, 111, 500_000, 1_000_000, b""])
    tx = {
        "to": config.position_manager,
        "input": _build_modify_input(
            bytes([
                _V4_LP_MINT_POSITION,
                _V4_LP_SETTLE_PAIR,
                _V4_LP_INCREASE_LIQUIDITY,
                _V4_LP_SETTLE_PAIR,
            ]),
            [mint_param, settle_param, increase_param, settle_param],
        ),
        "hash": "0x" + "14" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=1_200_000,
                log_index=6,
            ),
            _erc20_transfer_log(
                token=config.token1_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=2_500_000,
                log_index=7,
            ),
            _modify_liquidity_log(8),
            _modify_liquidity_log(
                12,
                liquidity_delta=111,
                salt_token_id=77,
            ),
        ],
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        {},
        {},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert [action.action_type for action in actions] == ["mint", "mint"]
    assert [action.amount_attribution_status for action in actions] == [
        "ambiguous_multiple_add_actions",
        "ambiguous_multiple_add_actions",
    ]
    assert [(action.amount0, action.amount1) for action in actions] == [
        (Decimal("0"), Decimal("0")),
        (Decimal("0"), Decimal("0")),
    ]
    assert [action.amount0_attribution_source for action in actions] == ["ambiguous", "ambiguous"]


def test_settle_pair_must_immediately_follow_add_action_for_exact_attribution():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    sender = "0x00000000000000000000000000000000000000BB"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    tx = {
        "to": config.position_manager,
        "input": _build_modify_input(
            bytes([_V4_LP_MINT_POSITION, 0x99, _V4_LP_SETTLE_PAIR]),
            [mint_param, b"", settle_param],
        ),
        "hash": "0x" + "15" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=700_000,
                log_index=6,
            ),
            _modify_liquidity_log(8),
        ],
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        {},
        {},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].amount_attribution_status == "ambiguous_missing_settle_pair"
    assert actions[0].amount0 == Decimal("0")


def test_decode_increase_with_single_currency_settle_actions_uses_exact_transfers():
    config = POOL_CONFIGS["uni-base"]
    sender = "0x00000000000000000000000000000000000000BB"
    increase_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 400, 1_000_000, 2_000_000, b""])
    settle0_param = encode(["address"], [config.token0_address])
    settle1_param = encode(["address"], [config.token1_address])
    tx = {
        "to": config.position_manager,
        "input": _build_modify_input(bytes([_V4_LP_INCREASE_LIQUIDITY, 18, 18]), [increase_param, settle0_param, settle1_param]),
        "hash": "0x" + "17" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=400,
                salt_token_id=55,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=700_000,
                log_index=9,
            ),
            _erc20_transfer_log(
                token=config.token1_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=1_500_000,
                log_index=10,
            ),
        ],
    }
    token_state = {
        55: LedgerPositionState(
            pool_id=config.pool_id,
            tick_lower=-120,
            tick_upper=120,
            liquidity_after=1_000,
        )
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        {55: _position_key(config, 55)},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].amount_attribution_status == "exact"
    assert actions[0].amount0 == Decimal("0.7")
    assert actions[0].amount1 == Decimal("1.5")


def test_decode_decrease_take_pair_combines_burn_and_collect_amounts():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    decrease_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 400, 0, 0, b""])
    take_pair_param = encode(["address", "address", "address"], [config.token0_address, config.token1_address, recipient])
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "input": _build_modify_input(bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]), [decrease_param, take_pair_param]),
        "hash": "0x" + "22" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-400,
                salt_token_id=55,
            ),
            _erc20_transfer_log(token=config.token0_address, from_address=config.pool_manager, to_address=recipient, amount_raw=2_500_000, log_index=9),
            _erc20_transfer_log(token=config.token1_address, from_address=config.pool_manager, to_address=recipient, amount_raw=1_250_000, log_index=10),
        ],
    }
    token_state = {
        55: LedgerPositionState(
            pool_id=config.pool_id,
            tick_lower=-120,
            tick_upper=120,
            liquidity_after=1_000,
        )
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        {55: _position_key(config, 55)},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].token_id == 55
    assert actions[0].tick_lower == -120
    assert actions[0].tick_upper == 120
    assert actions[0].liquidity_delta == -400
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert actions[0].collect_amount1 == Decimal("1.25")
    assert actions[0].log_index == 8
    assert token_state[55].liquidity_after == 600


def test_decode_decrease_take_pair_resolves_msg_sender_recipient():
    config = POOL_CONFIGS["uni-base"]
    sender = "0x00000000000000000000000000000000000000BB"
    action_recipient = "0x0000000000000000000000000000000000000001"
    decrease_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 400, 0, 0, b""])
    take_pair_param = encode(
        ["address", "address", "address"],
        [config.token0_address, config.token1_address, action_recipient],
    )
    tx = {
        "to": config.position_manager,
        "input": _build_modify_input(
            bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]),
            [decrease_param, take_pair_param],
        ),
        "hash": "0x" + "25" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-400,
                salt_token_id=55,
            ),
            _erc20_transfer_log(token=config.token0_address, from_address=config.pool_manager, to_address=sender, amount_raw=2_500_000, log_index=9),
            _erc20_transfer_log(token=config.token1_address, from_address=config.pool_manager, to_address=sender, amount_raw=1_250_000, log_index=10),
        ],
    }
    token_state = {
        55: LedgerPositionState(
            pool_id=config.pool_id,
            tick_lower=-120,
            tick_upper=120,
            liquidity_after=1_000,
        )
    }

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        token_state,
        {55: _position_key(config, 55)},
        wrapper_entrypoint=_ENTRYPOINT_V08,
    )

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert actions[0].collect_amount1 == Decimal("1.25")


def test_decode_decrease_resolves_preexisting_position_before_action_block():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    decrease_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 400, 0, 0, b""])
    take_pair_param = encode(["address", "address", "address"], [config.token0_address, config.token1_address, recipient])
    tx = {
        "to": config.position_manager,
        "from": "0x" + "bb" * 20,
        "input": _build_modify_input(bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]), [decrease_param, take_pair_param]),
        "hash": "0x" + "23" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(
                8,
                liquidity_delta=-400,
                salt_token_id=55,
            ),
            _erc20_transfer_log(token=config.token0_address, from_address=config.pool_manager, to_address=recipient, amount_raw=2_500_000, log_index=9),
        ],
    }
    calls = []

    def resolver(token_id: int, block_number: int) -> LedgerPositionState | None:
        calls.append((token_id, block_number))
        return LedgerPositionState(config.pool_id, -120, 120, 1_000)

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        {},
        {},
        wrapper_entrypoint=_ENTRYPOINT_V08,
        position_resolver=resolver,
    )

    assert calls == [(55, 99)]
    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].liquidity_delta == -400


def test_export_rpc_lp_ledger_writes_rows_from_rpc_inputs(tmp_path, monkeypatch):
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    sender = "0x00000000000000000000000000000000000000BB"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    tx_hash = "0x" + "24" * 32
    tx = {
        "to": config.position_manager,
        "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
        "hash": tx_hash,
        "blockNumber": 100,
        "transactionIndex": 0,
        "from": sender,
    }
    receipt = {
        "transactionHash": tx_hash,
        "blockNumber": 100,
        "transactionIndex": 0,
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _erc20_transfer_log(
                token=config.token0_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=700_000,
                log_index=6,
            ),
            _erc20_transfer_log(
                token=config.token1_address,
                from_address=sender,
                to_address=config.pool_manager,
                amount_raw=1_500_000,
                log_index=7,
            ),
            _modify_liquidity_log(8),
        ],
    }
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda requested_hash: tx,
            get_transaction_receipt=lambda requested_hash: receipt,
        )
    )
    output = tmp_path / "ledger.csv"

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(lp_ledger_export, "_block_timestamp_for_number", lambda *_args: 1_700_000_000)
    monkeypatch.setattr(
        lp_ledger_export,
        "_coverage_block_header",
        lambda _w3, block_number: (
            f"0x{block_number:064x}",
            1_700_000_000_000 + block_number,
        ),
    )
    monkeypatch.setattr(lp_ledger_export, "_coverage_chain_id", lambda *_args: 8453)
    monkeypatch.setattr(
        lp_ledger_export,
        "_replayed_price_events_for_actions",
        lambda *_args: [_price_event("mint", 100, 8, 0, 2**96, 0)],
    )

    count = lp_ledger_export.export_rpc_lp_ledger(
        "uni-base",
        100,
        100,
        output,
        candidate_tx_hashes=(tx_hash,),
    )

    rows = list(csv.DictReader(output.open()))
    assert count == 1
    assert rows[0]["token_id"] == "77"
    assert rows[0]["lp_owner"] == Web3.to_checksum_address(recipient)
    assert rows[0]["event_type"] == "mint"
    assert rows[0]["amount0"] == "0.7"
    assert rows[0]["amount1"] == "1.5"
    assert rows[0]["amount_attribution_status"] == "exact"
    coverage = json.loads(ledger_coverage_path(output).read_text())
    assert coverage["verification_mode"] == "candidate_list_unverified"
    assert coverage["covered_start_block"] == 100
    assert coverage["covered_end_block"] == 100
    assert coverage["ledger_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()

def test_export_rpc_lp_ledger_decodes_candidate_transactions_chronologically(tmp_path, monkeypatch):
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    next_owner = "0x00000000000000000000000000000000000000BB"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    increase_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [77, 111, 500_000, 1_000_000, b""])
    mint_hash = "0x" + "34" * 32
    transfer_hash = "0x" + "23" * 32
    increase_hash = "0x" + "12" * 32
    txs = {
        mint_hash: {
            "to": config.position_manager,
            "from": recipient,
            "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
            "hash": mint_hash,
            "blockNumber": 100,
            "transactionIndex": 5,
        },
        increase_hash: {
            "to": config.position_manager,
            "from": next_owner,
            "input": _build_modify_input(bytes([_V4_LP_INCREASE_LIQUIDITY, _V4_LP_SETTLE_PAIR]), [increase_param, settle_param]),
            "hash": increase_hash,
            "blockNumber": 100,
            "transactionIndex": 7,
        },
        transfer_hash: {
            "to": config.position_manager,
            "from": recipient,
            "input": "0xdeadbeef",
            "hash": transfer_hash,
            "blockNumber": 100,
            "transactionIndex": 6,
        },
    }
    receipts = {
        mint_hash: {
            "transactionHash": mint_hash,
            "blockNumber": 100,
            "transactionIndex": 5,
            "logs": [
                _transfer_log(
                    address=config.position_manager,
                    from_address=ZERO_ADDRESS,
                    to_address=recipient,
                    token_or_amount=77,
                    log_index=5,
                ),
                _modify_liquidity_log(8),
            ],
        },
        increase_hash: {
            "transactionHash": increase_hash,
            "blockNumber": 100,
            "transactionIndex": 7,
            "logs": [
                _modify_liquidity_log(
                    12,
                    liquidity_delta=111,
                    salt_token_id=77,
                )
            ],
        },
        transfer_hash: {
            "transactionHash": transfer_hash,
            "blockNumber": 100,
            "transactionIndex": 6,
            "logs": [
                _transfer_log(
                    address=config.position_manager,
                    from_address=recipient,
                    to_address=next_owner,
                    token_or_amount=77,
                    log_index=10,
                )
            ],
        },
    }
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda requested_hash: txs[requested_hash],
            get_transaction_receipt=lambda requested_hash: receipts[requested_hash],
        )
    )
    output = tmp_path / "ledger.csv"

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(lp_ledger_export, "_block_timestamp_for_number", lambda *_args: 1_700_000_000)
    monkeypatch.setattr(
        lp_ledger_export,
        "_coverage_block_header",
        lambda _w3, block_number: (
            f"0x{block_number:064x}",
            1_700_000_000_000 + block_number,
        ),
    )
    monkeypatch.setattr(lp_ledger_export, "_coverage_chain_id", lambda *_args: 8453)
    monkeypatch.setattr(
        lp_ledger_export,
        "_replayed_price_events_for_actions",
        lambda _w3, _config, _start, _end, actions: [
            _price_event(action.action_type, action.block_number, action.log_index, action.event_order, 2**96, 0)
            for action in actions
        ],
    )

    count = lp_ledger_export.export_rpc_lp_ledger(
        "uni-base",
        100,
        100,
        output,
        candidate_tx_hashes=(increase_hash, transfer_hash, mint_hash),
    )

    rows = list(csv.DictReader(output.open()))
    assert count == 2
    assert [row["event_type"] for row in rows] == ["mint", "mint"]
    assert rows[1]["liquidity_delta"] == "111"
    assert rows[1]["lp_owner"] == Web3.to_checksum_address(next_owner)
    assert rows[1]["owner_source"] == "transfer"


def test_candidate_tx_hashes_from_csv_filters_lp_events_and_block_range(tmp_path):
    path = tmp_path / "pool_history.csv"
    path.write_text(
        "block_number,event_type,tx_hash\n"
        f"99,mint,0x{'99' * 32}\n"
        f"100,swap,0x{'11' * 32}\n"
        f"101,mint,0x{'22' * 32}\n"
        f"102,burn,0x{'33' * 32}\n"
        f"103,collect,0x{'22' * 32}\n"
        "104,collect,\n"
        f"105,mint,0x{'44' * 32}\n"
    )

    tx_hashes = lp_ledger_export._candidate_tx_hashes_from_csv(path, 100, 104)

    assert tx_hashes == ["0x" + "22" * 32, "0x" + "33" * 32]


def test_export_v4_lp_ledger_cli_writes_fixture_rows(tmp_path):
    decoded_actions = tmp_path / "decoded_actions.json"
    ownership_events = tmp_path / "ownership_events.json"
    price_events = tmp_path / "price_events.json"
    output = tmp_path / "ledger.csv"
    decoded_actions.write_text(json.dumps([
        {
            "action_type": "mint",
            "block_number": 100,
            "log_index": 10,
            "event_order": 0,
            "token_id": 42,
            "lp_owner": None,
            "tick_lower": -120,
            "tick_upper": 120,
            "liquidity_delta": 1000,
            "amount0": "1",
            "amount1": "2",
            "amount0_actual": "1",
            "amount1_actual": "2",
            "amount0_attribution_source": "fixture",
            "amount1_attribution_source": "fixture",
            "amount_attribution_status": "fixture_exact",
            "collect_amount0": "0",
            "collect_amount1": "0",
            "chain": "base",
            "pool_id": POOL_CONFIGS["uni-base"].pool_id,
            "block_time": "2026-01-01T00:00:00+00:00",
            "tx_hash": "0xabc",
            "position_manager": "0xpm",
            "amount0_raw": "1",
            "amount1_raw": "2",
            "timestamp_ms": 1000,
        }
    ]))
    ownership_events.write_text(json.dumps([
        {
            "block_number": 100,
            "log_index": 9,
            "event_order": 0,
            "token_id": 42,
            "previous_owner": None,
            "new_owner": "0xowner",
        }
    ]))
    price_events.write_text(json.dumps([
        {
            "block_number": 100,
            "log_index": 10,
            "event_order": 0,
            "event_type": "mint",
            "sqrt_price_x96": None,
            "tick": None,
            "event_time_sqrt_price_x96": 222,
            "event_time_tick": 2,
            "event_time_state_source": "prior_event",
        }
    ]))

    completed = subprocess.run(
        [
            sys.executable,
            "research/scripts/export_v4_lp_ledger.py",
            "--pool",
            "uni-base",
            "--start-block",
            "100",
            "--end-block",
            "100",
            "--decoded-actions",
            str(decoded_actions),
            "--ownership-events",
            str(ownership_events),
            "--price-events",
            str(price_events),
            "--out",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    rows = list(csv.DictReader(output.open()))
    assert len(rows) == 1
    assert rows[0]["token_id"] == "42"
    assert rows[0]["lp_owner"] == "0xowner"
    assert rows[0]["sqrt_price_x96_at_event"] == "222"
    assert rows[0]["amount0_actual"] == "1"
    assert rows[0]["amount_attribution_status"] == "fixture_exact"
    coverage = json.loads(ledger_coverage_path(output).read_text())
    assert coverage["verification_mode"] == "fixture_unverified"
    assert coverage["ledger_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert coverage["fixture_input_sha256"] == {
        "decoded_actions": hashlib.sha256(decoded_actions.read_bytes()).hexdigest(),
        "ownership_events": hashlib.sha256(ownership_events.read_bytes()).hexdigest(),
        "price_events": hashlib.sha256(price_events.read_bytes()).hexdigest(),
    }
