import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

import research.backtester.lp_ledger_checkpoint as checkpoint_module
from research.backtester.lp_ledger_checkpoint import (
    CHECKPOINT_SOURCE_PATHS,
    ActionPriceBinding,
    BlockHeader,
    BlockRange,
    CandidateBundle,
    CheckpointContractError,
    CheckpointPaths,
    DecoderStateUpsert,
    DiscoveryWitness,
    EndpointSnapshot,
    LPLedgerCheckpoint,
    PositionResolution,
    ReplayEvidence,
    ReplaySeed,
    RunIdentity,
    acquire_run_lock,
    candidate_payload_sha256,
)
from research.backtester.v4_event_replay import PoolStateSnapshot
from research.backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
)

_HASH_A = f"0x{'a' * 64}"
_HASH_B = f"0x{'b' * 64}"
_ADDRESS_A = f"0x{'1' * 40}"
_ADDRESS_B = f"0x{'2' * 40}"
_ADDRESS_C = f"0x{'3' * 40}"
_ADDRESS_D = f"0x{'4' * 40}"
_ADDRESS_E = f"0x{'5' * 40}"
_ADDRESS_CHECKSUM = "0x52908400098527886E0F7030069857D2E4169EE7"
_HASH_C = f"0x{'c' * 64}"
_HASH_D = f"0x{'d' * 64}"
_HASH_E = f"0x{'e' * 64}"


def test_checkpoint_paths_are_output_derived(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    assert paths.output == tmp_path / "ledger.csv"
    assert paths.database == tmp_path / "ledger.csv.checkpoint.sqlite3"
    assert paths.wal == tmp_path / "ledger.csv.checkpoint.sqlite3-wal"
    assert paths.shm == tmp_path / "ledger.csv.checkpoint.sqlite3-shm"
    assert paths.progress == tmp_path / "ledger.csv.progress.json"
    assert paths.run_lock == tmp_path / "ledger.csv.run.lock"
    assert paths.publish_lock == tmp_path / "ledger.csv.publish.lock"


def test_discovery_chunk_commits_both_sources_and_quiet_ranges_atomically(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    modify = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=7,
        log_index=11,
    )
    transfer = _witness(
        source="position_transfer",
        transaction_hash=_HASH_D,
        block_number=102,
        block_hash=_HASH_D,
        transaction_index=5,
        log_index=13,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            ranges = run.incomplete_discovery_chunks()
            assert ranges == (BlockRange(0, 100, 104), BlockRange(1, 105, 109))

            run.commit_discovery_chunk(ranges[0], (modify,), (transfer,))
            after_evidence = run.snapshot()
            run.commit_discovery_chunk(ranges[1], (), ())
            after_quiet = run.snapshot()

            assert run.incomplete_discovery_chunks() == ()
            assert run.candidate_hashes() == (_HASH_C, _HASH_D)

    assert after_quiet.generation == after_evidence.generation + 1


def test_discovery_chunk_rejects_two_hashes_for_one_block_atomically(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    first_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    conflicting_witness = _witness(
        source="position_transfer",
        transaction_hash=_HASH_D,
        block_number=101,
        block_hash=_HASH_D,
        transaction_index=6,
        log_index=12,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first = run.incomplete_discovery_chunks()[0]
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="conflicting hashes"):
                run.commit_discovery_chunk(
                    first,
                    (first_witness,),
                    (conflicting_witness,),
                )

            assert run.snapshot() == before
            assert run.incomplete_discovery_chunks()[0] == first
            assert run.candidate_hashes() == ()


def test_candidate_bundles_decode_in_location_order_not_fetch_order(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    earlier = _witness(
        source="pool_modify",
        transaction_hash=_HASH_D,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    later = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=7,
        log_index=13,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (earlier, later), ())
            run.commit_discovery_chunk(second, (), ())
            run.commit_candidate_bundle(_bundle(later))
            run.commit_candidate_bundle(_bundle(earlier))

            assert [bundle.transaction_index for bundle in run.undecoded_bundles()] == [5, 7]


def test_candidate_bundle_missing_a_discovery_witness_rolls_back(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (witness,), ())
            run.commit_discovery_chunk(second, (), ())
            valid = _bundle(witness)
            receipt = json.loads(valid.receipt_json)
            receipt["logs"] = []
            receipt_json = _canonical_json(receipt)
            missing = replace(
                valid,
                receipt_json=receipt_json,
                payload_sha256=candidate_payload_sha256(
                    valid.transaction_json,
                    receipt_json,
                ),
            )
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="missing"):
                run.commit_candidate_bundle(missing)

            assert run.snapshot() == before
            assert run.unfetched_candidate_hashes() == (_HASH_C,)


def test_large_evm_values_round_trip_without_sqlite_numeric_coercion(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    action = _action(witness)
    huge = 2**160 - 1

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (witness,), ())
            run.commit_discovery_chunk(second, (), ())
            bundle = _bundle(witness)
            run.commit_candidate_bundle(bundle)
            run.commit_decoded_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        token_id=77,
                        state=LedgerPositionState(_HASH_A, -10, 10, 100),
                        last_block_number=101,
                        last_log_index=11,
                        last_event_order=0,
                    ),
                ),
                state_deletes=(),
                actions=(action,),
                owners=(),
            )
            replay_ranges = run.incomplete_replay_chunks()
            run.commit_replay_chunk(replay_ranges[0], (), ())
            run.commit_replay_chunk(replay_ranges[1], (), ())
            run.commit_replay_seed(
                ReplaySeed(
                    first_event_block=101,
                    state=PoolStateSnapshot(huge, -1, "prior_block"),
                    state_block_number=100,
                    state_block_hash=_HASH_A,
                ),
                BlockHeader(100, _HASH_A, 1_000),
            )

            loaded = run.load_replay_seed()

    assert loaded is not None
    assert loaded.state is not None
    assert loaded.state.sqrt_price_x96 == huge
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT sqrt_price_x96 FROM replay_seed").fetchone()[0] == str(
            huge
        )


