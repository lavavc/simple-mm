"""Run and atomically publish the frozen cross-pool lead-lag analysis."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from engine.web3_utils import redact_rpc_credentials
from research.backtester.lp_ledger_attribution import (
    VerifiedLedgerCoverageEvidence,
    ledger_coverage_path,
    load_ledger_coverage,
    load_verified_ledger_attribution_rows,
)
from research.cross_pool.contracts import (
    CrossPoolContractError,
    PanelConfig,
    PoolEvent,
    PoolName,
)
from research.cross_pool.io import load_pool_events
from research.cross_pool.manifest import (
    InputFileProvenance,
    PredictiveSensitivityInput,
    QaBlockedManifestInput,
    QaReasonCode,
    RunConfiguration,
    RunProvenance,
    RuntimeEnvironment,
    StatisticalManifestInput,
    build_article_manifest,
    build_qa_blocked_manifest,
)
from research.cross_pool.market_structure import (
    MarketStructureAnalysis,
    ReplayStreamEvidence,
    analyze_market_structure,
    inspect_replay_stream,
    replay_end_block_at_or_before,
)
from research.cross_pool.panel import build_causal_panel
from research.cross_pool.pipeline import analyze_cross_pool
from research.cross_pool.provenance import (
    capture_git_state,
    collect_runtime_environment,
    csv_file_provenance,
    sha256_file,
    snapshot_regular_file,
)
from research.cross_pool.publication import (
    PublicationMode,
    classify_output,
    output_lock,
    publish_or_verify_candidate,
    write_sealed_candidate,
)
from research.cross_pool.reporting import (
    RenderedArtifact,
    causal_audit_counts_payload,
    dtw_null_summary_payload,
    event_study_manifest_payload,
    market_structure_manifest_payload,
    render_statistical_artifacts,
)

_PANEL_HORIZONS_MS = (900_000, 3_600_000, 14_400_000)


@dataclass(frozen=True)
class RunArguments:
    base_features: Path
    bsc_features: Path
    base_replay: Path
    bsc_replay: Path
    base_ledger: Path
    bsc_ledger: Path
    out_dir: Path


@dataclass(frozen=True)
class SnapshotPaths:
    base_features: Path
    bsc_features: Path
    base_replay: Path
    bsc_replay: Path
    base_ledger: Path
    bsc_ledger: Path


@dataclass(frozen=True)
class CapturedInputs:
    base_events: tuple[PoolEvent, ...] | None
    bsc_events: tuple[PoolEvent, ...] | None
    base_replay_evidence: ReplayStreamEvidence | None
    bsc_replay_evidence: ReplayStreamEvidence | None
    base_features_provenance: InputFileProvenance | None
    bsc_features_provenance: InputFileProvenance | None
    base_replay_provenance: InputFileProvenance | None
    bsc_replay_provenance: InputFileProvenance | None
    base_ledger_provenance: InputFileProvenance | None
    bsc_ledger_provenance: InputFileProvenance | None
    reason_codes: tuple[QaReasonCode, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the frozen Base/BSC cross-pool lead-lag analysis",
    )
    parser.add_argument("--base-features", type=Path, required=True)
    parser.add_argument("--bsc-features", type=Path, required=True)
    parser.add_argument("--base-replay", type=Path, required=True)
    parser.add_argument("--bsc-replay", type=Path, required=True)
    parser.add_argument("--base-ledger", type=Path, required=True)
    parser.add_argument("--bsc-ledger", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    arguments = RunArguments(
        base_features=cast(Path, namespace.base_features),
        bsc_features=cast(Path, namespace.bsc_features),
        base_replay=cast(Path, namespace.base_replay),
        bsc_replay=cast(Path, namespace.bsc_replay),
        base_ledger=cast(Path, namespace.base_ledger),
        bsc_ledger=cast(Path, namespace.bsc_ledger),
        out_dir=cast(Path, namespace.out_dir),
    )
    try:
        return run(arguments)
    except Exception as exc:  # CLI boundary must never expose RPC credentials.
        message = redact_rpc_credentials(str(exc))
        print(f"cross-pool run failed: {message}", file=sys.stderr)
        return 1


def run(arguments: RunArguments) -> int:
    with output_lock(arguments.out_dir):
        mode = classify_output(arguments.out_dir)
        return _run_locked(arguments, mode)


def _run_locked(arguments: RunArguments, mode: PublicationMode) -> int:
    runtime = collect_runtime_environment()
    code_commit, source_diff_sha256 = capture_git_state(_REPOSITORY_ROOT)
    stage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{arguments.out_dir.name}.stage.",
            dir=arguments.out_dir.parent,
        )
    )
    inputs_dir = stage_root / "inputs"
    inputs_dir.mkdir(mode=0o700)
    candidate_dir = stage_root / "candidate"
    try:
        snapshots, snapshot_reasons = _snapshot_inputs(arguments, inputs_dir)
        captured = _capture_inputs(snapshots, snapshot_reasons)
        provenance = _run_provenance(
            captured,
            code_commit=code_commit,
            source_diff_sha256=source_diff_sha256,
            runtime=runtime,
            base_coverage=None,
            bsc_coverage=None,
        )
        if captured.reason_codes:
            return _publish_qa_candidate(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                captured.reason_codes,
            )

        assert captured.base_events is not None
        assert captured.bsc_events is not None
        assert captured.base_replay_evidence is not None
        assert captured.bsc_replay_evidence is not None
        replay_alignment_reasons = _validate_replay_alignment(
            captured.base_replay_evidence,
            captured.bsc_replay_evidence,
        )
        if replay_alignment_reasons:
            return _publish_qa_candidate(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                replay_alignment_reasons,
            )
        base_coverage, bsc_coverage, coverage_reasons = _verified_coverage(
            snapshots,
            captured,
        )
        provenance = _run_provenance(
            captured,
            code_commit=code_commit,
            source_diff_sha256=source_diff_sha256,
            runtime=runtime,
            base_coverage=base_coverage,
            bsc_coverage=bsc_coverage,
        )
        if coverage_reasons:
            return _publish_qa_candidate(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                coverage_reasons,
            )

        market_structure, market_reasons = _market_structure(snapshots)
        if market_reasons:
            return _publish_qa_candidate(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                market_reasons,
            )
        assert market_structure is not None

        alignment_reasons = _validate_causal_alignment(
            captured.base_events,
            captured.bsc_events,
        )
        if alignment_reasons:
            return _publish_qa_candidate(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                alignment_reasons,
            )
        try:
            analysis = analyze_cross_pool(
                captured.base_events,
                captured.bsc_events,
                market_structure,
            )
        except CrossPoolContractError:
            return _publish_qa_candidate(
                arguments.out_dir,
                candidate_dir,
                mode,
                provenance,
                ("ANALYSIS_CONTRACT_INVALID",),
            )

        artifacts = render_statistical_artifacts(analysis)
        artifact_hashes: dict[str, object] = {
            artifact.relative_name: artifact.sha256 for artifact in artifacts
        }
        figures = _figure_bindings(artifacts)
        manifest = build_article_manifest(
            StatisticalManifestInput(
                provenance=provenance,
                primary_predictive_audit=analysis.primary.audit_1h,
                reverse_predictive_audit=analysis.reverse.audit_1h,
                primary_dtw_stability=analysis.primary.dtw_stability,
                reverse_dtw_stability=analysis.reverse.dtw_stability,
                predictive_sensitivity=PredictiveSensitivityInput(
                    primary_15m=analysis.primary.inference_15m,
                    reverse_15m=analysis.reverse.inference_15m,
                    primary_4h=analysis.primary.inference_4h,
                    reverse_4h=analysis.reverse.inference_4h,
                ),
                causal_audit_counts=causal_audit_counts_payload(analysis),
                event_study=event_study_manifest_payload(analysis),
                dtw_null_summary=dtw_null_summary_payload(analysis),
                market_structure=market_structure_manifest_payload(analysis),
                figures=figures,
                artifacts=artifact_hashes,
            )
        )
        write_sealed_candidate(candidate_dir, artifacts, manifest)
        publish_or_verify_candidate(
            arguments.out_dir,
            candidate_dir,
            mode=mode,
        )
        return 0
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def _snapshot_inputs(
    arguments: RunArguments,
    inputs_dir: Path,
) -> tuple[SnapshotPaths, tuple[QaReasonCode, ...]]:
    snapshots = SnapshotPaths(
        base_features=inputs_dir / "base_features.csv",
        bsc_features=inputs_dir / "bsc_features.csv",
        base_replay=inputs_dir / "base_replay.csv",
        bsc_replay=inputs_dir / "bsc_replay.csv",
        base_ledger=inputs_dir / "base_ledger.csv",
        bsc_ledger=inputs_dir / "bsc_ledger.csv",
    )
    reasons: set[QaReasonCode] = set()
    primary_inputs: tuple[tuple[Path, Path, QaReasonCode], ...] = (
        (arguments.base_features, snapshots.base_features, "FEATURE_BASE_INVALID"),
        (arguments.bsc_features, snapshots.bsc_features, "FEATURE_BSC_INVALID"),
        (arguments.base_replay, snapshots.base_replay, "REPLAY_BASE_INVALID"),
        (arguments.bsc_replay, snapshots.bsc_replay, "REPLAY_BSC_INVALID"),
        (arguments.base_ledger, snapshots.base_ledger, "LEDGER_BASE_COVERAGE_INVALID"),
        (arguments.bsc_ledger, snapshots.bsc_ledger, "LEDGER_BSC_COVERAGE_INVALID"),
    )
    for source, destination, reason in primary_inputs:
        if snapshot_regular_file(source, destination) is None:
            reasons.add(reason)
    ledger_pairs: tuple[tuple[Path, Path, QaReasonCode], ...] = (
        (
            arguments.base_ledger,
            snapshots.base_ledger,
            "LEDGER_BASE_COVERAGE_INVALID",
        ),
        (
            arguments.bsc_ledger,
            snapshots.bsc_ledger,
            "LEDGER_BSC_COVERAGE_INVALID",
        ),
    )
    for ledger_source, ledger_destination, reason in ledger_pairs:
        try:
            source = ledger_coverage_path(ledger_source)
            destination = ledger_coverage_path(ledger_destination)
        except CrossPoolContractError:
            reasons.add(reason)
            continue
        if snapshot_regular_file(source, destination) is None:
            reasons.add(reason)
    return snapshots, tuple(sorted(reasons))


def _capture_inputs(
    snapshots: SnapshotPaths,
    snapshot_reasons: tuple[QaReasonCode, ...],
) -> CapturedInputs:
    reasons = set(snapshot_reasons)
    base_events, base_features_provenance = _capture_feature(
        snapshots.base_features,
        "uni-base",
        "FEATURE_BASE_INVALID",
        reasons,
    )
    bsc_events, bsc_features_provenance = _capture_feature(
        snapshots.bsc_features,
        "uni-bsc",
        "FEATURE_BSC_INVALID",
        reasons,
    )
    base_replay_evidence, base_replay_provenance = _capture_replay(
        snapshots.base_replay,
        "uni-base",
        "REPLAY_BASE_INVALID",
        reasons,
    )
    bsc_replay_evidence, bsc_replay_provenance = _capture_replay(
        snapshots.bsc_replay,
        "uni-bsc",
        "REPLAY_BSC_INVALID",
        reasons,
    )
    base_ledger_provenance = _capture_ledger(
        snapshots.base_ledger,
        "uni-base",
        "LEDGER_BASE_COVERAGE_INVALID",
        reasons,
    )
    bsc_ledger_provenance = _capture_ledger(
        snapshots.bsc_ledger,
        "uni-bsc",
        "LEDGER_BSC_COVERAGE_INVALID",
        reasons,
    )
    return CapturedInputs(
        base_events=base_events,
        bsc_events=bsc_events,
        base_replay_evidence=base_replay_evidence,
        bsc_replay_evidence=bsc_replay_evidence,
        base_features_provenance=base_features_provenance,
        bsc_features_provenance=bsc_features_provenance,
        base_replay_provenance=base_replay_provenance,
        bsc_replay_provenance=bsc_replay_provenance,
        base_ledger_provenance=base_ledger_provenance,
        bsc_ledger_provenance=bsc_ledger_provenance,
        reason_codes=tuple(sorted(reasons)),
    )


def _capture_feature(
    path: Path,
    pool: PoolName,
    reason: QaReasonCode,
    reasons: set[QaReasonCode],
) -> tuple[tuple[PoolEvent, ...] | None, InputFileProvenance | None]:
    if not path.is_file():
        return None, None
    try:
        events = load_pool_events(path, expected_pool=pool)
        provenance = InputFileProvenance(
            sha256=sha256_file(path),
            rows=len(events),
            first_timestamp_ms=events[0].timestamp_ms,
            last_timestamp_ms=events[-1].timestamp_ms,
        )
    except CrossPoolContractError:
        reasons.add(reason)
        return None, None
    return events, provenance


def _capture_replay(
    path: Path,
    pool: PoolName,
    reason: QaReasonCode,
    reasons: set[QaReasonCode],
) -> tuple[ReplayStreamEvidence | None, InputFileProvenance | None]:
    if not path.is_file():
        return None, None
    try:
        evidence = inspect_replay_stream(pool, path)
        provenance = InputFileProvenance(
            sha256=sha256_file(path),
            rows=evidence.swap_count,
            first_timestamp_ms=evidence.first_timestamp_ms,
            last_timestamp_ms=evidence.last_timestamp_ms,
        )
    except CrossPoolContractError:
        reasons.add(reason)
        return None, None
    return evidence, provenance


def _capture_ledger(
    path: Path,
    pool: str,
    reason: QaReasonCode,
    reasons: set[QaReasonCode],
) -> InputFileProvenance | None:
    if not path.is_file():
        return None
    try:
        provenance = csv_file_provenance(path, timestamp_field="timestamp_ms")
        load_ledger_coverage(pool, path)
    except CrossPoolContractError:
        reasons.add(reason)
        return None
    return provenance


def _verified_coverage(
    snapshots: SnapshotPaths,
    captured: CapturedInputs,
) -> tuple[
    VerifiedLedgerCoverageEvidence | None,
    VerifiedLedgerCoverageEvidence | None,
    tuple[QaReasonCode, ...],
]:
    assert captured.base_replay_evidence is not None
    assert captured.bsc_replay_evidence is not None
    common_end_ms = min(
        captured.base_replay_evidence.last_timestamp_ms,
        captured.bsc_replay_evidence.last_timestamp_ms,
    )
    base: VerifiedLedgerCoverageEvidence | None = None
    bsc: VerifiedLedgerCoverageEvidence | None = None
    reasons: set[QaReasonCode] = set()
    try:
        base_end_block = replay_end_block_at_or_before(
            "uni-base",
            snapshots.base_replay,
            cutoff_timestamp_ms=common_end_ms,
        )
        base = load_verified_ledger_attribution_rows(
            "uni-base",
            snapshots.base_ledger,
            required_end_block=base_end_block,
            required_end_timestamp_ms=common_end_ms,
        ).coverage
    except CrossPoolContractError:
        reasons.add("LEDGER_BASE_COVERAGE_INVALID")
    try:
        bsc_end_block = replay_end_block_at_or_before(
            "uni-bsc",
            snapshots.bsc_replay,
            cutoff_timestamp_ms=common_end_ms,
        )
        bsc = load_verified_ledger_attribution_rows(
            "uni-bsc",
            snapshots.bsc_ledger,
            required_end_block=bsc_end_block,
            required_end_timestamp_ms=common_end_ms,
        ).coverage
    except CrossPoolContractError:
        reasons.add("LEDGER_BSC_COVERAGE_INVALID")
    return base, bsc, tuple(sorted(reasons))


def _market_structure(
    snapshots: SnapshotPaths,
) -> tuple[MarketStructureAnalysis | None, tuple[QaReasonCode, ...]]:
    try:
        return (
            analyze_market_structure(
                base_replay_path=snapshots.base_replay,
                bsc_replay_path=snapshots.bsc_replay,
                base_ledger_path=snapshots.base_ledger,
                bsc_ledger_path=snapshots.bsc_ledger,
            ),
            (),
        )
    except CrossPoolContractError:
        return None, ("ANALYSIS_CONTRACT_INVALID",)


def _validate_causal_alignment(
    base_events: tuple[PoolEvent, ...],
    bsc_events: tuple[PoolEvent, ...],
) -> tuple[QaReasonCode, ...]:
    try:
        for horizon_ms in _PANEL_HORIZONS_MS:
            build_causal_panel(base_events, bsc_events, PanelConfig(horizon_ms=horizon_ms))
    except CrossPoolContractError:
        return ("CAUSAL_ALIGNMENT_INVALID",)
    return ()


def _validate_replay_alignment(
    base: ReplayStreamEvidence,
    bsc: ReplayStreamEvidence,
) -> tuple[QaReasonCode, ...]:
    if max(base.first_timestamp_ms, bsc.first_timestamp_ms) > min(
        base.last_timestamp_ms,
        bsc.last_timestamp_ms,
    ):
        return ("CAUSAL_ALIGNMENT_INVALID",)
    return ()


def _run_provenance(
    captured: CapturedInputs,
    *,
    code_commit: str,
    source_diff_sha256: str,
    runtime: RuntimeEnvironment,
    base_coverage: VerifiedLedgerCoverageEvidence | None,
    bsc_coverage: VerifiedLedgerCoverageEvidence | None,
) -> RunProvenance:
    return RunProvenance(
        code_commit=code_commit,
        source_diff_sha256=source_diff_sha256,
        base_features=captured.base_features_provenance,
        bsc_features=captured.bsc_features_provenance,
        base_replay=captured.base_replay_provenance,
        bsc_replay=captured.bsc_replay_provenance,
        base_ledger=captured.base_ledger_provenance,
        bsc_ledger=captured.bsc_ledger_provenance,
        base_ledger_coverage=base_coverage,
        bsc_ledger_coverage=bsc_coverage,
        config=RunConfiguration(),
        runtime=runtime,
    )


def _publish_qa_candidate(
    out_dir: Path,
    candidate_dir: Path,
    mode: PublicationMode,
    provenance: RunProvenance,
    reason_codes: tuple[QaReasonCode, ...],
) -> int:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=provenance,
            reason_codes=tuple(sorted(set(reason_codes))),
        )
    )
    write_sealed_candidate(candidate_dir, (), manifest)
    publish_or_verify_candidate(out_dir, candidate_dir, mode=mode)
    return 2


def _figure_bindings(
    artifacts: Sequence[RenderedArtifact],
) -> dict[str, object]:
    digests = {artifact.relative_name: artifact.sha256 for artifact in artifacts}
    return {
        "price_gap": {
            "artifact": "price_gap.png",
            "sha256": digests["price_gap.png"],
        },
        "event_response": {
            "artifact": "event_response.png",
            "sha256": digests["event_response.png"],
        },
        "dtw_lag": {
            "artifact": "dtw_lag.png",
            "sha256": digests["dtw_lag.png"],
        },
        "diagnostic_predictive_performance": {
            "artifact": "predictive_performance.png",
            "sha256": digests["predictive_performance.png"],
        },
        "lp_performance": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
