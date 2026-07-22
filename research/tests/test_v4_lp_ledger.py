import copy
import csv
import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode  # type: ignore[attr-defined]
from requests import ConnectionError as RequestsConnectionError
from web3 import Web3

import research.scripts.export_v4_lp_ledger as lp_ledger_export
from engine.lp.types import (
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_INCREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_SETTLE_PAIR,
    _V4_LP_TAKE_PAIR,
)
from research.backtester.lp_ledger_attribution import (
    build_rpc_ledger_coverage_bytes,
    ledger_coverage_path,
    load_ledger_coverage,
)
from research.backtester.lp_ledger_checkpoint import BlockRange, DiscoveryWitness
from research.backtester.v4_event_replay import ReplayedEvent
from research.backtester.v4_export import (
    POOL_CONFIGS,
    V4_MODIFY_LIQUIDITY_TOPIC,
    _make_web3,
)
from research.backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
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
        candidate_transaction_hashes=("0x" + "33" * 32,),
        verification_mode="rpc_verified",
    )
    return ledger_bytes, coverage_bytes


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
        "input": "0x",
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

    with pytest.raises(ValueError, match="not representable"):
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

    with pytest.raises(ValueError, match="every target-pool ModifyLiquidity"):
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
    monkeypatch.setattr(lp_ledger_export, "build_rpc_ledger_coverage_bytes", build_coverage)
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
    monkeypatch.setattr(lp_ledger_export, "build_rpc_ledger_coverage_bytes", build_coverage)
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
        "data": hex(token_or_amount),
        "logIndex": log_index,
    }


def _erc20_transfer_log(
    *,
    token: str,
    from_address: str = "0x0000000000000000000000000000000000000011",
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


def _modify_liquidity_log(log_index: int) -> dict[str, object]:
    return {
        "address": POOL_CONFIGS["uni-base"].pool_manager,
        "topics": [
            V4_MODIFY_LIQUIDITY_TOPIC,
            POOL_CONFIGS["uni-base"].pool_id,
            _topic_address("0x0000000000000000000000000000000000000022"),
        ],
        "data": "0x",
        "logIndex": log_index,
    }


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

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, {})

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

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, {})

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
            _modify_liquidity_log(12),
        ],
    }

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, {})

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

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, {})

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
        "input": _build_modify_input(bytes([_V4_LP_INCREASE_LIQUIDITY, 18, 18]), [increase_param, settle0_param, settle1_param]),
        "hash": "0x" + "17" * 32,
        "blockNumber": 100,
        "from": sender,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(8),
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

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, token_state)

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
        "input": _build_modify_input(bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]), [decrease_param, take_pair_param]),
        "hash": "0x" + "22" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(8),
            _erc20_transfer_log(token=config.token0_address, to_address=recipient, amount_raw=2_500_000, log_index=9),
            _erc20_transfer_log(token=config.token1_address, to_address=recipient, amount_raw=1_250_000, log_index=10),
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

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, token_state)

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
            _modify_liquidity_log(8),
            _erc20_transfer_log(token=config.token0_address, to_address=sender, amount_raw=2_500_000, log_index=9),
            _erc20_transfer_log(token=config.token1_address, to_address=sender, amount_raw=1_250_000, log_index=10),
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

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, token_state)

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
        "input": _build_modify_input(bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]), [decrease_param, take_pair_param]),
        "hash": "0x" + "23" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(8),
            _erc20_transfer_log(token=config.token0_address, to_address=recipient, amount_raw=2_500_000, log_index=9),
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
            "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
            "hash": mint_hash,
            "blockNumber": 100,
            "transactionIndex": 5,
        },
        increase_hash: {
            "input": _build_modify_input(bytes([_V4_LP_INCREASE_LIQUIDITY, _V4_LP_SETTLE_PAIR]), [increase_param, settle_param]),
            "hash": increase_hash,
            "blockNumber": 100,
            "transactionIndex": 7,
        },
        transfer_hash: {
            "input": "0x",
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
            "logs": [_modify_liquidity_log(12)],
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