def test_precommit_failure_rolls_back_entire_discovery_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    def fail_before_commit(stage: str) -> None:
        if stage == "before_commit":
            raise RuntimeError("injected precommit crash")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            before = run.snapshot()
            first = run.incomplete_discovery_chunks()[0]
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_before_commit)

            with pytest.raises(RuntimeError, match="precommit"):
                run.commit_discovery_chunk(first, (witness,), ())

            assert run.snapshot() == before
            assert run.incomplete_discovery_chunks()[0] == first
            assert run.candidate_hashes() == ()


def test_postcommit_failure_preserves_entire_discovery_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    def fail_after_commit(stage: str) -> None:
        if stage == "after_commit":
            raise RuntimeError("injected postcommit crash")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            before = run.snapshot()
            first = run.incomplete_discovery_chunks()[0]
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_after_commit)

            with pytest.raises(RuntimeError, match="postcommit"):
                run.commit_discovery_chunk(first, (witness,), ())

            monkeypatch.setattr(checkpoint_module, "_mutation_hook", lambda _stage: None)
            assert run.snapshot().generation == before.generation + 1
            assert run.incomplete_discovery_chunks() == (BlockRange(1, 105, 109),)
            assert run.candidate_hashes() == (_HASH_C,)


def test_precommit_failure_rolls_back_entire_decoded_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    transfer = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=12,
    )
    action = _action(witness)
    state = LedgerPositionState(_HASH_A, -10, 10, 100)
    resolved_state = LedgerPositionState(_HASH_A, -20, 20, 50)

    def fail_before_commit(stage: str) -> None:
        if stage == "before_commit":
            raise RuntimeError("injected decode precommit crash")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_candidate_to_decode(run, witness, transfer)
            before = run.snapshot()
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_before_commit)

            with pytest.raises(RuntimeError, match="decode precommit"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(
                        BlockHeader(100, _HASH_A, 1_000),
                        BlockHeader(101, _HASH_E, 1_010),
                    ),
                    resolutions=(PositionResolution(88, 100, True, resolved_state),),
                    state_upserts=(DecoderStateUpsert(77, state, 101, 11, 0),),
                    state_deletes=(),
                    actions=(action,),
                    owners=(OwnershipEvent(101, 12, 77, None, _ADDRESS_C, 0),),
                )

            assert run.snapshot() == before
            assert run.undecoded_bundles() == (bundle,)
            assert run.load_header(101) is None
            assert run.load_position_resolution(88, 100) is None
            assert run.load_decoded_actions() == ()
            assert run.load_ownership_events() == ()
            assert run.load_decoder_state() == {}


def test_postcommit_failure_preserves_entire_decoded_transaction_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    transfer = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=12,
    )
    action = _action(witness)
    state = LedgerPositionState(_HASH_A, -10, 10, 100)
    resolved_state = LedgerPositionState(_HASH_A, -20, 20, 50)
    owner = OwnershipEvent(101, 12, 77, None, _ADDRESS_C, 0)

    def fail_after_commit(stage: str) -> None:
        if stage == "after_commit":
            raise RuntimeError("injected decode postcommit crash")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            bundle = _stage_candidate_to_decode(run, witness, transfer)
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_after_commit)

            with pytest.raises(RuntimeError, match="decode postcommit"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(
                        BlockHeader(100, _HASH_A, 1_000),
                        BlockHeader(101, _HASH_E, 1_010),
                    ),
                    resolutions=(PositionResolution(88, 100, True, resolved_state),),
                    state_upserts=(DecoderStateUpsert(77, state, 101, 11, 0),),
                    state_deletes=(),
                    actions=(action,),
                    owners=(owner,),
                )

        monkeypatch.setattr(checkpoint_module, "_mutation_hook", lambda _stage: None)
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as reopened:
            assert reopened.undecoded_bundles() == ()
            assert reopened.load_header(101) == BlockHeader(101, _HASH_E, 1_010)
            assert reopened.load_position_resolution(88, 100) == PositionResolution(
                88,
                100,
                True,
                resolved_state,
            )
            assert reopened.load_decoded_actions() == (action,)
            assert reopened.load_ownership_events() == (owner,)
            assert reopened.load_decoder_state() == {77: state}


def test_decode_rejects_candidate_header_fork_without_partial_state(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="conflicting hashes"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(BlockHeader(101, _HASH_D, 1_010),),
                    resolutions=(),
                    state_upserts=(
                        DecoderStateUpsert(
                            77,
                            LedgerPositionState(_HASH_A, -10, 10, 100),
                            101,
                            11,
                            0,
                        ),
                    ),
                    state_deletes=(),
                    actions=(_action(witness),),
                    owners=(),
                )

            assert run.snapshot() == before
            assert run.undecoded_bundles() == (bundle,)
            assert run.load_header(101) is None
            assert run.load_decoded_actions() == ()
            assert run.load_decoder_state() == {}


def test_replay_rejects_conflicting_decoded_header_timestamp_atomically(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        77,
                        LedgerPositionState(_HASH_A, -10, 10, 100),
                        101,
                        11,
                        0,
                    ),
                ),
                state_deletes=(),
                actions=(_action(witness),),
                owners=(),
            )
            first = run.incomplete_replay_chunks()[0]
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="conflicting timestamps"):
                run.commit_replay_chunk(
                    first,
                    (BlockHeader(101, _HASH_E, 1_011),),
                    (),
                )

            assert run.snapshot() == before
            assert run.incomplete_replay_chunks()[0] == first
            assert run.load_replay_events() == ()


def test_zero_candidate_and_zero_action_phases_advance_only_after_exact_evidence(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (), ())
            run.commit_discovery_chunk(second, (), ())
            assert run.snapshot().phase == "candidate_fetch"

            run.complete_phase("candidate_fetch")
            assert run.snapshot().phase == "chronological_decode"
            run.complete_phase("chronological_decode")
            assert run.snapshot().phase == "price_event_scan"

            replay_first, replay_second = run.incomplete_replay_chunks()
            run.commit_replay_chunk(replay_first, (), ())
            run.commit_replay_chunk(replay_second, (), ())
            assert run.snapshot().phase == "price_replay"
            run.complete_phase("price_replay")
            assert run.snapshot().phase == "build"


