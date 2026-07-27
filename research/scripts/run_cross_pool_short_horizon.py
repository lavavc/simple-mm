"""Run and atomically publish the frozen short-horizon response extension."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import platform
import shutil
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import matplotlib

from engine.web3_utils import redact_rpc_credentials
from research.cross_pool.contracts import CrossPoolContractError, PoolEvent
from research.cross_pool.io import load_pool_events
from research.cross_pool.short_horizon import (
    ShortHorizonStudy,
    measure_short_horizon_direction,
)
from research.cross_pool.short_horizon_inference import (
    ShortHorizonInference,
    infer_short_horizon,
)
from research.cross_pool.short_horizon_manifest import (
    BASE_FEATURES_SHA256,
    BSC_FEATURES_SHA256,
    GIRUM_NOTE_SHA256,
    PARENT_MANIFEST_SHA256,
    GeneratedManifestInput,
    InputIdentity,
    QaBlockedManifestInput,
    QaReasonCode,
    ShortHorizonProvenance,
    SourceDependencyIdentity,
    build_generated_short_horizon_manifest,
    build_qa_blocked_short_horizon_manifest,
    capture_short_horizon_source,
)
from research.cross_pool.short_horizon_parent import (
    PARENT_EVENT_STUDY_SHA256,
    validate_frozen_parent_anchor,
)
from research.cross_pool.short_horizon_publication import (
    PublicationMode,
    classify_short_horizon_output,
    output_lock,
    publish_or_verify_short_horizon_candidate,
    write_short_horizon_candidate,
)
from research.cross_pool.short_horizon_reporting import (
    ShortHorizonEvidence,
    render_short_horizon_artifacts,
    validate_rendered_short_horizon_artifacts,
)


class ShortHorizonCliError(RuntimeError):
    """Raised for argument errors without printing unredacted parser state."""


class _FailClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ShortHorizonCliError(message)


@dataclass(frozen=True)
class RunArguments:
    base_features: Path
    bsc_features: Path
    parent_manifest: Path
    girum_note: Path
    out_dir: Path


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    identity: InputIdentity


@dataclass(frozen=True)
class _Snapshots:
    base_features: _Snapshot | None
    bsc_features: _Snapshot | None
    parent_manifest: _Snapshot
    parent_event_study: _Snapshot
    girum_note: _Snapshot | None


def build_parser() -> argparse.ArgumentParser:
    parser = _FailClosedArgumentParser(
        description="Run the frozen Base/BSC short-horizon response study",
    )
    parser.add_argument("--base-features", type=Path, required=True)
    parser.add_argument("--bsc-features", type=Path, required=True)
    parser.add_argument("--parent-manifest", type=Path, required=True)
    parser.add_argument("--girum-note", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        namespace = build_parser().parse_args(raw_arguments)
        arguments = RunArguments(
            base_features=cast(Path, namespace.base_features),
            bsc_features=cast(Path, namespace.bsc_features),
            parent_manifest=cast(Path, namespace.parent_manifest),
            girum_note=cast(Path, namespace.girum_note),
            out_dir=cast(Path, namespace.out_dir),
        )
        return run(arguments)
    except Exception as exc:
        message = redact_rpc_credentials(
            str(exc),
            sensitive_values=tuple(value for value in raw_arguments if value),
        )
        print(f"short-horizon run failed: {message}", file=sys.stderr)
        return 1


def run(arguments: RunArguments) -> int:
    with output_lock(arguments.out_dir):
        mode = classify_short_horizon_output(arguments.out_dir)
        return _run_locked(arguments, mode)


def _run_locked(arguments: RunArguments, mode: PublicationMode) -> int:
    source = capture_short_horizon_source(_REPOSITORY_ROOT)
    runtime = _runtime_identity()
    stage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{arguments.out_dir.name}.stage.",
            dir=arguments.out_dir.parent,
        )
    )
    try:
        snapshots = _snapshot_inputs(arguments, stage_root / "inputs")
        provenance = _provenance(snapshots, source.code_commit, source.dependencies, runtime)
        input_reasons = _input_reason_codes(snapshots)
        candidate_dir = stage_root / "candidate"
        if input_reasons:
            return _publish_blocked(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                input_reasons,
            )

        assert snapshots.base_features is not None
        assert snapshots.bsc_features is not None
        assert snapshots.girum_note is not None
        try:
            base_events = load_pool_events(
                snapshots.base_features.path,
                expected_pool="uni-base",
            )
            bsc_events = load_pool_events(
                snapshots.bsc_features.path,
                expected_pool="uni-bsc",
            )
            studies = _measure(base_events, bsc_events)
        except CrossPoolContractError:
            return _publish_blocked(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                ("measurement_failure",),
            )

        try:
            validate_frozen_parent_anchor(
                snapshots.parent_manifest.path.read_bytes(),
                snapshots.parent_event_study.path.read_bytes(),
                studies,
            )
        except (CrossPoolContractError, OSError):
            return _publish_blocked(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                ("parent_anchor_mismatch",),
            )

        try:
            inferences = _infer(studies)
        except CrossPoolContractError:
            return _publish_blocked(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                ("inference_failure",),
            )

        evidence = ShortHorizonEvidence(
            studies=studies,
            inferences=inferences,
            parent_anchor_reconciled=True,
        )
        try:
            artifacts = render_short_horizon_artifacts(evidence)
            validate_rendered_short_horizon_artifacts(artifacts, evidence)
            manifest = build_generated_short_horizon_manifest(
                GeneratedManifestInput(
                    provenance=provenance,
                    qa_counts=evidence.qa_counts,
                    inferences=inferences,
                    artifacts=artifacts,
                    parent_anchor_reconciled=True,
                )
            )
        except CrossPoolContractError:
            return _publish_blocked(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                ("reporting_failure",),
            )
        write_short_horizon_candidate(candidate_dir, artifacts, manifest)
        publish_or_verify_short_horizon_candidate(
            arguments.out_dir,
            candidate_dir,
            mode=mode,
        )
        return 0
    finally:
        _remove_private_stage(stage_root)


def _measure(
    base_events: tuple[PoolEvent, ...],
    bsc_events: tuple[PoolEvent, ...],
) -> tuple[ShortHorizonStudy, ...]:
    return (
        measure_short_horizon_direction(bsc_events, base_events),
        measure_short_horizon_direction(base_events, bsc_events),
    )


def _infer(
    studies: tuple[ShortHorizonStudy, ...],
) -> tuple[ShortHorizonInference, ...]:
    return tuple(
        infer_short_horizon(study, exclude_same_timestamp=exclude)
        for study in studies
        for exclude in (False, True)
    )


def _snapshot_inputs(arguments: RunArguments, inputs_dir: Path) -> _Snapshots:
    inputs_dir.mkdir(mode=0o700)
    parent = _snapshot_regular_file(
        arguments.parent_manifest,
        inputs_dir / "parent_manifest.json",
    )
    if parent is None or parent.identity.sha256 != PARENT_MANIFEST_SHA256:
        raise CrossPoolContractError("parent manifest is missing or does not match the freeze")
    parent_event = _snapshot_regular_file(
        arguments.parent_manifest.with_name("event_study.csv"),
        inputs_dir / "parent_event_study.csv",
    )
    if parent_event is None or parent_event.identity.sha256 != PARENT_EVENT_STUDY_SHA256:
        raise CrossPoolContractError("parent event study is missing or does not match the freeze")
    return _Snapshots(
        base_features=_snapshot_regular_file(
            arguments.base_features,
            inputs_dir / "base_features.csv",
        ),
        bsc_features=_snapshot_regular_file(
            arguments.bsc_features,
            inputs_dir / "bsc_features.csv",
        ),
        parent_manifest=parent,
        parent_event_study=parent_event,
        girum_note=_snapshot_regular_file(
            arguments.girum_note,
            inputs_dir / "girum_note.md",
        ),
    )


def _snapshot_regular_file(source: Path, destination: Path) -> _Snapshot | None:
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CrossPoolContractError("input snapshot source could not be opened") from exc
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(source_descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    raw = b"".join(chunks)
    if identity_before != identity_after or len(raw) != before.st_size:
        raise CrossPoolContractError("input snapshot source changed while being read")
    destination_created = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        destination_created = True
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("input snapshot write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(destination.parent)
    except OSError as exc:
        if destination_created:
            try:
                destination.unlink()
            except OSError as cleanup_exc:
                raise CrossPoolContractError(
                    "private input snapshot write and cleanup both failed"
                ) from cleanup_exc
        raise CrossPoolContractError("private input snapshot could not be written") from exc
    return _Snapshot(
        path=destination,
        identity=InputIdentity(
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_count=len(raw),
        ),
    )


def _provenance(
    snapshots: _Snapshots,
    code_commit: str,
    dependencies: tuple[SourceDependencyIdentity, ...],
    runtime: tuple[tuple[str, str], ...],
) -> ShortHorizonProvenance:
    return ShortHorizonProvenance(
        parent_manifest=snapshots.parent_manifest.identity,
        base_features=(
            None if snapshots.base_features is None else snapshots.base_features.identity
        ),
        bsc_features=(None if snapshots.bsc_features is None else snapshots.bsc_features.identity),
        girum_note=None if snapshots.girum_note is None else snapshots.girum_note.identity,
        code_commit=code_commit,
        source_dependencies=dependencies,
        runtime=runtime,
    )


def _input_reason_codes(snapshots: _Snapshots) -> tuple[QaReasonCode, ...]:
    reasons: list[QaReasonCode] = []
    for snapshot, expected, missing, mismatch in (
        (
            snapshots.base_features,
            BASE_FEATURES_SHA256,
            "missing_base_features",
            "base_features_hash_mismatch",
        ),
        (
            snapshots.bsc_features,
            BSC_FEATURES_SHA256,
            "missing_bsc_features",
            "bsc_features_hash_mismatch",
        ),
        (
            snapshots.girum_note,
            GIRUM_NOTE_SHA256,
            "missing_girum_note",
            "girum_note_hash_mismatch",
        ),
    ):
        if snapshot is None:
            reasons.append(cast(QaReasonCode, missing))
        elif snapshot.identity.sha256 != expected:
            reasons.append(cast(QaReasonCode, mismatch))
    return tuple(sorted(reasons))


def _publish_blocked(
    out_dir: Path,
    candidate_dir: Path,
    mode: PublicationMode,
    provenance: ShortHorizonProvenance,
    reasons: tuple[QaReasonCode, ...],
) -> int:
    manifest = build_qa_blocked_short_horizon_manifest(
        QaBlockedManifestInput(
            provenance=provenance,
            reason_codes=tuple(sorted(set(reasons))),
        )
    )
    write_short_horizon_candidate(candidate_dir, (), manifest)
    publish_or_verify_short_horizon_candidate(out_dir, candidate_dir, mode=mode)
    return 2


def _runtime_identity() -> tuple[tuple[str, str], ...]:
    font_path = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
    return tuple(
        sorted(
            (
                ("dejavu_sans_sha256", hashlib.sha256(font_path.read_bytes()).hexdigest()),
                ("jsonschema", importlib.metadata.version("jsonschema")),
                ("matplotlib", importlib.metadata.version("matplotlib")),
                ("numpy", importlib.metadata.version("numpy")),
                ("python", platform.python_version()),
            )
        )
    )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_private_stage(stage_root: Path) -> None:
    try:
        shutil.rmtree(stage_root)
    except OSError as exc:
        raise CrossPoolContractError(
            "private short-horizon input stage could not be removed"
        ) from exc


if __name__ == "__main__":
    raise SystemExit(main())
