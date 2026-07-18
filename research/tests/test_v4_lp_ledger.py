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
from research.backtester.v4_event_replay import ReplayedEvent
from research.backtester.v4_export import POOL_CONFIGS, V4_MODIFY_LIQUIDITY_TOPIC
from research.backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
)
from research.cross_pool.contracts import CrossPoolContractError

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


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


def test_verified_candidate_discovery_unions_target_pool_actions_and_transfers(
    monkeypatch,
):
    config = POOL_CONFIGS["uni-base"]
    action_only_hash = "0x" + "AA" * 32
    shared_hash = "0x" + "22" * 32
    transfer_only_hash = "0x" + "11" * 32
    calls = []

    def fake_fetch(_w3, params, *, context):
        calls.append((params, context))
        if params["address"] == Web3.to_checksum_address(config.pool_manager):
            return [
                {"transactionHash": action_only_hash},
                {"transactionHash": shared_hash},
            ]
        return [
            {"transactionHash": transfer_only_hash},
            {"transactionHash": shared_hash},
        ]

    monkeypatch.setattr(lp_ledger_export, "_fetch_logs_with_debug", fake_fetch)

    discovery = lp_ledger_export._discover_rpc_lp_candidates(
        object(),
        config,
        1,
        12_000,
    )

    assert len(calls) == 6
    assert [
        (call[0]["fromBlock"], call[0]["toBlock"]) for call in calls[::2]
    ] == [(1, 5_000), (5_001, 10_000), (10_001, 12_000)]
    for action_call, transfer_call in zip(calls[::2], calls[1::2], strict=True):
        assert action_call[0]["address"] == Web3.to_checksum_address(
            config.pool_manager
        )
        assert action_call[0]["topics"] == [
            V4_MODIFY_LIQUIDITY_TOPIC,
            config.pool_id,
        ]
        assert transfer_call[0]["address"] == Web3.to_checksum_address(
            config.position_manager
        )
        assert transfer_call[0]["topics"] == [
            "0x" + TRANSFER_TOPIC.removeprefix("0x")
        ]
    assert discovery.action_transaction_hashes == (
        "0x" + "22" * 32,
        "0x" + "aa" * 32,
    )
    assert discovery.ownership_transaction_hashes == (
        "0x" + "11" * 32,
        "0x" + "22" * 32,
    )
    assert discovery.all_transaction_hashes == (
        "0x" + "11" * 32,
        "0x" + "22" * 32,
        "0x" + "aa" * 32,
    )


def test_verified_candidate_discovery_rejects_a_log_without_transaction_hash(
    monkeypatch,
):
    config = POOL_CONFIGS["uni-base"]
    monkeypatch.setattr(
        lp_ledger_export,
        "_fetch_logs_with_debug",
        lambda *_args, **_kwargs: [{}],
    )

    with pytest.raises(ValueError, match="transactionHash"):
        lp_ledger_export._discover_rpc_lp_candidates(object(), config, 1, 1)


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
    discovery_called = False

    def discover(*_args):
        nonlocal discovery_called
        discovery_called = True
        return lp_ledger_export.RpcLPCandidateDiscovery((), (), ())

    monkeypatch.setattr(lp_ledger_export, "_discover_rpc_lp_candidates", discover)
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
        )

    assert not discovery_called
    assert not output.exists()
    assert not ledger_coverage_path(output).exists()


def test_rpc_chain_identity_must_match_the_frozen_pool() -> None:
    fake_w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=56))

    with pytest.raises(ValueError, match="does not match 8453"):
        lp_ledger_export._coverage_chain_id(fake_w3, "uni-base")