def test_decoded_transaction_round_trip_is_one_durable_unit(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    transfer = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=12,
    )
    action = replace(_action(witness), lp_owner=_ADDRESS_CHECKSUM)
    owner = OwnershipEvent(101, 12, 77, None, _ADDRESS_CHECKSUM, 0)
    state = LedgerPositionState(_HASH_A, -10, 10, 100)
    resolved_state = LedgerPositionState(_HASH_A, -20, 20, 50)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_candidate_to_decode(run, witness, transfer)
            before = run.snapshot()
            run.commit_decoded_transaction(
                bundle,
                headers=(
                    BlockHeader(100, _HASH_A, 1_000),
                    BlockHeader(101, _HASH_E, 1_010),
                ),
                resolutions=(PositionResolution(88, 100, True, resolved_state),),
                state_upserts=(DecoderStateUpsert(77, state, 101, 11, 0),),
                state_deletes=(),
                actions=(action,),
                owners=(owner,),
            )

            assert run.snapshot().generation == before.generation + 1
            assert run.load_decoded_actions() == (action,)
            assert run.load_ownership_events() == (owner,)
            assert run.load_decoder_state() == {77: state}
            assert run.load_position_resolution(88, 100) == PositionResolution(
                88,
                100,
                True,
                resolved_state,
            )
            marker = run._connection.execute(
                "SELECT action_count, ownership_count FROM decoded_transactions"
            ).fetchone()

    assert tuple(marker) == (1, 1)


def test_ownership_only_decode_caches_explicit_no_position_resolution(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    owner = OwnershipEvent(101, 11, 88, None, _ADDRESS_C, 0)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_transaction(
                bundle,
                headers=(
                    BlockHeader(100, _HASH_A, 1_000),
                    BlockHeader(101, _HASH_E, 1_010),
                ),
                resolutions=(PositionResolution(88, 100, False, None),),
                state_upserts=(),
                state_deletes=(),
                actions=(),
                owners=(owner,),
            )

            assert run.load_position_resolution(88, 100) == PositionResolution(88, 100, False, None)
            assert run.load_decoded_actions() == ()
            assert run.load_ownership_events() == (owner,)
            assert run.snapshot().phase == "price_event_scan"


def test_decoder_state_update_and_burn_delete_share_transaction_cursors(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    mint_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_D,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    burn_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=102,
        block_hash=_HASH_D,
        transaction_index=7,
        log_index=13,
    )
    position = LedgerPositionState(_HASH_A, -10, 10, 100)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (burn_witness, mint_witness), ())
            run.commit_discovery_chunk(second, (), ())
            burn_bundle = _bundle(burn_witness)
            mint_bundle = _bundle(mint_witness)
            run.commit_candidate_bundle(burn_bundle)
            run.commit_candidate_bundle(mint_bundle)
            run.commit_decoded_transaction(
                mint_bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(DecoderStateUpsert(77, position, 101, 11, 0),),
                state_deletes=(),
                actions=(_action(mint_witness),),
                owners=(),
            )
            burn_action = replace(
                _action(burn_witness),
                action_type="burn",
                liquidity_delta=-100,
                amount0=Decimal("0"),
                amount1=Decimal("0"),
                amount0_actual=Decimal("0"),
                amount1_actual=Decimal("0"),
            )
            run.commit_decoded_transaction(
                burn_bundle,
                headers=(BlockHeader(102, _HASH_D, 1_020),),
                resolutions=(),
                state_upserts=(),
                state_deletes=(77,),
                actions=(burn_action,),
                owners=(),
            )

            assert run.load_decoder_state() == {}
            assert run.load_decoded_actions() == (_action(mint_witness), burn_action)
            assert run.snapshot().phase == "price_event_scan"


def test_decoder_state_upsert_must_match_final_action_for_its_token(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="decoder state"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(BlockHeader(101, _HASH_E, 1_010),),
                    resolutions=(),
                    state_upserts=(
                        DecoderStateUpsert(
                            88,
                            LedgerPositionState(_HASH_A, -10, 10, 100),
                            101,
                            11,
                            0,
                        ),
                    ),
                    state_deletes=(),
                    actions=(_action(witness),),
                    owners=(),
                )

            assert run.snapshot() == before
            assert run.load_decoder_state() == {}


def test_decoder_state_upsert_must_match_action_derived_state(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="action-derived state"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(BlockHeader(101, _HASH_E, 1_010),),
                    resolutions=(),
                    state_upserts=(
                        DecoderStateUpsert(
                            77,
                            LedgerPositionState(_HASH_A, -10, 10, 999),
                            101,
                            11,
                            0,
                        ),
                    ),
                    state_deletes=(),
                    actions=(_action(witness),),
                    owners=(),
                )

            assert run.snapshot() == before
            assert run.undecoded_bundles() == (bundle,)
            assert run.load_decoder_state() == {}


def test_zero_liquidity_position_delete_can_be_tied_to_ownership_burn(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    mint_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_D,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    burn_witness = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=102,
        block_hash=_HASH_D,
        transaction_index=7,
        log_index=13,
    )
    zero_state = LedgerPositionState(_HASH_A, -10, 10, 0)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (mint_witness,), (burn_witness,))
            run.commit_discovery_chunk(second, (), ())
            mint_bundle = _bundle(mint_witness)
            burn_bundle = _bundle(burn_witness)
            run.commit_candidate_bundle(burn_bundle)
            run.commit_candidate_bundle(mint_bundle)
            mint = replace(_action(mint_witness), liquidity_delta=0)
            run.commit_decoded_transaction(
                mint_bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(DecoderStateUpsert(77, zero_state, 101, 11, 0),),
                state_deletes=(),
                actions=(mint,),
                owners=(),
            )
            burn_owner = OwnershipEvent(102, 13, 77, _ADDRESS_C, None, 0)
            run.commit_decoded_transaction(
                burn_bundle,
                headers=(BlockHeader(102, _HASH_D, 1_020),),
                resolutions=(),
                state_upserts=(),
                state_deletes=(77,),
                actions=(),
                owners=(burn_owner,),
            )

            assert run.load_decoder_state() == {}
            assert run.load_ownership_events() == (burn_owner,)


