import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
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
    ErrorCode,
    LPLedgerCheckpoint,
    PositionResolution,
    RunIdentity,
    acquire_run_lock,
    candidate_payload_sha256,
    progress_from_snapshot,
    read_export_status,
    render_safe_failure,
    should_write_progress,
    write_progress_atomically,
)
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


def test_schema_v2_public_contract_is_action_first_and_replay_bound() -> None:
    assert checkpoint_module.CHECKPOINT_SCHEMA_VERSION == 2
    assert checkpoint_module.PROGRESS_SCHEMA_VERSION == 2
    assert checkpoint_module._PHASES == (
        "preflight",
        "action_discovery",
        "action_fetch",
        "action_decode",
        "token_set_freeze",
        "full_transfer_scan",
        "relevant_transfer_fetch",
        "relevant_transfer_decode",
        "replay_input_bind",
        "price_replay",
        "build",
        "publish",
        "succeeded",
    )
    assert tuple(field.name for field in fields(DiscoveryWitness)) == (
        "source",
        "block_number",
        "block_hash",
        "transaction_hash",
        "transaction_index",
        "log_index",
        "address",
        "topics",
        "data",
    )
    assert tuple(
        field.name for field in fields(checkpoint_module.ReplayInputEvidence)
    ) == (
        "path",
        "sha256",
        "byte_length",
        "row_count",
        "header_sha256",
        "parser_version",
        "price_semantics_sha256",
        "chain",
        "pool_id",
        "first_block",
        "last_block",
        "first_timestamp_ms",
        "last_timestamp_ms",
        "price_event_count",
        "price_events_sha256",
    )
    assert tuple(field.name for field in fields(checkpoint_module.PositionKeyMapping)) == (
        "token_id",
        "pool_id",
        "tick_lower",
        "tick_upper",
        "salt",
        "mint_block_number",
        "mint_log_index",
        "mint_event_order",
    )
    identity_fields = tuple(field.name for field in fields(RunIdentity))
    assert "state_view" not in identity_fields
    assert "discovery_topics" not in identity_fields
    assert "wrapper_entrypoint" in identity_fields
    assert "action_topic" in identity_fields
    assert "transfer_topic" in identity_fields
    assert "replay_input" in identity_fields
    build_fields = tuple(field.name for field in fields(checkpoint_module.BuildInputs))
    assert build_fields == (
        "action_witnesses",
        "action_transaction_hashes",
        "frozen_token_ids",
        "position_keys",
        "transfer_chunk_attestations",
        "relevant_transfer_witnesses",
        "relevant_transfer_transaction_hashes",
        "eligible_bundles",
        "replay_input",
        "actions",
        "ownership_events",
        "action_price_bindings",
    )
    assert "research/backtester/pool_price_semantics.py" in CHECKPOINT_SOURCE_PATHS


def test_action_chunk_round_trips_full_witnesses_and_quiet_ranges_atomically(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    modify = replace(
        _witness(
            source="pool_modify",
            transaction_hash=_HASH_C,
            block_number=101,
            block_hash=_HASH_E,
            transaction_index=7,
            log_index=11,
        ),
        topics=(_HASH_A, _HASH_A, _HASH_D),
        data="0x1234",
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            ranges = run.incomplete_action_chunks()
            assert ranges == (BlockRange(0, 100, 104), BlockRange(1, 105, 109))

            run.commit_action_chunk(ranges[0], (modify,))
            after_evidence = run.snapshot()
            run.commit_action_chunk(ranges[1], ())
            after_quiet = run.snapshot()

            assert run.incomplete_action_chunks() == ()
            assert run.action_candidate_hashes() == (_HASH_C,)
            assert run.action_witnesses(_HASH_C) == (modify,)

    assert after_quiet.generation == after_evidence.generation + 1
    assert after_quiet.phase == "action_fetch"
    assert after_quiet.action_candidate_transaction_count == 1


def test_action_chunk_rejects_two_hashes_for_one_block_atomically(tmp_path: Path) -> None:
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
        source="pool_modify",
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
            first = run.incomplete_action_chunks()[0]
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="conflicting hashes"):
                run.commit_action_chunk(first, (first_witness, conflicting_witness))

            assert run.snapshot() == before
            assert run.incomplete_action_chunks()[0] == first
            assert run.action_candidate_hashes() == ()


def test_action_bundles_decode_in_location_order_not_fetch_order(
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
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (earlier, later))
            run.commit_action_chunk(second, ())
            run.commit_action_bundle(_bundle(later))
            run.commit_action_bundle(_bundle(earlier))

            assert [
                bundle.transaction_index for bundle in run.undecoded_action_bundles()
            ] == [5, 7]
            assert run.snapshot().phase == "action_decode"


def test_action_bundle_missing_a_full_witness_rolls_back(tmp_path: Path) -> None:
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
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (witness,))
            run.commit_action_chunk(second, ())
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
                run.commit_action_bundle(missing)

            assert run.snapshot() == before
            assert run.unfetched_action_hashes() == (_HASH_C,)


def test_action_decode_persists_position_key_and_freezes_token_set(
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
    bundle = _bundle(witness)
    action = _action(witness)
    position_key = checkpoint_module.PositionKeyMapping(
        token_id=action.token_id,
        pool_id=_HASH_A,
        tick_lower=-10,
        tick_upper=10,
        salt=_HASH_D,
        mint_block_number=101,
        mint_log_index=11,
        mint_event_order=0,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (witness,))
            run.commit_action_chunk(second, ())
            run.commit_action_bundle(bundle)
            decoded = run.commit_decoded_action_transaction(
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
                position_keys=(position_key,),
            )
            frozen = run.freeze_action_token_set()

            assert run.frozen_token_ids() == (77,)
            assert run.load_position_keys() == (position_key,)

    assert decoded.phase == "token_set_freeze"
    assert frozen.phase == "full_transfer_scan"
    assert frozen.frozen_token_count == 1

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as resumed:
            assert resumed.frozen_token_ids() == (77,)

    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "UPDATE frozen_token_set SET token_ids_sha256 = ? WHERE singleton = 1",
            ("f" * 64,),
        )
    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="staged evidence"):
            LPLedgerCheckpoint.create_or_resume(
                paths,
                _identity(paths.output),
                fresh=False,
            )


def test_action_decode_identity_is_read_from_the_frozen_run(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            identity = run.action_decode_identity()

    assert identity.pool == "uni-base"
    assert identity.chain == "base"
    assert identity.pool_id == _HASH_A
    assert identity.pool_manager == _ADDRESS_A
    assert identity.position_manager == _ADDRESS_B
    assert identity.wrapper_entrypoint == _ADDRESS_C


def test_creation_mapping_covers_a_later_mint_typed_increase_in_the_same_tx(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    creation_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    increase_witness = replace(creation_witness, log_index=12)
    bundle = _bundle(creation_witness, increase_witness)
    creation = _action(creation_witness)
    increase = replace(
        creation,
        log_index=12,
        event_order=1,
        liquidity_delta=25,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            _stage_candidate_to_decode(run, creation_witness, increase_witness)

            decoded = run.commit_decoded_action_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        token_id=77,
                        state=LedgerPositionState(_HASH_A, -10, 10, 125),
                        last_block_number=101,
                        last_log_index=12,
                        last_event_order=1,
                    ),
                ),
                state_deletes=(),
                actions=(creation, increase),
                position_keys=(_position_key(creation),),
            )

            assert decoded.phase == "token_set_freeze"
            assert run.load_position_keys() == (_position_key(creation),)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as resumed:
            assert resumed.load_position_keys() == (_position_key(creation),)
            assert resumed.load_decoded_actions() == (creation, increase)