def test_wrong_rpc_chain_fails_before_candidate_discovery(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "ledger.csv"
    fake_w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=56))
    discovery_called = False

    def discover(*_args):
        nonlocal discovery_called
        discovery_called = True
        return lp_ledger_export.RpcLPCandidateDiscovery((), (), ())

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(lp_ledger_export, "_discover_rpc_lp_candidates", discover)

    with pytest.raises(ValueError, match="does not match 8453"):
        lp_ledger_export.export_rpc_lp_ledger(
            "uni-base",
            100,
            100,
            output,
        )

    assert not discovery_called
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

    def discover(*_args):
        events.append("discovery")
        return lp_ledger_export.RpcLPCandidateDiscovery((), (), ())

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
    monkeypatch.setattr(lp_ledger_export, "_discover_rpc_lp_candidates", discover)
    monkeypatch.setattr(lp_ledger_export, "_decode_rpc_lp_inputs", decode)
    monkeypatch.setattr(lp_ledger_export, "_replayed_price_events_for_actions", replay)
    monkeypatch.setattr(lp_ledger_export, "build_rpc_ledger_coverage_bytes", build_coverage)
    monkeypatch.setattr(lp_ledger_export, "_publish_ledger_pair", publish)

    count = lp_ledger_export.export_rpc_lp_ledger("uni-base", 100, 102, output)

    assert count == 0
    assert events == [
        "header:100",
        "header:102",
        "discovery",
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
    monkeypatch.setattr(
        lp_ledger_export,
        "_discover_rpc_lp_candidates",
        lambda *_args: lp_ledger_export.RpcLPCandidateDiscovery((), (), ()),
    )
    monkeypatch.setattr(lp_ledger_export, "_decode_rpc_lp_inputs", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(lp_ledger_export, "_replayed_price_events_for_actions", lambda *_args: [])
    monkeypatch.setattr(lp_ledger_export, "build_rpc_ledger_coverage_bytes", build_coverage)
    monkeypatch.setattr(lp_ledger_export, "_publish_ledger_pair", publish)

    with pytest.raises(CrossPoolContractError, match="endpoint.*changed"):
        lp_ledger_export.export_rpc_lp_ledger("uni-base", 100, 102, output)

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
    monkeypatch.setattr(
        lp_ledger_export,
        "_discover_rpc_lp_candidates",
        lambda *_args: lp_ledger_export.RpcLPCandidateDiscovery(
            action_transaction_hashes=(tx_hash,),
            ownership_transaction_hashes=(tx_hash,),
            all_transaction_hashes=(tx_hash,),
        ),
    )
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

    count = lp_ledger_export.export_rpc_lp_ledger("uni-base", 100, 100, output)

    rows = list(csv.DictReader(output.open()))
    assert count == 1
    assert rows[0]["token_id"] == "77"
    assert rows[0]["lp_owner"] == Web3.to_checksum_address(recipient)
    assert rows[0]["event_type"] == "mint"
    assert rows[0]["amount0"] == "0.7"
    assert rows[0]["amount1"] == "1.5"
    assert rows[0]["amount_attribution_status"] == "exact"
    coverage = json.loads(ledger_coverage_path(output).read_text())
    assert coverage["verification_mode"] == "rpc_verified"
    assert coverage["covered_start_block"] == 100
    assert coverage["covered_end_block"] == 100
    assert coverage["ledger_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()

    candidate_output = tmp_path / "candidate-ledger.csv"
    lp_ledger_export.export_rpc_lp_ledger(
        "uni-base",
        100,
        100,
        candidate_output,
        candidate_tx_hashes=[tx_hash],
    )
    candidate_coverage = json.loads(ledger_coverage_path(candidate_output).read_text())
    assert candidate_coverage["verification_mode"] == "candidate_list_unverified"


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
    monkeypatch.setattr(
        lp_ledger_export,
        "_discover_rpc_lp_candidates",
        lambda *_args: lp_ledger_export.RpcLPCandidateDiscovery(
            action_transaction_hashes=(increase_hash, mint_hash),
            ownership_transaction_hashes=(mint_hash, transfer_hash),
            all_transaction_hashes=(increase_hash, transfer_hash, mint_hash),
        ),
    )
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

    count = lp_ledger_export.export_rpc_lp_ledger("uni-base", 100, 100, output)

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