def test_ownership_event_must_match_candidate_transfer_witness(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="Transfer witness"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(BlockHeader(101, _HASH_E, 1_010),),
                    resolutions=(),
                    state_upserts=(),
                    state_deletes=(),
                    actions=(),
                    owners=(OwnershipEvent(101, 12, 77, None, _ADDRESS_C, 0),),
                )

            assert run.snapshot() == before
            assert run.undecoded_bundles() == (bundle,)
            assert run.load_ownership_events() == ()


def test_one_modify_witness_cannot_expand_to_multiple_actions(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    first_action = _action(witness)
    second_action = replace(first_action, event_order=1)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)

            with pytest.raises(CheckpointContractError, match="one action"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(BlockHeader(101, _HASH_E, 1_010),),
                    resolutions=(),
                    state_upserts=(
                        DecoderStateUpsert(
                            77,
                            LedgerPositionState(_HASH_A, -10, 10, 200),
                            101,
                            11,
                            1,
                        ),
                    ),
                    state_deletes=(),
                    actions=(first_action, second_action),
                    owners=(),
                )

            assert run.undecoded_bundles() == (bundle,)


def test_collect_action_outside_modify_witness_is_preserved(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    mint = _action(witness)
    collect = replace(
        mint,
        action_type="collect",
        log_index=12,
        event_order=1,
        liquidity_delta=0,
        amount0=Decimal("0"),
        amount1=Decimal("0"),
        amount0_raw="0",
        amount1_raw="0",
        amount0_actual=Decimal("0"),
        amount1_actual=Decimal("0"),
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            state = LedgerPositionState(_HASH_A, -10, 10, 100)
            run.commit_decoded_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(DecoderStateUpsert(77, state, 101, 12, 1),),
                state_deletes=(),
                actions=(mint, collect),
                owners=(),
            )

            assert run.load_decoded_actions() == (mint, collect)
            assert run.load_decoder_state() == {77: state}


def test_position_resolution_found_flag_must_be_an_exact_bool(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    state = LedgerPositionState(_HASH_A, -10, 10, 100)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="found flag"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(
                        BlockHeader(100, _HASH_A, 1_000),
                        BlockHeader(101, _HASH_E, 1_010),
                    ),
                    resolutions=(PositionResolution(77, 100, 1, state),),  # type: ignore[arg-type]
                    state_upserts=(),
                    state_deletes=(),
                    actions=(),
                    owners=(OwnershipEvent(101, 11, 77, None, _ADDRESS_C, 0),),
                )

            assert run.snapshot() == before
            assert run.load_position_resolution(77, 100) is None


@pytest.mark.parametrize("field", ("amount0_raw", "amount1_raw"))
def test_decoded_raw_amounts_reject_negative_text(tmp_path: Path, field: str) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()
            action = replace(_action(witness), **{field: "-1"})

            with pytest.raises(CheckpointContractError, match="unsigned"):
                run.commit_decoded_transaction(
                    bundle,
                    headers=(BlockHeader(101, _HASH_E, 1_010),),
                    resolutions=(),
                    state_upserts=(
                        DecoderStateUpsert(
                            77,
                            LedgerPositionState(_HASH_A, -10, 10, 100),
                            101,
                            11,
                            0,
                        ),
                    ),
                    state_deletes=(),
                    actions=(action,),
                    owners=(),
                )

            assert run.snapshot() == before
            assert run.undecoded_bundles() == (bundle,)


def test_build_inputs_fail_closed_before_build_phase(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            with pytest.raises(CheckpointContractError, match="build"):
                run.load_build_inputs()


def test_sqlite_integer_boundary_rejects_values_above_signed_64_bit(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="signed-64"):
                run.commit_discovery_chunk(BlockRange(2**63, 100, 104), (), ())

            assert run.snapshot() == before


def test_replay_evidence_bindings_and_build_inputs_are_canonical(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    action = _action(witness)
    initialize = ReplayEvidence(
        block_number=100,
        log_index=1,
        event_order=0,
        transaction_index=0,
        block_hash=_HASH_A,
        transaction_hash=_HASH_B,
        event_type="initialize",
        sqrt_price_x96=2**96,
        tick=0,
    )
    swap = ReplayEvidence(
        block_number=101,
        log_index=2,
        event_order=0,
        transaction_index=1,
        block_hash=_HASH_E,
        transaction_hash=_HASH_D,
        event_type="swap",
        sqrt_price_x96=2**96 + 1,
        tick=1,
    )
    binding = ActionPriceBinding(101, 11, 0, 2**96 + 1, 1, "same_block_prior_event")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        77,
                        LedgerPositionState(_HASH_A, -10, 10, 100),
                        101,
                        11,
                        0,
                    ),
                ),
                state_deletes=(),
                actions=(action,),
                owners=(),
            )
            first, second = run.incomplete_replay_chunks()
            run.commit_replay_chunk(
                first,
                (
                    BlockHeader(100, _HASH_A, 1_000),
                    BlockHeader(101, _HASH_E, 1_010),
                ),
                (swap, initialize),
            )
            run.commit_replay_chunk(second, (), ())
            run.commit_replay_seed(ReplaySeed(100, None, None, None), None)
            before_binding = run.snapshot()
            with pytest.raises(CheckpointContractError, match="deterministic replay"):
                run.commit_action_price_bindings((replace(binding, event_time_tick=999),))
            assert run.snapshot() == before_binding
            assert run.load_action_price_bindings() == ()
            run.commit_action_price_bindings((binding,))

            assert [event.event_type for event in run.load_replay_events()] == [
                "initialize",
                "swap",
            ]
            assert run.load_action_price_bindings() == (binding,)
            inputs = run.load_build_inputs()
            run.begin_publication("a" * 64, "b" * 64)
            assert run.load_publication_state().state == "started"
            run.mark_published()
            publication = run.load_publication_state()
            published_snapshot = run.snapshot()
            assert run.checkpoint_terminal_wal() in ("truncated", "busy")
        with LPLedgerCheckpoint.open_status(paths) as reopened:
            assert reopened.load_publication_state() == publication
            assert reopened.load_build_inputs() == inputs

    assert inputs.actions == (action,)
    assert inputs.candidate_hashes == (_HASH_C,)
    assert inputs.action_price_bindings == (binding,)
    assert len(inputs.replay_events) == 1
    assert inputs.replay_events[0].event_type == "mint"
    assert inputs.replay_events[0].event_time_sqrt_price_x96 == 2**96 + 1
    assert inputs.replay_events[0].event_time_state_source == "same_block_prior_event"
    assert publication.state == "published"
    assert publication.expected_ledger_sha256 == "a" * 64
    assert publication.expected_sidecar_sha256 == "b" * 64
    assert published_snapshot.phase == "succeeded"
    assert published_snapshot.status == "succeeded"
    assert published_snapshot.output_published is True