def test_transfer_attestation_reuses_action_bundle_and_decodes_exact_owner(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    action_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    owner_topic = f"0x{'0' * 24}{_ADDRESS_C[2:]}"
    transfer_witness = replace(
        _witness(
            source="position_transfer",
            transaction_hash=_HASH_C,
            block_number=101,
            block_hash=_HASH_E,
            transaction_index=5,
            log_index=12,
        ),
        topics=(
            _HASH_B,
            f"0x{'0' * 64}",
            owner_topic,
            f"0x{77:064x}",
        ),
    )
    bundle = _bundle(action_witness, transfer_witness)
    action = _action(action_witness)
    position_key = checkpoint_module.PositionKeyMapping(
        token_id=77,
        pool_id=_HASH_A,
        tick_lower=-10,
        tick_upper=10,
        salt=_HASH_D,
        mint_block_number=101,
        mint_log_index=11,
        mint_event_order=0,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            identity,
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            action_first, action_second = run.incomplete_action_chunks()
            run.commit_action_chunk(action_first, (action_witness,))
            run.commit_action_chunk(action_second, ())
            run.commit_action_bundle(bundle)
            run.commit_decoded_action_transaction(
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
                position_keys=(position_key,),
            )
            run.freeze_action_token_set()

            transfer_first, transfer_second = run.incomplete_transfer_chunks()
            run.commit_transfer_chunk(
                transfer_first,
                unfiltered_count=5,
                unfiltered_sha256=hashlib.sha256(b"all-parent-zero-logs").hexdigest(),
                relevant_witnesses=(transfer_witness,),
            )
            scanned = run.commit_transfer_chunk(
                transfer_second,
                unfiltered_count=0,
                unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                relevant_witnesses=(),
            )

            assert run.relevant_transfer_transaction_hashes() == (_HASH_C,)
            assert run.relevant_transfer_witnesses(_HASH_C) == (transfer_witness,)
            assert run.unfetched_relevant_transfer_hashes() == (_HASH_C,)

            run.commit_relevant_transfer_bundle(bundle)
            assert run.unfetched_relevant_transfer_hashes() == ()
            assert len(run.undecoded_relevant_transfer_bundles()) == 1
            assert run._connection.execute(
                "SELECT COUNT(*) FROM transaction_bundles"
            ).fetchone()[0] == 1

            decoded = run.commit_decoded_relevant_transfer_transaction(
                bundle,
                owners=(OwnershipEvent(101, 12, 77, None, _ADDRESS_C),),
            )
            run.commit_replay_input(identity.replay_input)
            binding = ActionPriceBinding(
                block_number=101,
                log_index=11,
                event_order=0,
                event_time_sqrt_price_x96=2**96,
                event_time_tick=0,
                event_time_state_source="prior_event",
            )
            with pytest.raises(CheckpointContractError, match="binding set"):
                run.commit_action_price_bindings(())
            built = run.commit_action_price_bindings((binding,))
            inputs = run.load_build_inputs()

    assert scanned.phase == "relevant_transfer_fetch"
    assert decoded.phase == "replay_input_bind"
    assert decoded.full_transfer_log_count == 5
    assert decoded.relevant_transfer_witness_count == 1
    assert decoded.relevant_transfer_transaction_count == 1
    assert decoded.ownership_event_count == 1
    assert built.phase == "build"
    assert inputs.action_witnesses == (action_witness,)
    assert inputs.action_transaction_hashes == (_HASH_C,)
    assert inputs.frozen_token_ids == (77,)
    assert inputs.position_keys == (position_key,)
    assert inputs.relevant_transfer_witnesses == (transfer_witness,)
    assert inputs.relevant_transfer_transaction_hashes == (_HASH_C,)
    assert inputs.eligible_bundles == (bundle,)
    assert inputs.replay_input == identity.replay_input
    assert inputs.actions == (action,)
    assert inputs.ownership_events == (
        OwnershipEvent(101, 12, 77, None, _ADDRESS_C),
    )
    assert inputs.action_price_bindings == (binding,)
    assert inputs.transfer_chunk_attestations == (
        checkpoint_module.TransferChunkAttestation(
            index=0,
            start_block=100,
            end_block=104,
            unfiltered_count=5,
            unfiltered_sha256=hashlib.sha256(b"all-parent-zero-logs").hexdigest(),
            relevant_count=1,
        ),
        checkpoint_module.TransferChunkAttestation(
            index=1,
            start_block=105,
            end_block=109,
            unfiltered_count=0,
            unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
            relevant_count=0,
        ),
    )


def test_zero_action_and_zero_relevant_transfer_phases_complete_explicitly(
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
            action_first, action_second = run.incomplete_action_chunks()
            run.commit_action_chunk(action_first, ())
            discovered = run.commit_action_chunk(action_second, ())

            assert discovered.phase == "action_fetch"
            assert run.complete_phase("action_fetch").phase == "action_decode"
            assert run.complete_phase("action_decode").phase == "token_set_freeze"
            assert run.freeze_action_token_set().phase == "full_transfer_scan"
            assert run.frozen_token_ids() == ()

            transfer_first, transfer_second = run.incomplete_transfer_chunks()
            run.commit_transfer_chunk(
                transfer_first,
                unfiltered_count=4,
                unfiltered_sha256=hashlib.sha256(b"four unrelated logs").hexdigest(),
                relevant_witnesses=(),
            )
            scanned = run.commit_transfer_chunk(
                transfer_second,
                unfiltered_count=2,
                unfiltered_sha256=hashlib.sha256(b"two unrelated logs").hexdigest(),
                relevant_witnesses=(),
            )

            assert scanned.phase == "relevant_transfer_fetch"
            assert scanned.full_transfer_log_count == 6
            assert run.complete_phase("relevant_transfer_fetch").phase == (
                "relevant_transfer_decode"
            )
            completed = run.complete_phase("relevant_transfer_decode")

    assert completed.phase == "replay_input_bind"
    assert completed.action_candidate_transaction_count == 0
    assert completed.action_count == 0
    assert completed.frozen_token_count == 0
    assert completed.relevant_transfer_witness_count == 0
    assert completed.relevant_transfer_transaction_count == 0
    assert completed.ownership_event_count == 0


def test_replay_input_bind_is_identity_exact_and_resume_validated(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(chunk, ())
            run.complete_phase("action_fetch")
            run.complete_phase("action_decode")
            run.freeze_action_token_set()
            for chunk in run.incomplete_transfer_chunks():
                run.commit_transfer_chunk(
                    chunk,
                    unfiltered_count=0,
                    unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                    relevant_witnesses=(),
                )
            run.complete_phase("relevant_transfer_fetch")
            run.complete_phase("relevant_transfer_decode")

            bound = run.commit_replay_input(identity.replay_input)

            assert bound.phase == "price_replay"
            assert run.load_replay_input() == identity.replay_input
            durable = dict(bound.last_durable or ())
            assert durable["replay_input_sha256"] == identity.replay_input.sha256

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as resumed:
            assert resumed.load_replay_input() == identity.replay_input

    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "UPDATE replay_input SET price_events_sha256 = ? WHERE singleton = 1",
            ("f" * 64,),
        )
    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="staged evidence"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_requires_empty_frozen_set_evidence_after_phase_completion(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(chunk, ())
            run.complete_phase("action_fetch")
            run.complete_phase("action_decode")
            run.freeze_action_token_set()

    with sqlite3.connect(paths.database) as connection:
        connection.execute("DELETE FROM frozen_token_set")

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="staged evidence"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_requires_replay_record_after_completed_empty_phase_tamper(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(chunk, ())
            run.complete_phase("action_fetch")
            run.complete_phase("action_decode")
            run.freeze_action_token_set()
            for chunk in run.incomplete_transfer_chunks():
                run.commit_transfer_chunk(
                    chunk,
                    unfiltered_count=0,
                    unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                    relevant_witnesses=(),
                )
            run.complete_phase("relevant_transfer_fetch")
            run.complete_phase("relevant_transfer_decode")
            run.commit_replay_input(identity.replay_input)

    with sqlite3.connect(paths.database) as connection:
        connection.execute("DELETE FROM replay_input")
        connection.execute(
            "UPDATE phase_state SET completed = 0, total = 0 "
            "WHERE phase = 'replay_input_bind'"
        )

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="staged evidence"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize("mutation", ("address", "topic", "pool"))
def test_resume_rechecks_action_witness_target_identity(
    tmp_path: Path,
    mutation: str,
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
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (witness,))
            run.commit_action_chunk(second, ())

    with sqlite3.connect(paths.database) as connection:
        if mutation == "address":
            connection.execute(
                "UPDATE action_witnesses SET address = ? WHERE transaction_hash = ?",
                (_ADDRESS_D, witness.transaction_hash),
            )
        else:
            topics = list(witness.topics)
            topics[0 if mutation == "topic" else 1] = _HASH_D
            connection.execute(
                "UPDATE action_witnesses SET topics_json = ? WHERE transaction_hash = ?",
                (_canonical_json(topics), witness.transaction_hash),
            )

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="staged evidence"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rechecks_decoded_action_run_identity(tmp_path: Path) -> None:
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
    action = _action(witness)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_action_transaction(
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
                position_keys=(_position_key(action),),
            )

    with sqlite3.connect(paths.database) as connection:
        row = connection.execute(
            "SELECT action_json FROM decoded_actions WHERE transaction_hash = ?",
            (witness.transaction_hash,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["chain"] = "bsc"
        action_json = _canonical_json(payload)
        connection.execute(
            "UPDATE decoded_actions SET action_json = ?, payload_sha256 = ? "
            "WHERE transaction_hash = ?",
            (
                action_json,
                hashlib.sha256(action_json.encode()).hexdigest(),
                witness.transaction_hash,
            ),
        )

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="staged evidence"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize(
    ("field_name", "wrong_value"),
    (
        ("chain", "bsc"),
        ("pool_id", _HASH_D),
        ("position_manager", _ADDRESS_D),
    ),
)
def test_action_commit_rejects_wrong_run_identity(
    tmp_path: Path,
    field_name: str,
    wrong_value: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / f"{field_name}.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    action = replace(_action(witness), **{field_name: wrong_value})

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            with pytest.raises(CheckpointContractError, match="run identity"):
                run.commit_decoded_action_transaction(
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
                    position_keys=(_position_key(action),),
                )


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
    huge = 2**160 - 1
    action = replace(
        _action(witness),
        liquidity_delta=huge,
        amount0_raw=str(huge),
        amount1_raw=str(huge),
    )
    state = LedgerPositionState(_HASH_A, -10, 10, huge)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            run.commit_decoded_action_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        token_id=77,
                        state=state,
                        last_block_number=101,
                        last_log_index=11,
                        last_event_order=0,
                    ),
                ),
                state_deletes=(),
                actions=(action,),
                position_keys=(_position_key(action),),
            )
            assert run.load_decoder_state() == {77: state}
            assert run.load_decoded_actions() == (action,)
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute(
            "SELECT liquidity_after FROM decoder_token_state"
        ).fetchone()[0] == str(huge)


def test_precommit_failure_rolls_back_entire_action_discovery_unit(
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
            first = run.incomplete_action_chunks()[0]
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_before_commit)

            with pytest.raises(RuntimeError, match="precommit"):
                run.commit_action_chunk(first, (witness,))

            assert run.snapshot() == before
            assert run.incomplete_action_chunks()[0] == first
            assert run.action_candidate_hashes() == ()


def test_postcommit_failure_preserves_entire_action_discovery_unit(
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
            first = run.incomplete_action_chunks()[0]
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_after_commit)

            with pytest.raises(RuntimeError, match="postcommit"):
                run.commit_action_chunk(first, (witness,))

            monkeypatch.setattr(checkpoint_module, "_mutation_hook", lambda _stage: None)
            assert run.snapshot().generation == before.generation + 1
            assert run.incomplete_action_chunks() == (BlockRange(1, 105, 109),)
            assert run.action_candidate_hashes() == (_HASH_C,)


def test_precommit_failure_rolls_back_entire_action_decode_transaction(
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
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_before_commit)

            with pytest.raises(RuntimeError, match="decode precommit"):
                run.commit_decoded_action_transaction(
                    bundle,
                    headers=(
                        BlockHeader(100, _HASH_A, 1_000),
                        BlockHeader(101, _HASH_E, 1_010),
                    ),
                    resolutions=(PositionResolution(88, 100, True, resolved_state),),
                    state_upserts=(DecoderStateUpsert(77, state, 101, 11, 0),),
                    state_deletes=(),
                    actions=(action,),
                    position_keys=(_position_key(action),),
                )

            assert run.snapshot() == before
            assert run.undecoded_action_bundles() == (bundle,)
            assert run.load_header(101) is None
            assert run.load_position_resolution(88, 100) is None
            assert run.load_decoded_actions() == ()
            assert run.load_ownership_events() == ()
            assert run.load_decoder_state() == {}


def test_postcommit_failure_preserves_entire_action_decode_on_reopen(
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
    action = _action(witness)
    state = LedgerPositionState(_HASH_A, -10, 10, 100)
    resolved_state = LedgerPositionState(_HASH_A, -20, 20, 50)

    def fail_after_commit(stage: str) -> None:
        if stage == "after_commit":
            raise RuntimeError("injected decode postcommit crash")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            monkeypatch.setattr(checkpoint_module, "_mutation_hook", fail_after_commit)

            with pytest.raises(RuntimeError, match="decode postcommit"):
                run.commit_decoded_action_transaction(
                    bundle,
                    headers=(
                        BlockHeader(100, _HASH_A, 1_000),
                        BlockHeader(101, _HASH_E, 1_010),
                    ),
                    resolutions=(PositionResolution(88, 100, True, resolved_state),),
                    state_upserts=(DecoderStateUpsert(77, state, 101, 11, 0),),
                    state_deletes=(),
                    actions=(action,),
                    position_keys=(_position_key(action),),
                )

        monkeypatch.setattr(checkpoint_module, "_mutation_hook", lambda _stage: None)
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as reopened:
            assert reopened.undecoded_action_bundles() == ()
            assert reopened.load_header(101) == BlockHeader(101, _HASH_E, 1_010)
            assert reopened.load_position_resolution(88, 100) == PositionResolution(
                88,
                100,
                True,
                resolved_state,
            )
            assert reopened.load_decoded_actions() == (action,)
            assert reopened.load_ownership_events() == ()
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
            action = _action(witness)

            with pytest.raises(CheckpointContractError, match="conflicting hashes"):
                run.commit_decoded_action_transaction(
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
                    actions=(action,),
                    position_keys=(_position_key(action),),
                )

            assert run.snapshot() == before
            assert run.undecoded_action_bundles() == (bundle,)
            assert run.load_header(101) is None
            assert run.load_decoded_actions() == ()
            assert run.load_decoder_state() == {}


def test_replay_input_cannot_bind_before_ownership_phase(
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
            action = _action(witness)
            run.commit_decoded_action_transaction(
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
                position_keys=(_position_key(action),),
            )
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="phase"):
                run.commit_replay_input(_identity(paths.output).replay_input)

            assert run.snapshot() == before


def test_zero_action_run_reaches_build_only_after_replay_binding(
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
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(chunk, ())
            run.complete_phase("action_fetch")
            run.complete_phase("action_decode")
            run.freeze_action_token_set()
            for chunk in run.incomplete_transfer_chunks():
                run.commit_transfer_chunk(
                    chunk,
                    unfiltered_count=0,
                    unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                    relevant_witnesses=(),
                )
            run.complete_phase("relevant_transfer_fetch")
            run.complete_phase("relevant_transfer_decode")
            run.commit_replay_input(_identity(paths.output).replay_input)

            assert run.snapshot().phase == "price_replay"
            assert run.commit_action_price_bindings(()).phase == "build"


def test_decoded_action_round_trip_is_one_durable_unit(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    action = replace(_action(witness), lp_owner=_ADDRESS_CHECKSUM)
    state = LedgerPositionState(_HASH_A, -10, 10, 100)
    resolved_state = LedgerPositionState(_HASH_A, -20, 20, 50)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)
            before = run.snapshot()
            run.commit_decoded_action_transaction(
                bundle,
                headers=(
                    BlockHeader(100, _HASH_A, 1_000),
                    BlockHeader(101, _HASH_E, 1_010),
                ),
                resolutions=(PositionResolution(88, 100, True, resolved_state),),
                state_upserts=(DecoderStateUpsert(77, state, 101, 11, 0),),
                state_deletes=(),
                actions=(action,),
                position_keys=(_position_key(action),),
            )

            assert run.snapshot().generation == before.generation + 1
            assert run.load_decoded_actions() == (action,)
            assert run.load_ownership_events() == ()
            assert run.load_decoder_state() == {77: state}
            assert run.load_position_resolution(88, 100) == PositionResolution(
                88,
                100,
                True,
                resolved_state,
            )
            marker = run._connection.execute(
                "SELECT action_count FROM decoded_action_transactions"
            ).fetchone()

    assert tuple(marker) == (1,)


def test_later_decode_reuses_exact_cached_position_resolution(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    first_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    second_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_D,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=7,
        log_index=13,
    )
    first_action = _action(first_witness)
    resolved_state = LedgerPositionState(_HASH_A, -20, 20, 50)
    burn_action = replace(
        _action(second_witness),
        action_type="burn",
        token_id=88,
        tick_lower=-20,
        tick_upper=20,
        liquidity_delta=-10,
        amount0=Decimal("0"),
        amount1=Decimal("0"),
        amount0_raw="0",
        amount1_raw="0",
        amount0_actual=Decimal("0"),
        amount1_actual=Decimal("0"),
        amount0_attribution_source="not_applicable",
        amount1_attribution_source="not_applicable",
        amount_attribution_status="not_applicable",
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(
                    chunk,
                    tuple(
                        witness
                        for witness in (first_witness, second_witness)
                        if chunk.start_block <= witness.block_number <= chunk.end_block
                    ),
                )
            first_bundle = _bundle(first_witness)
            second_bundle = _bundle(second_witness)
            run.commit_action_bundle(first_bundle)
            run.commit_action_bundle(second_bundle)
            run.commit_decoded_action_transaction(
                first_bundle,
                headers=(
                    BlockHeader(100, _HASH_A, 1_000),
                    BlockHeader(101, _HASH_E, 1_010),
                ),
                resolutions=(
                    PositionResolution(88, 100, True, resolved_state),
                ),
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
                actions=(first_action,),
                position_keys=(_position_key(first_action),),
            )

        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.commit_decoded_action_transaction(
                second_bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        88,
                        LedgerPositionState(_HASH_A, -20, 20, 40),
                        101,
                        13,
                        0,
                    ),
                ),
                state_deletes=(),
                actions=(burn_action,),
                position_keys=(),
            )

            assert run.load_decoder_state()[88] == LedgerPositionState(
                _HASH_A,
                -20,
                20,
                40,
            )
            assert run.load_position_resolution(88, 100) == PositionResolution(
                88,
                100,
                True,
                resolved_state,
            )
            count = run._connection.execute(
                "SELECT COUNT(*) FROM position_resolutions WHERE token_id = '88'"
            ).fetchone()[0]

    assert count == 1


def test_transfer_scan_rejects_a_witness_for_an_unfrozen_token(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    action_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=11,
    )
    action = _action(action_witness)
    transfer = replace(
        _witness(
            source="position_transfer",
            transaction_hash=_HASH_D,
            block_number=102,
            block_hash=_HASH_D,
            transaction_index=6,
            log_index=12,
        ),
        topics=(
            _HASH_B,
            f"0x{'0' * 64}",
            f"0x{'0' * 24}{_ADDRESS_C[2:]}",
            f"0x{88:064x}",
        ),
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, action_witness)
            run.commit_decoded_action_transaction(
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
                position_keys=(_position_key(action),),
            )
            run.freeze_action_token_set()
            first = run.incomplete_transfer_chunks()[0]

            with pytest.raises(CheckpointContractError, match="relevant transfer"):
                run.commit_transfer_chunk(
                    first,
                    unfiltered_count=1,
                    unfiltered_sha256=hashlib.sha256(b"one transfer").hexdigest(),
                    relevant_witnesses=(transfer,),
                )


def test_decoder_state_update_and_burn_delete_share_action_cursor(
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
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (mint_witness, burn_witness))
            run.commit_action_chunk(second, ())
            burn_bundle = _bundle(burn_witness)
            mint_bundle = _bundle(mint_witness)
            run.commit_action_bundle(burn_bundle)
            run.commit_action_bundle(mint_bundle)
            mint_action = _action(mint_witness)
            run.commit_decoded_action_transaction(
                mint_bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(DecoderStateUpsert(77, position, 101, 11, 0),),
                state_deletes=(),
                actions=(mint_action,),
                position_keys=(_position_key(mint_action),),
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
            run.commit_decoded_action_transaction(
                burn_bundle,
                headers=(BlockHeader(102, _HASH_D, 1_020),),
                resolutions=(),
                state_upserts=(),
                state_deletes=(77,),
                actions=(burn_action,),
                position_keys=(),
            )

            assert run.load_decoder_state() == {}
            assert run.load_decoded_actions() == (mint_action, burn_action)
            assert run.snapshot().phase == "token_set_freeze"


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
            action = _action(witness)

            with pytest.raises(CheckpointContractError, match="decoder state"):
                run.commit_decoded_action_transaction(
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
                    actions=(action,),
                    position_keys=(_position_key(action),),
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
            action = _action(witness)

            with pytest.raises(CheckpointContractError, match="action-derived state"):
                run.commit_decoded_action_transaction(
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
                    actions=(action,),
                    position_keys=(_position_key(action),),
                )

            assert run.snapshot() == before
            assert run.undecoded_action_bundles() == (bundle,)
            assert run.load_decoder_state() == {}


def test_action_decode_rejects_liquidity_decrement_below_zero(
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
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (mint_witness, burn_witness))
            run.commit_action_chunk(second, ())
            mint_bundle = _bundle(mint_witness)
            burn_bundle = _bundle(burn_witness)
            run.commit_action_bundle(burn_bundle)
            run.commit_action_bundle(mint_bundle)
            mint = _action(mint_witness)
            run.commit_decoded_action_transaction(
                mint_bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(DecoderStateUpsert(77, position, 101, 11, 0),),
                state_deletes=(),
                actions=(mint,),
                position_keys=(_position_key(mint),),
            )
            burn = replace(
                _action(burn_witness),
                action_type="burn",
                liquidity_delta=-101,
            )

            with pytest.raises(CheckpointContractError, match="exceeds position liquidity"):
                run.commit_decoded_action_transaction(
                    burn_bundle,
                    headers=(BlockHeader(102, _HASH_D, 1_020),),
                    resolutions=(),
                    state_upserts=(),
                    state_deletes=(77,),
                    actions=(burn,),
                    position_keys=(),
                )

            assert run.load_decoder_state() == {77: position}
            assert run.undecoded_action_bundles() == (burn_bundle,)


def test_ownership_event_must_match_relevant_transfer_witness(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    action_witness = _witness(
        source="pool_modify",
        transaction_hash=_HASH_C,
        block_number=101,
        block_hash=_HASH_E,
        transaction_index=5,
        log_index=10,
    )
    transfer_witness = replace(
        _witness(
            source="position_transfer",
            transaction_hash=_HASH_C,
            block_number=101,
            block_hash=_HASH_E,
            transaction_index=5,
            log_index=11,
        ),
        topics=(
            _HASH_B,
            f"0x{'0' * 64}",
            f"0x{'0' * 24}{_ADDRESS_C[2:]}",
            f"0x{77:064x}",
        ),
    )
    action = _action(action_witness)
    bundle = _bundle(action_witness, transfer_witness)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (action_witness,))
            run.commit_action_chunk(second, ())
            run.commit_action_bundle(bundle)
            run.commit_decoded_action_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(
                    DecoderStateUpsert(
                        77,
                        LedgerPositionState(_HASH_A, -10, 10, 100),
                        101,
                        10,
                        0,
                    ),
                ),
                state_deletes=(),
                actions=(action,),
                position_keys=(_position_key(action),),
            )
            run.freeze_action_token_set()
            transfer_first, transfer_second = run.incomplete_transfer_chunks()
            run.commit_transfer_chunk(
                transfer_first,
                unfiltered_count=1,
                unfiltered_sha256=hashlib.sha256(b"transfer").hexdigest(),
                relevant_witnesses=(transfer_witness,),
            )
            run.commit_transfer_chunk(
                transfer_second,
                unfiltered_count=0,
                unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                relevant_witnesses=(),
            )
            run.commit_relevant_transfer_bundle(bundle)
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="reconcile exactly"):
                run.commit_decoded_relevant_transfer_transaction(
                    bundle,
                    owners=(OwnershipEvent(101, 12, 77, None, _ADDRESS_C, 0),),
                )

            assert run.snapshot() == before
            assert run.undecoded_relevant_transfer_bundles() == (bundle,)
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
    second_action = replace(
        first_action,
        action_type="collect",
        event_order=1,
        liquidity_delta=0,
    )

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            bundle = _stage_single_candidate_to_decode(run, witness)

            with pytest.raises(CheckpointContractError, match="one action"):
                run.commit_decoded_action_transaction(
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
                    position_keys=(_position_key(first_action),),
                )

            assert run.undecoded_action_bundles() == (bundle,)


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
            run.commit_decoded_action_transaction(
                bundle,
                headers=(BlockHeader(101, _HASH_E, 1_010),),
                resolutions=(),
                state_upserts=(DecoderStateUpsert(77, state, 101, 12, 1),),
                state_deletes=(),
                actions=(mint, collect),
                position_keys=(_position_key(mint),),
            )

            assert run.load_decoded_actions() == (mint, collect)
            assert run.load_decoder_state() == {77: state}


def test_position_resolution_found_flag_must_be_an_exact_bool(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    witness = _witness(
        source="pool_modify",
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
            action = _action(witness)

            with pytest.raises(CheckpointContractError, match="found flag"):
                run.commit_decoded_action_transaction(
                    bundle,
                    headers=(
                        BlockHeader(100, _HASH_A, 1_000),
                        BlockHeader(101, _HASH_E, 1_010),
                    ),
                    resolutions=(PositionResolution(77, 100, 1, state),),  # type: ignore[arg-type]
                    state_upserts=(),
                    state_deletes=(),
                    actions=(action,),
                    position_keys=(_position_key(action),),
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
                run.commit_decoded_action_transaction(
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
                    position_keys=(_position_key(action),),
                )

            assert run.snapshot() == before
            assert run.undecoded_action_bundles() == (bundle,)


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
                run.commit_action_chunk(BlockRange(2**63, 100, 104), ())

            assert run.snapshot() == before


def test_zero_action_build_inputs_and_publication_are_canonical(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            identity,
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(chunk, ())
            run.complete_phase("action_fetch")
            run.complete_phase("action_decode")
            run.freeze_action_token_set()
            for chunk in run.incomplete_transfer_chunks():
                run.commit_transfer_chunk(
                    chunk,
                    unfiltered_count=0,
                    unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                    relevant_witnesses=(),
                )
            run.complete_phase("relevant_transfer_fetch")
            run.complete_phase("relevant_transfer_decode")
            run.commit_replay_input(identity.replay_input)
            run.commit_action_price_bindings(())
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

    assert inputs.actions == ()
    assert inputs.action_transaction_hashes == ()
    assert inputs.frozen_token_ids == ()
    assert inputs.action_price_bindings == ()
    assert inputs.replay_input == identity.replay_input
    assert publication.state == "published"
    assert publication.expected_ledger_sha256 == "a" * 64
    assert publication.expected_sidecar_sha256 == "b" * 64
    assert published_snapshot.phase == "succeeded"
    assert published_snapshot.status == "succeeded"
    assert published_snapshot.output_published is True


def test_replay_input_bind_rejects_metadata_not_in_run_identity(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            identity,
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            for chunk in run.incomplete_action_chunks():
                run.commit_action_chunk(chunk, ())
            run.complete_phase("action_fetch")
            run.complete_phase("action_decode")
            run.freeze_action_token_set()
            for chunk in run.incomplete_transfer_chunks():
                run.commit_transfer_chunk(
                    chunk,
                    unfiltered_count=0,
                    unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                    relevant_witnesses=(),
                )
            run.complete_phase("relevant_transfer_fetch")
            run.complete_phase("relevant_transfer_decode")
            before = run.snapshot()

            with pytest.raises(CheckpointContractError, match="run identity"):
                run.commit_replay_input(replace(identity.replay_input, sha256="f" * 64))

            assert run.snapshot() == before
            with pytest.raises(CheckpointContractError, match="not committed"):
                run.load_replay_input()


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
        "user_version": 2,
    }
    assert tables == (
        "action_bundle_fetches",
        "action_chunks",
        "action_price_bindings",
        "action_witnesses",
        "chain_blocks",
        "decoded_action_transactions",
        "decoded_actions",
        "decoded_relevant_transfer_transactions",
        "decoder_token_state",
        "frozen_token_ids",
        "frozen_token_set",
        "ownership_events",
        "phase_state",
        "position_resolutions",
        "publication_state",
        "relevant_transfer_bundle_fetches",
        "relevant_transfer_witnesses",
        "replay_input",
        "run_identity",
        "run_state",
        "schema_meta",
        "token_position_keys",
        "transaction_bundles",
        "transfer_chunks",
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
                connection.execute("PRAGMA user_version = 1")
            elif drift == "application_id":
                connection.execute("PRAGMA application_id = 1")
            else:
                connection.execute("DROP INDEX phase_state_status")
        with pytest.raises(CheckpointContractError, match="schema"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize(
    "corruption", ("action_partition", "transfer_partition", "count", "phase")
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
            if corruption == "action_partition":
                connection.execute(
                    "UPDATE action_chunks SET end_block = 105 WHERE chunk_index = 0"
                )
            elif corruption == "transfer_partition":
                connection.execute(
                    "UPDATE transfer_chunks SET start_block = 106 WHERE chunk_index = 1"
                )
            elif corruption == "count":
                connection.execute(
                    """
                    UPDATE action_chunks
                    SET status = 'completed', witness_count = 1,
                        completed_generation = 1
                    WHERE chunk_index = 0
                    """
                )
            else:
                connection.execute(
                    "UPDATE phase_state SET status = 'running' WHERE phase = 'action_discovery'"
                )
        with pytest.raises(CheckpointContractError, match="invalid|checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_action_witness_moved_outside_its_chunk(
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
            run.commit_action_chunk(
                run.incomplete_action_chunks()[0],
                (witness,),
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
                UPDATE action_witnesses
                SET block_number = 106, block_hash = ?
                WHERE transaction_hash = ?
                """,
                (_HASH_D, _HASH_C),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_action_bundle_payload_digest_tamper(tmp_path: Path) -> None:
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
                "UPDATE transaction_bundles SET payload_sha256 = ?",
                ("f" * 64,),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_nonprefix_decoded_action_transaction(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
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
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_action_chunks()
            run.commit_action_chunk(first, (earlier, later))
            run.commit_action_chunk(second, ())
            run.commit_action_bundle(_bundle(later))
            run.commit_action_bundle(_bundle(earlier))
            generation = run.snapshot().generation
        empty_state_digest = hashlib.sha256(b"[]").hexdigest()
        with sqlite3.connect(paths.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO decoded_action_transactions VALUES (?, ?, ?, ?, 0, ?, ?)
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
                WHERE phase = 'action_decode'
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
            generation = run.snapshot().generation
        with sqlite3.connect(paths.database) as connection:
            connection.execute("DELETE FROM transaction_bundles")
            connection.execute(
                """
                UPDATE phase_state
                SET completed = 0, total = 0, updated_generation = ?
                WHERE phase IN ('action_fetch', 'action_decode')
                """,
                (generation,),
            )

        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_resume_rejects_tampered_action_price_binding(tmp_path: Path) -> None:
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
            action = _action(witness)
            run.commit_decoded_action_transaction(
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
                position_keys=(_position_key(action),),
            )
            run.freeze_action_token_set()
            for chunk in run.incomplete_transfer_chunks():
                run.commit_transfer_chunk(
                    chunk,
                    unfiltered_count=0,
                    unfiltered_sha256=hashlib.sha256(b"[]").hexdigest(),
                    relevant_witnesses=(),
                )
            run.complete_phase("relevant_transfer_fetch")
            run.complete_phase("relevant_transfer_decode")
            run.commit_replay_input(identity.replay_input)
            run.commit_action_price_bindings(
                (ActionPriceBinding(101, 11, 0, 2**96, 0, "prior_event"),)
            )
        with sqlite3.connect(paths.database) as connection:
            connection.execute(
                "UPDATE action_price_bindings SET event_time_sqrt_price_x96 = '0'"
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


def test_safe_failure_never_interpolates_exception_text() -> None:
    secret = "current-provider-secret"

    line = render_safe_failure(
        pool="uni-base",
        phase="action_fetch",
        error_code="rpc_error",
        exception=RuntimeError(f"Authorization: Bearer {secret}"),
    )

    assert secret not in line
    assert "RuntimeError" not in line
    assert line == "[lp-ledger][uni-base] phase=action_fetch error=rpc_error"


def test_progress_rate_and_eta_are_phase_local(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    started = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            baseline = progress_from_snapshot(
                run.snapshot(),
                started,
                monotonic_seconds=100.0,
                previous=None,
            )
            first, _second = run.incomplete_action_chunks()
            snapshot = run.commit_action_chunk(first, ())
            progress = progress_from_snapshot(
                snapshot,
                started + timedelta(seconds=5),
                monotonic_seconds=105.0,
                previous=baseline,
            )

    assert progress.rate_per_second == "0.2"
    assert progress.eta_seconds == 5
    assert progress.updated_at == "2026-07-22T00:00:05.000Z"
    assert progress.last_durable == (
        ("chunk_index", "0"),
        ("durable_end_block", "104"),
    )


def test_progress_rate_resets_on_attempt_phase_or_terminal_change(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            before = progress_from_snapshot(
                run.snapshot(), now, monotonic_seconds=10.0, previous=None
            )
            after = progress_from_snapshot(
                run.complete_phase("preflight"),
                now + timedelta(seconds=5),
                monotonic_seconds=15.0,
                previous=before,
            )

    assert before.rate_per_second is None
    assert after.rate_per_second is None
    assert after.eta_seconds is None


@pytest.mark.parametrize(
    ("elapsed", "durable_delta", "expected"),
    (
        (4.9, 25, False),
        (5.0, 24, False),
        (5.0, 25, True),
        (29.9, 0, False),
        (30.0, 0, True),
    ),
)
def test_progress_write_schedule_is_bounded(
    tmp_path: Path,
    elapsed: float,
    durable_delta: int,
    expected: bool,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            previous = progress_from_snapshot(
                run.snapshot(), now, monotonic_seconds=100.0, previous=None
            )
            current = replace(
                previous,
                phases=tuple(
                    replace(phase, completed=phase.completed + durable_delta)
                    if phase.phase == previous.phase
                    else phase
                    for phase in previous.phases
                ),
                sample_monotonic_seconds=100.0 + elapsed,
            )

    assert should_write_progress(previous, current) is expected


def test_atomic_progress_replace_preserves_prior_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            first = progress_from_snapshot(
                run.snapshot(), now, monotonic_seconds=10.0, previous=None
            )
            write_progress_atomically(paths, first)
            prior = paths.progress.read_bytes()
            second = progress_from_snapshot(
                run.complete_phase("preflight"),
                now + timedelta(seconds=1),
                monotonic_seconds=11.0,
                previous=first,
            )

            def fail_replace(_source: object, _destination: object) -> None:
                raise OSError("injected replace failure")

            monkeypatch.setattr(checkpoint_module.os, "replace", fail_replace)
            with pytest.raises(CheckpointContractError, match="progress"):
                write_progress_atomically(paths, second)

    assert paths.progress.read_bytes() == prior
    assert stat.S_IMODE(paths.progress.stat().st_mode) == 0o600
    assert not tuple(tmp_path.glob("*.progress.json.*.tmp"))


def test_progress_writer_rejects_bool_substitutes_for_typed_values(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            progress = progress_from_snapshot(
                run.snapshot(),
                now,
                monotonic_seconds=10.0,
                previous=None,
            )

    invalid_values = (
        replace(progress, attempt_number=True),
        replace(progress, start_block=True),
        replace(progress, action_count=True),
        replace(progress, output_published=0),
        replace(
            progress,
            phases=(replace(progress.phases[0], completed=True), *progress.phases[1:]),
        ),
        replace(progress, eta_seconds=True),
    )
    for invalid in invalid_values:
        with pytest.raises(CheckpointContractError, match="progress"):
            write_progress_atomically(paths, invalid)


def test_status_uses_sqlite_generation_and_rejects_stale_progress(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            first, second = run.incomplete_action_chunks()
            stale_snapshot = run.commit_action_chunk(first, ())
            stale = replace(
                progress_from_snapshot(stale_snapshot, now, monotonic_seconds=10.0, previous=None),
                rate_per_second="1",
                eta_seconds=1,
            )
            write_progress_atomically(paths, stale)
            current = run.commit_action_chunk(second, ())

    status = read_export_status(paths)

    assert current.generation == 4
    assert status.checkpoint_generation == 4
    assert status.progress_state == "progress_unavailable"
    assert status.rate_per_second is None
    assert status.eta_seconds is None


def test_running_checkpoint_with_free_lock_reports_interrupted(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ):
            pass

    status = read_export_status(paths)

    assert status.state == "interrupted"
    assert status.lock_held is False
    assert status.locally_compatible is True
    assert status.resumable is True


def test_held_lock_reports_active_even_with_an_old_checkpoint_update(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ):
            status = read_export_status(paths)

    assert status.state == "active"
    assert status.lock_held is True


@pytest.mark.parametrize("mutation", ("run", "attempt", "generation"))
def test_status_rejects_mismatched_progress(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            progress = replace(
                progress_from_snapshot(run.snapshot(), now, monotonic_seconds=10.0, previous=None),
                rate_per_second="1",
                eta_seconds=1,
            )
            if mutation == "run":
                progress = replace(progress, run_id="00000000-0000-0000-0000-000000000000")
            elif mutation == "attempt":
                progress = replace(progress, attempt_number=progress.attempt_number + 1)
            else:
                assert progress.checkpoint_generation is not None
                progress = replace(
                    progress,
                    checkpoint_generation=progress.checkpoint_generation + 1,
                )
            write_progress_atomically(paths, progress)

    status = read_export_status(paths)

    assert status.progress_state == "progress_unavailable"
    assert status.rate_per_second is None


def test_status_reader_handles_a_live_wal_without_mutating_it(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            run.complete_phase("preflight")
            before = {
                path: path.stat().st_mtime_ns
                for path in (paths.database, paths.wal, paths.shm)
                if path.exists()
            }
            status = read_export_status(paths)
            after = {path: path.stat().st_mtime_ns for path in before if path.exists()}

    assert status.state == "active"
    assert after == before


@pytest.mark.parametrize("damage", ("version", "bytes"))
def test_corrupt_or_incompatible_checkpoint_status_is_blocked(
    tmp_path: Path,
    damage: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ):
            pass
    if damage == "version":
        with sqlite3.connect(paths.database) as connection:
            connection.execute("PRAGMA user_version = 1")
    else:
        paths.database.write_bytes(b"not a sqlite database")
        paths.database.chmod(0o600)

    status = read_export_status(paths)

    assert status.state == "blocked"
    assert status.locally_compatible is False
    assert status.resumable is False


def test_progress_only_fixture_is_non_resumable(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            progress = replace(
                progress_from_snapshot(run.snapshot(), now, monotonic_seconds=10.0, previous=None),
                checkpoint_generation=None,
                mode="fixture",
            )
            write_progress_atomically(paths, progress)
    for path in (paths.database, paths.wal, paths.shm):
        path.unlink(missing_ok=True)

    status = read_export_status(paths)

    assert status.state == "non_resumable"
    assert status.resumable is False
    assert status.progress_state == "current"


def test_malformed_progress_without_checkpoint_is_unknown(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    paths.progress.write_text('{"schema_version":1}', encoding="utf-8")

    status = read_export_status(paths)

    assert status.state == "unknown"
    assert status.progress_state == "progress_unavailable"


def test_progress_only_status_rejects_another_outputs_progress(tmp_path: Path) -> None:
    source_paths = CheckpointPaths.from_output(tmp_path / "source.csv")
    target_paths = CheckpointPaths.from_output(tmp_path / "target.csv")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with acquire_run_lock(source_paths):
        with LPLedgerCheckpoint.create_or_resume(
            source_paths,
            _identity(source_paths.output),
            fresh=False,
        ) as run:
            progress = replace(
                progress_from_snapshot(run.snapshot(), now, monotonic_seconds=10.0, previous=None),
                checkpoint_generation=None,
                mode="fixture",
            )
            write_progress_atomically(source_paths, progress)
    target_paths.progress.write_bytes(source_paths.progress.read_bytes())
    target_paths.progress.chmod(0o600)

    status = read_export_status(target_paths)

    assert status.state == "unknown"
    assert status.progress_state == "progress_unavailable"


@pytest.mark.parametrize("error_code", tuple(ErrorCode.__args__))
def test_attempt_failure_is_durable_and_resumable(
    tmp_path: Path,
    error_code: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            before = run.snapshot()
            failed = run.mark_attempt_failed(error_code)  # type: ignore[arg-type]
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as resumed_run:
            resumed = resumed_run.snapshot()

    expected_status = "interrupted" if error_code == "interrupted" else "failed"
    assert failed.status == expected_status
    assert failed.phase == before.phase
    assert failed.generation == before.generation + 1
    assert failed.error_code == error_code
    assert resumed.status == "running"
    assert resumed.phase == before.phase
    assert resumed.attempt_number == before.attempt_number + 1


def test_status_cli_reports_base_and_bsc_in_human_form(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research.scripts.report_lp_ledger_export_status import main

    base_paths, bsc_paths = _create_base_and_bsc_checkpoints(tmp_path)

    assert main([os.fspath(base_paths.output), os.fspath(bsc_paths.output)]) == 0

    output = capsys.readouterr().out
    assert "OUTPUT" in output
    assert "POOL" in output
    assert "ACTION_TX" in output
    assert "TRANSFER_LOGS" in output
    assert "CANDIDATES" not in output
    assert "uni-base" in output
    assert "uni-bsc" in output
    assert output.index("uni-base") < output.index("uni-bsc")


def test_status_cli_json_preserves_requested_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research.scripts.report_lp_ledger_export_status import main

    base_paths, bsc_paths = _create_base_and_bsc_checkpoints(tmp_path)

    assert (
        main(
            [
                "--json",
                os.fspath(bsc_paths.output),
                os.fspath(base_paths.output),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert [item["pool"] for item in payload["exports"]] == ["uni-bsc", "uni-base"]
    assert all(item["state"] == "interrupted" for item in payload["exports"])


def test_status_cli_is_directly_executable_outside_the_repository(
    tmp_path: Path,
) -> None:
    base_paths, _bsc_paths = _create_base_and_bsc_checkpoints(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "report_lp_ledger_export_status.py"

    result = subprocess.run(
        [sys.executable, os.fspath(script), "--json", os.fspath(base_paths.output)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["exports"][0]["pool"] == "uni-base"


def _create_base_and_bsc_checkpoints(
    tmp_path: Path,
) -> tuple[CheckpointPaths, CheckpointPaths]:
    base_paths = CheckpointPaths.from_output(tmp_path / "base.csv")
    bsc_paths = CheckpointPaths.from_output(tmp_path / "bsc.csv")
    identities = (
        (base_paths, _identity(base_paths.output)),
        (
            bsc_paths,
            replace(
                (bsc_identity := _identity(bsc_paths.output)),
                pool="uni-bsc",
                chain="bsc",
                chain_id=56,
                replay_input=replace(bsc_identity.replay_input, chain="bsc"),
            ),
        ),
    )
    for paths, identity in identities:
        with acquire_run_lock(paths):
            with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
                pass
    return base_paths, bsc_paths


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
        topics=(
            (_HASH_A, _HASH_A)
            if source == "pool_modify"
            else (_HASH_B, _HASH_A, _HASH_B, _HASH_C)
        ),
        data="0x",
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
                    "data": value.data,
                    "logIndex": str(value.log_index),
                    "topics": list(value.topics),
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
        raise AssertionError("action staging requires at least one witness")
    if any(witness.source != "pool_modify" for witness in witnesses):
        raise AssertionError("action staging accepts only pool witnesses")
    run.complete_phase("preflight")
    for chunk in run.incomplete_action_chunks():
        run.commit_action_chunk(
            chunk,
            tuple(
                witness
                for witness in witnesses
                if chunk.start_block <= witness.block_number <= chunk.end_block
            ),
        )
    bundle = _bundle(witnesses[0], *witnesses[1:])
    run.commit_action_bundle(bundle)
    return bundle


def _position_key(action: DecodedLiquidityAction) -> checkpoint_module.PositionKeyMapping:
    if action.tick_lower is None or action.tick_upper is None:
        raise AssertionError("mint test action requires ticks")
    return checkpoint_module.PositionKeyMapping(
        token_id=action.token_id,
        pool_id=action.pool_id,
        tick_lower=action.tick_lower,
        tick_upper=action.tick_upper,
        salt=_HASH_D,
        mint_block_number=action.block_number,
        mint_log_index=action.log_index,
        mint_event_order=action.event_order,
    )


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
        schema_version=2,
        exporter_version="lp-ledger-checkpoint-v2",
        verification_mode="rpc_verified",
        pool="uni-base",
        chain="base",
        chain_id=8453,
        pool_id=_HASH_A,
        pool_manager=_ADDRESS_A,
        position_manager=_ADDRESS_B,
        wrapper_entrypoint=_ADDRESS_C,
        token0_address=_ADDRESS_D,
        token1_address=_ADDRESS_E,
        token0_decimals=6,
        token1_decimals=18,
        fee_rate="0.0005",
        invert_price=False,
        start_block=100,
        end_block=109,
        chunk_size=5,
        action_topic=_HASH_A,
        transfer_topic=_HASH_B,
        replay_input=checkpoint_module.ReplayInputEvidence(
            path=os.path.abspath(output.with_name("replay.csv")),
            sha256="b" * 64,
            byte_length=1_000,
            row_count=10,
            header_sha256="c" * 64,
            parser_version="pool-history-replay-v1",
            price_semantics_sha256="d" * 64,
            chain="base",
            pool_id=_HASH_A,
            first_block=100,
            last_block=109,
            first_timestamp_ms=1_000,
            last_timestamp_ms=2_000,
            price_event_count=4,
            price_events_sha256="e" * 64,
        ),
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
        connection.execute(
            "UPDATE action_chunks SET status = 'completed', completed_generation = 1"
        )
        connection.execute(
            """
            UPDATE transfer_chunks
            SET status = 'completed', unfiltered_sha256 = ?, completed_generation = 1
            """,
            (hashlib.sha256(b"[]").hexdigest(),),
        )
        connection.execute(
            "INSERT INTO frozen_token_set VALUES (1, 0, ?, 1)",
            (hashlib.sha256(b"[]").hexdigest(),),
        )
        identity = json.loads(
            connection.execute(
                "SELECT canonical_json FROM run_identity WHERE singleton = 1"
            ).fetchone()[0]
        )
        replay = identity["replay_input"]
        connection.execute(
            """
            INSERT INTO replay_input VALUES (
                1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1
            )
            """,
            (
                replay["path"],
                replay["sha256"],
                replay["byte_length"],
                replay["row_count"],
                replay["header_sha256"],
                replay["parser_version"],
                replay["price_semantics_sha256"],
                replay["chain"],
                replay["pool_id"],
                replay["first_block"],
                replay["last_block"],
                replay["first_timestamp_ms"],
                replay["last_timestamp_ms"],
                replay["price_event_count"],
                replay["price_events_sha256"],
            ),
        )
        phase_counts = {
            "preflight": (1, 1),
            "action_discovery": (2, 2),
            "action_fetch": (0, 0),
            "action_decode": (0, 0),
            "token_set_freeze": (0, 0),
            "full_transfer_scan": (2, 2),
            "relevant_transfer_fetch": (0, 0),
            "relevant_transfer_decode": (0, 0),
            "replay_input_bind": (1, 1),
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