def test_no_state_seed_rejects_a_later_earliest_event(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        77,
                        LedgerPositionState(_HASH_A, -10, 10, 100),
                        101,
                        11,
                        0,
                    ),
                ),
                state_deletes=(),
                actions=(_action(witness),),
                owners=(),
            )
            first, second = run.incomplete_replay_chunks()
            run.commit_replay_chunk(first, (), ())
            run.commit_replay_chunk(second, (), ())

            with pytest.raises(CheckpointContractError, match="earliest"):
                run.commit_replay_seed(ReplaySeed(100, None, None, None), None)


def test_checkpoint_paths_normalize_absolute_output_and_reject_non_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert CheckpointPaths.from_output(Path("nested/../ledger.csv")).output == (
        tmp_path / "ledger.csv"
    )
    with pytest.raises(CheckpointContractError, match=r"\.csv"):
        CheckpointPaths.from_output(tmp_path / "ledger.json")


def test_run_identity_is_canonical_and_contains_no_rpc_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "alchemy-secret-that-must-not-be-persisted"
    monkeypatch.setenv("ALCHEMY_KEY", secret)
    identity = _identity(tmp_path / "ledger.csv")

    payload = identity.canonical_bytes()
    assert payload == identity.canonical_bytes()
    assert json.loads(payload) == json.loads(identity.canonical_bytes())
    assert secret.encode() not in payload
    assert identity.sha256() == identity.sha256()
    assert len(identity.sha256()) == 64
    assert {field.name for field in fields(RunIdentity)}.isdisjoint(
        {"rpc_url", "api_key", "headers", "command_line", "exception"}
    )
    for unsafe in (
        replace(identity, exporter_version=f"https://provider.invalid/v2/{secret}"),
        replace(identity, pool=f"Authorization: Bearer {secret}"),
        replace(identity, python_version=f"X-API-Key={secret}"),
    ):
        with pytest.raises(CheckpointContractError, match="identity"):
            unsafe.canonical_bytes()


@pytest.mark.parametrize("mutation", ("missing", "extra", "reordered"))
def test_run_identity_requires_exact_semantic_source_manifest(
    tmp_path: Path,
    mutation: str,
) -> None:
    identity = _identity(tmp_path / "ledger.csv")
    sources = identity.source_sha256
    if mutation == "missing":
        mutated = sources[:-1]
    elif mutation == "extra":
        mutated = tuple(sorted((*sources, ("unexpected.py", "f" * 64))))
    else:
        mutated = tuple(reversed(sources))

    with pytest.raises(CheckpointContractError, match="source"):
        replace(identity, source_sha256=mutated).canonical_bytes()


def test_compatible_resume_increments_attempt_and_generation(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            first = run.snapshot()
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            resumed = run.snapshot()

    assert resumed.run_id == first.run_id
    assert resumed.attempt_number == first.attempt_number + 1
    assert resumed.generation == first.generation + 1


@pytest.mark.parametrize("mutation", ("range", "endpoint", "source", "output"))
def test_incompatible_resume_fails_without_mutating_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    original = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, original, fresh=False) as run:
            before = run.snapshot()
        with pytest.raises(CheckpointContractError, match="identity"):
            LPLedgerCheckpoint.create_or_resume(
                paths,
                _mutated_identity(original, mutation),
                fresh=False,
            )
        with LPLedgerCheckpoint.open_status(paths) as run:
            assert run.snapshot() == before


def test_run_lock_contention_precedes_checkpoint_and_progress_mutation(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="already active"):
            with acquire_run_lock(paths):
                raise AssertionError("unreachable")
        assert not paths.database.exists()
        assert not paths.progress.exists()

    assert stat.S_IMODE(paths.run_lock.stat().st_mode) == 0o600


def test_checkpoint_creation_uses_frozen_schema_and_durable_pragmas(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            connection = run._connection
            pragmas = {
                name: connection.execute(f"PRAGMA {name}").fetchone()[0]
                for name in (
                    "application_id",
                    "foreign_keys",
                    "journal_mode",
                    "synchronous",
                    "trusted_schema",
                    "user_version",
                )
            }
            objects = tuple(
                connection.execute(
                    """
                    SELECT type, name
                    FROM sqlite_master
                    WHERE type IN ('table', 'view', 'trigger')
                      AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            )
            tables = tuple(row["name"] for row in objects if row["type"] == "table")
            views = tuple(row["name"] for row in objects if row["type"] == "view")
            triggers = tuple(row["name"] for row in objects if row["type"] == "trigger")
            declared_types = {
                column[2].upper()
                for table in tables
                for column in connection.execute(f'PRAGMA table_info("{table}")')
            }

    assert pragmas == {
        "application_id": 1_280_330_819,
        "foreign_keys": 1,
        "journal_mode": "wal",
        "synchronous": 2,
        "trusted_schema": 0,
        "user_version": 1,
    }
    assert tables == (
        "action_price_bindings",
        "candidate_bundles",
        "chain_blocks",
        "decoded_actions",
        "decoded_transactions",
        "decoder_token_state",
        "discovery_chunks",
        "discovery_witnesses",
        "ownership_events",
        "phase_state",
        "position_resolutions",
        "publication_state",
        "replay_chunks",
        "replay_events",
        "replay_seed",
        "run_identity",
        "run_state",
        "schema_meta",
    )
    assert views == ()
    assert triggers == ()
    assert "REAL" not in declared_types
    assert "BLOB" not in declared_types
    assert stat.S_IMODE(paths.database.stat().st_mode) == 0o600


@pytest.mark.parametrize("drift", ("table", "version", "application_id", "index"))
def test_resume_rejects_schema_or_version_drift(tmp_path: Path, drift: str) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        with sqlite3.connect(paths.database) as connection:
            if drift == "table":
                connection.execute("CREATE TABLE unexpected (value TEXT) STRICT")
            elif drift == "version":
                connection.execute("PRAGMA user_version = 2")
            elif drift == "application_id":
                connection.execute("PRAGMA application_id = 1")
            else:
                connection.execute("DROP INDEX phase_state_status")
        with pytest.raises(CheckpointContractError, match="schema"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize(
    "corruption", ("discovery_partition", "replay_partition", "count", "phase")
)
def test_resume_rejects_logically_inconsistent_checkpoint(
    tmp_path: Path,
    corruption: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        with sqlite3.connect(paths.database) as connection:
            if corruption == "discovery_partition":
                connection.execute(
                    "UPDATE discovery_chunks SET end_block = 105 WHERE chunk_index = 0"
                )
            elif corruption == "replay_partition":
                connection.execute(
                    "UPDATE replay_chunks SET start_block = 106 WHERE chunk_index = 1"
                )
            elif corruption == "count":
                connection.execute(
                    """
                    UPDATE discovery_chunks
                    SET status = 'completed', modify_witness_count = 1,
                        completed_generation = 1
                    WHERE chunk_index = 0
                    """
                )
            else:
                connection.execute(
                    "UPDATE phase_state SET status = 'running' WHERE phase = 'candidate_discovery'"
                )
        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_discovery_witness_moved_outside_its_chunk(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            run.commit_discovery_chunk(
                run.incomplete_discovery_chunks()[0],
                (witness,),
                (),
            )
            generation = run.snapshot().generation
        with sqlite3.connect(paths.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO chain_blocks VALUES (?, ?, NULL, ?)",
                (106, _HASH_D, generation),
            )
            connection.execute(
                """
                UPDATE discovery_witnesses
                SET block_number = 106, block_hash = ?
                WHERE transaction_hash = ?
                """,
                (_HASH_D, _HASH_C),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_candidate_payload_digest_tamper(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            _stage_single_candidate_to_decode(run, witness)
        with sqlite3.connect(paths.database) as connection:
            connection.execute(
                "UPDATE candidate_bundles SET payload_sha256 = ?",
                ("f" * 64,),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_nonprefix_decoded_transaction(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    earlier = _witness(
        source="position_transfer",
        transaction_hash=_HASH_D,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    later = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=7,
        log_index=13,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_discovery_chunks()
            run.commit_discovery_chunk(first, (), (earlier, later))
            run.commit_discovery_chunk(second, (), ())
            run.commit_candidate_bundle(_bundle(later))
            run.commit_candidate_bundle(_bundle(earlier))
            generation = run.snapshot().generation
        empty_state_digest = hashlib.sha256(b"[]").hexdigest()
        with sqlite3.connect(paths.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO decoded_transactions VALUES (?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    later.transaction_hash,
                    later.block_number,
                    later.block_hash,
                    later.transaction_index,
                    empty_state_digest,
                    generation,
                ),
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = 1, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'chronological_decode'
                """,
                (_canonical_json({"transaction_hash": later.transaction_hash}), generation),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_missing_bundle_even_if_phase_totals_are_rewritten(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    witness = _witness(
        source="position_transfer",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            _stage_single_candidate_to_decode(run, witness)
            generation = run.snapshot().generation
        with sqlite3.connect(paths.database) as connection:
            connection.execute("DELETE FROM candidate_bundles")
            connection.execute(
                """
                UPDATE phase_state
                SET completed = 0, total = 0, updated_generation = ?
                WHERE phase IN ('candidate_fetch', 'chronological_decode')
                """,
                (generation,),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_replay_seed_moved_after_earliest_event(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        77,
                        LedgerPositionState(_HASH_A, -10, 10, 100),
                        101,
                        11,
                        0,
                    ),
                ),
                state_deletes=(),
                actions=(_action(witness),),
                owners=(),
            )
            first, second = run.incomplete_replay_chunks()
            run.commit_replay_chunk(first, (), ())
            run.commit_replay_chunk(second, (), ())
            run.commit_replay_seed(
                ReplaySeed(
                    101,
                    PoolStateSnapshot(2**96, 0, "prior_block"),
                    100,
                    _HASH_A,
                ),
                BlockHeader(100, _HASH_A, 1_000),
            )
        with sqlite3.connect(paths.database) as connection:
            connection.execute(
                """
                UPDATE replay_seed
                SET first_event_block = 102, state_block_number = 101,
                    state_block_hash = ?
                WHERE singleton = 1
                """,
                (_HASH_E,),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize("status", ("succeeded", "failed"))
def test_terminal_checkpoint_is_not_reset_to_running_on_reopen(
    tmp_path: Path,
    status: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        _mark_checkpoint_succeeded(paths.database, status=status)
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            terminal = run.snapshot()

    assert terminal.status == status
    assert terminal.phase == "succeeded"
    assert terminal.attempt_number == 1
    assert terminal.generation == 1
    assert terminal.output_published is True
    assert terminal.error_code == ("checkpoint_maintenance_error" if status == "failed" else None)


def test_terminal_checkpoint_can_record_nonbusy_wal_maintenance_failure(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        _mark_checkpoint_succeeded(paths.database, status="succeeded")

        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            before = run.snapshot()
            failed = run.mark_checkpoint_maintenance_failed()

            assert failed.generation == before.generation + 1
            assert failed.phase == "succeeded"
            assert failed.status == "failed"
            assert failed.output_published is True
            assert failed.error_code == "checkpoint_maintenance_error"
            assert run.load_publication_state().state == "published"


def test_fresh_replaces_only_operational_namespace(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    coverage = Path(f"{paths.output}.coverage.json")
    paths.output.write_bytes(b"prior-ledger")
    coverage.write_bytes(b"prior-coverage")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            prior_run_id = run.snapshot().run_id
        paths.progress.write_text("stale progress", encoding="utf-8")
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=True) as run:
            fresh_run_id = run.snapshot().run_id

    assert fresh_run_id != prior_run_id
    assert paths.output.read_bytes() == b"prior-ledger"
    assert coverage.read_bytes() == b"prior-coverage"
    assert not paths.progress.exists()


def test_status_open_does_not_create_missing_checkpoint(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with pytest.raises(CheckpointContractError, match="does not exist"):
        LPLedgerCheckpoint.open_status(paths)

    assert not paths.database.exists()
    assert not paths.run_lock.exists()
    assert not paths.progress.exists()


@pytest.mark.parametrize(
    "leaf",
    ("output", "coverage", "database", "wal", "shm", "progress", "publish_lock"),
)
def test_checkpoint_rejects_every_symlinked_namespace_leaf(
    tmp_path: Path,
    leaf: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    target = tmp_path / "real-file"
    target.write_bytes(b"")
    selected = {
        "output": paths.output,
        "coverage": Path(f"{paths.output}.coverage.json"),
        "database": paths.database,
        "wal": paths.wal,
        "shm": paths.shm,
        "progress": paths.progress,
        "publish_lock": paths.publish_lock,
    }[leaf]
    selected.symlink_to(target)

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="symlink"):
            LPLedgerCheckpoint.create_or_resume(
                paths,
                _identity(paths.output),
                fresh=False,
            )


def test_run_lock_rejects_symlinked_leaf(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    target = tmp_path / "real-lock"
    target.write_bytes(b"")
    paths.run_lock.symlink_to(target)

    with pytest.raises(CheckpointContractError, match="symlink"):
        with acquire_run_lock(paths):
            raise AssertionError("unreachable")


def test_checkpoint_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_paths = CheckpointPaths.from_output(linked_parent / "ledger.csv")
    with pytest.raises(CheckpointContractError, match="symlink"):
        with acquire_run_lock(linked_paths):
            raise AssertionError("unreachable")


@pytest.mark.parametrize("leaf", ("wal", "shm"))
def test_status_rejects_symlinked_sqlite_sibling(tmp_path: Path, leaf: str) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass

    sibling = getattr(paths, leaf)
    sibling.unlink(missing_ok=True)
    target = tmp_path / f"real-{leaf}"
    target.write_bytes(b"")
    sibling.symlink_to(target)

    with pytest.raises(CheckpointContractError, match="symlink"):
        LPLedgerCheckpoint.open_status(paths)


def test_resume_rechecks_database_after_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass

        original_connect = checkpoint_module.sqlite3.connect
        real_database = tmp_path / "real-checkpoint.sqlite3"

        def swap_then_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            paths.database.replace(real_database)
            paths.database.symlink_to(real_database)
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(checkpoint_module.sqlite3, "connect", swap_then_connect)
        with pytest.raises(CheckpointContractError, match="symlink"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_failed_sqlite_configuration_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingConnection:
        row_factory: object = None
        closed = False

        def execute(self, _statement: str) -> object:
            raise sqlite3.DatabaseError("injected pragma failure")

        def close(self) -> None:
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(checkpoint_module.sqlite3, "connect", lambda *_a, **_k: connection)
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    paths.database.touch(mode=0o600)

    with pytest.raises(CheckpointContractError, match="could not be opened"):
        checkpoint_module._connect_read_write(paths)

    assert connection.closed is True


def _witness(
    *,
    source: str,
    transaction_hash: str,
    block_number: int,
    block_hash: str,
    transaction_index: int,
    log_index: int,
) -> DiscoveryWitness:
    return DiscoveryWitness(
        source=source,  # type: ignore[arg-type]
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=transaction_hash,
        transaction_index=transaction_index,
        log_index=log_index,
        address=_ADDRESS_A if source == "pool_modify" else _ADDRESS_B,
        topic0=_HASH_A,
        topic1=_HASH_B,
    )


def _bundle(
    witness: DiscoveryWitness,
    *additional_witnesses: DiscoveryWitness,
) -> CandidateBundle:
    witnesses = (witness, *additional_witnesses)
    if any(
        (
            value.transaction_hash,
            value.block_number,
            value.block_hash,
            value.transaction_index,
        )
        != (
            witness.transaction_hash,
            witness.block_number,
            witness.block_hash,
            witness.transaction_index,
        )
        for value in witnesses
    ):
        raise AssertionError("candidate witnesses must share one transaction location")
    transaction_json = _canonical_json(
        {
            "blockHash": witness.block_hash,
            "blockNumber": str(witness.block_number),
            "hash": witness.transaction_hash,
            "transactionIndex": str(witness.transaction_index),
        }
    )
    receipt_json = _canonical_json(
        {
            "blockHash": witness.block_hash,
            "blockNumber": str(witness.block_number),
            "logs": [
                {
                    "address": value.address,
                    "blockHash": value.block_hash,
                    "logIndex": str(value.log_index),
                    "topics": [
                        value.topic0,
                        *([] if value.topic1 is None else [value.topic1]),
                    ],
                    "transactionHash": value.transaction_hash,
                    "transactionIndex": str(value.transaction_index),
                }
                for value in witnesses
            ],
            "transactionHash": witness.transaction_hash,
            "transactionIndex": str(witness.transaction_index),
        }
    )
    return CandidateBundle(
        transaction_hash=witness.transaction_hash,
        block_number=witness.block_number,
        block_hash=witness.block_hash,
        transaction_index=witness.transaction_index,
        transaction_json=transaction_json,
        receipt_json=receipt_json,
        payload_sha256=candidate_payload_sha256(transaction_json, receipt_json),
    )


def _stage_single_candidate_to_decode(
    run: LPLedgerCheckpoint,
    witness: DiscoveryWitness,
) -> CandidateBundle:
    return _stage_candidate_to_decode(run, witness)


def _stage_candidate_to_decode(
    run: LPLedgerCheckpoint,
    *witnesses: DiscoveryWitness,
) -> CandidateBundle:
    if not witnesses:
        raise AssertionError("candidate staging requires at least one witness")
    run.complete_phase("preflight")
    first, second = run.incomplete_discovery_chunks()
    run.commit_discovery_chunk(
        first,
        tuple(witness for witness in witnesses if witness.source == "pool_modify"),
        tuple(witness for witness in witnesses if witness.source == "position_transfer"),
    )
    run.commit_discovery_chunk(second, (), ())
    bundle = _bundle(witnesses[0], *witnesses[1:])
    run.commit_candidate_bundle(bundle)
    return bundle


def _action(witness: DiscoveryWitness) -> DecodedLiquidityAction:
    return DecodedLiquidityAction(
        action_type="mint",
        block_number=witness.block_number,
        log_index=witness.log_index,
        event_order=0,
        token_id=77,
        lp_owner=_ADDRESS_C,
        tick_lower=-10,
        tick_upper=10,
        liquidity_delta=100,
        amount0=Decimal("1.25"),
        amount1=Decimal("2.5"),
        collect_amount0=Decimal("0"),
        collect_amount1=Decimal("0"),
        chain="base",
        pool_id=_HASH_A,
        block_time="1970-01-01T00:00:01.010Z",
        tx_hash=witness.transaction_hash,
        position_manager=_ADDRESS_B,
        amount0_raw="1250000",
        amount1_raw="2500000000000000000",
        timestamp_ms=1_010,
        amount0_actual=Decimal("1.25"),
        amount1_actual=Decimal("2.5"),
        amount0_attribution_source="test",
        amount1_attribution_source="test",
        amount_attribution_status="exact",
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _identity(output: Path) -> RunIdentity:
    return RunIdentity(
        schema_version=1,
        exporter_version="lp-ledger-checkpoint-v1",
        verification_mode="rpc_verified",
        pool="uni-base",
        chain="base",
        chain_id=8453,
        pool_id=_HASH_A,
        pool_manager=_ADDRESS_A,
        position_manager=_ADDRESS_B,
        state_view=_ADDRESS_C,
        token0_address=_ADDRESS_D,
        token1_address=_ADDRESS_E,
        token0_decimals=6,
        token1_decimals=18,
        fee_rate="0.0005",
        invert_price=False,
        start_block=100,
        end_block=109,
        chunk_size=5,
        discovery_topics=(_HASH_A, _HASH_B),
        output_path=os.path.abspath(output),
        endpoint=EndpointSnapshot(
            start=BlockHeader(100, _HASH_A, 1_000),
            end=BlockHeader(109, _HASH_B, 2_000),
        ),
        python_version="3.12.0",
        web3_version="7.0.0",
        source_sha256=tuple((path, "a" * 64) for path in CHECKPOINT_SOURCE_PATHS),
    )


def _mutated_identity(identity: RunIdentity, mutation: str) -> RunIdentity:
    if mutation == "range":
        return replace(identity, end_block=identity.end_block + 1)
    if mutation == "endpoint":
        return replace(
            identity,
            endpoint=replace(
                identity.endpoint,
                end=replace(identity.endpoint.end, block_hash=_HASH_A),
            ),
        )
    if mutation == "source":
        return replace(identity, source_sha256=(("export_v4_lp_ledger.py", "b" * 64),))
    if mutation == "output":
        return replace(identity, output_path=f"{identity.output_path}.other")
    raise AssertionError(f"unsupported identity mutation: {mutation}")


def _mark_checkpoint_succeeded(database: Path, *, status: str) -> None:
    error_code = "checkpoint_maintenance_error" if status == "failed" else None
    with sqlite3.connect(database) as connection:
        for table in ("discovery_chunks", "replay_chunks"):
            connection.execute(
                f"""
                UPDATE {table}
                SET status = 'completed', completed_generation = 1
                """
            )
        phase_counts = {
            "preflight": (1, 1),
            "candidate_discovery": (2, 2),
            "candidate_fetch": (0, 0),
            "chronological_decode": (0, 0),
            "price_event_scan": (2, 2),
            "price_replay": (0, 0),
            "build": (0, 0),
            "publish": (2, 2),
            "succeeded": (1, 1),
        }
        for phase, (completed, total) in phase_counts.items():
            connection.execute(
                """
                UPDATE phase_state
                SET status = 'completed', completed = ?, total = ?,
                    updated_generation = 1
                WHERE phase = ?
                """,
                (completed, total, phase),
            )
        connection.execute(
            """
            UPDATE publication_state
            SET state = 'published', expected_ledger_sha256 = ?,
                expected_sidecar_sha256 = ?, started_generation = 1,
                published_generation = 1
            WHERE singleton = 1
            """,
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            """
            UPDATE run_state
            SET status = ?, phase = 'succeeded',
                finished_at_utc = updated_at_utc, output_published = 1,
                error_code = ?
            WHERE singleton = 1
            """,
            (status, error_code),
        )
