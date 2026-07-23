"""Evaluate reduced directional paper LP archetypes against causal gates."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import csv
import sys
from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.data import Event, load_v4_events
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.run import (
    WindowSpec,
    _build_pool_state,
    _compute_metrics,
    _iter_window_slices,
)
from research.backtester.simulator import PoolConfig, simulate_pool
from research.scripts.evaluate_flow_gated_lp import (
    DEFAULT_GATE_FIELDS,
    _clean,
    _format_fixed,
    _required_clean,
    _required_decimal,
    build_entry_states,
    build_gate_summary_rows,
)
from research.scripts.evaluate_frozen_family_lp import (
    POOL_EXPERIMENTS,
    PoolExperiment,
    hold_cngn_rows,
    no_position_rows,
    resolve_gas_costs,
    static_lp_configs,
)

DIRECTIONAL_ACTIVE_GATE = "gate_directional_active"
DIRECTIONAL_GATE_FIELDS = (DIRECTIONAL_ACTIVE_GATE, *DEFAULT_GATE_FIELDS)
DIRECTIONAL_COMPONENT_STRATEGY = "directional_paper_component"
DIRECTIONAL_POLICY_STRATEGY = "directional_paper_lp"

HIGH_FLOW_PCT = Decimal("0.75")
HIGH_FEE_INTENSITY_PCT = Decimal("0.80")
HIGH_VOLUME_PCT = Decimal("0.80")
NEAR_FLAT_QTS_MARKOUT = Decimal("0.0005")

RESULT_FIELDS = (
    "window_index",
    "window_start",
    "window_end",
    "strategy",
    "config",
    "archetype",
    "route_reason",
    "center_offset_pct",
    "lower_width_pct",
    "upper_width_pct",
    "profit_take_pnl_mode",
    "downward_range_fraction",
    "validation_net_return",
    "validation_total_fees",
    "validation_total_transaction_cost",
    "validation_rebalance_count",
    "validation_max_drawdown",
    "validation_final_value",
    "validation_fee_to_transaction_cost_ratio",
)

ATTRIBUTION_COMPARATOR_STRATEGIES = (
    DIRECTIONAL_POLICY_STRATEGY,
    "passive_static_lp",
    "hold_cngn",
    "no_position",
)
EXTERNAL_HOLD_STRATEGY = "hold_cngn_external"


@dataclass(frozen=True)
class ReferencePricePoint:
    timestamp_ms: int
    price: Decimal


@dataclass(frozen=True)
class DirectionalArchetypeConfig:
    archetype: str
    profile: str
    name: str
    params: BacktestParams


@dataclass(frozen=True)
class DirectionalShapeSpec:
    archetype: str
    center_offset_pct: float
    lower_width_pct: float
    upper_width_pct: float
    profit_take_return: float
    stop_loss_return: float
    downward_range_fraction: float | None = None


DIRECTIONAL_PROFILE_SPECS: dict[str, tuple[DirectionalShapeSpec, ...]] = {
    "balanced_v1": (
        DirectionalShapeSpec("upside_capture", 0.0025, 0.0025, 0.015, 0.01, -0.005),
        DirectionalShapeSpec(
            "dip_accumulator",
            -0.0025,
            0.015,
            0.0025,
            0.005,
            -0.01,
            0.80,
        ),
        DirectionalShapeSpec("fee_box", 0.0, 0.0025, 0.0025, 0.0025, -0.0025, 0.50),
    ),
    "upside_wide_v1": (
        DirectionalShapeSpec("upside_capture", 0.005, 0.00125, 0.020, 0.01, -0.005),
        DirectionalShapeSpec(
            "dip_accumulator",
            -0.0025,
            0.015,
            0.0025,
            0.005,
            -0.01,
            0.80,
        ),
        DirectionalShapeSpec("fee_box", 0.0, 0.0025, 0.0025, 0.0025, -0.0025, 0.50),
    ),
    "upside_tight_v1": (
        DirectionalShapeSpec("upside_capture", 0.00125, 0.0025, 0.0075, 0.005, -0.0025),
        DirectionalShapeSpec(
            "dip_accumulator",
            -0.0025,
            0.015,
            0.0025,
            0.005,
            -0.01,
            0.80,
        ),
        DirectionalShapeSpec("fee_box", 0.0, 0.0025, 0.0025, 0.0025, -0.0025, 0.50),
    ),
    "dip_wide_v1": (
        DirectionalShapeSpec("upside_capture", 0.0025, 0.0025, 0.015, 0.01, -0.005),
        DirectionalShapeSpec(
            "dip_accumulator",
            -0.005,
            0.020,
            0.0025,
            0.005,
            -0.0125,
            0.70,
        ),
        DirectionalShapeSpec("fee_box", 0.0, 0.0025, 0.0025, 0.0025, -0.0025, 0.50),
    ),
    "fee_tight_v1": (
        DirectionalShapeSpec("upside_capture", 0.0025, 0.0025, 0.015, 0.01, -0.005),
        DirectionalShapeSpec(
            "dip_accumulator",
            -0.0025,
            0.015,
            0.0025,
            0.005,
            -0.01,
            0.80,
        ),
        DirectionalShapeSpec("fee_box", 0.0, 0.0015, 0.0015, 0.0015, -0.0015, 0.35),
    ),
}


def directional_archetype_configs(
    *,
    initial_capital_usd: float,
    mint_gas_usd: float | None,
    remove_gas_usd: float | None,
) -> list[DirectionalArchetypeConfig]:
    costs = TransactionCostModel(mint_gas_usd=mint_gas_usd, remove_gas_usd=remove_gas_usd)
    base_kwargs = {
        "strategy_mode": "paper",
        "range_mode": "fixed_pct_width",
        "center_mode": "spot",
        "harvest_upward_range_fraction": 0.04,
        "profit_take_pnl_mode": "fees",
        "require_profit_after_cost": True,
        "out_of_range_overshoot_fraction": 0.0,
        "exit_confirmation_swaps": 1,
        "exit_price_mode": "spot",
        "transaction_costs": costs,
        "initial_capital_usd": initial_capital_usd,
        "min_tick_width": 50,
        "max_tick_width": 5000,
    }
    configs: list[DirectionalArchetypeConfig] = []
    for profile, specs in DIRECTIONAL_PROFILE_SPECS.items():
        for spec in specs:
            configs.append(
                DirectionalArchetypeConfig(
                    archetype=spec.archetype,
                    profile=profile,
                    name=f"{spec.archetype}_{profile}",
                    params=BacktestParams(
                        **base_kwargs,
                        fixed_width_pct=spec.lower_width_pct + spec.upper_width_pct,
                        lower_width_pct=spec.lower_width_pct,
                        upper_width_pct=spec.upper_width_pct,
                        center_offset_pct=spec.center_offset_pct,
                        profit_take_return=spec.profit_take_return,
                        stop_loss_return=spec.stop_loss_return,
                        downward_range_fraction=spec.downward_range_fraction,
                    ),
                )
            )
    return configs


def directional_policy_profiles(
    configs: Sequence[DirectionalArchetypeConfig],
) -> dict[str, dict[str, DirectionalArchetypeConfig]]:
    profiles: dict[str, dict[str, DirectionalArchetypeConfig]] = {}
    for config in configs:
        profile = profiles.setdefault(config.profile, {})
        if config.archetype in profile:
            raise ValueError(
                f"duplicate directional config for profile={config.profile} "
                f"archetype={config.archetype}"
            )
        profile[config.archetype] = config

    required_archetypes = {"upside_capture", "dip_accumulator", "fee_box"}
    for profile, profile_configs in profiles.items():
        missing = required_archetypes.difference(profile_configs)
        if missing:
            raise ValueError(f"profile {profile} missing archetypes: {', '.join(sorted(missing))}")
    return profiles


def route_directional_archetype(entry_state: dict[str, str]) -> tuple[str, str]:
    predicted_20 = _optional_decimal(entry_state.get("entry_predicted_markout_20_25", ""))
    predicted_100 = _optional_decimal(entry_state.get("entry_predicted_markout_100_25", ""))

    if _clean(entry_state.get("gate_strict_sign_cone", "")) == "1" and (
        _is_positive(predicted_20) or _is_positive(predicted_100)
    ):
        return "upside_capture", "strict_sign_cone_positive_qts"

    fee_intensity = _optional_decimal(entry_state.get("entry_fee_intensity_proxy_cone_pct_1h", ""))
    volume = _optional_decimal(entry_state.get("entry_volume_cone_pct_1h", ""))
    if (
        fee_intensity is not None
        and fee_intensity >= HIGH_FEE_INTENSITY_PCT
        and volume is not None
        and volume >= HIGH_VOLUME_PCT
        and predicted_20 is not None
        and abs(predicted_20) <= NEAR_FLAT_QTS_MARKOUT
    ):
        return "fee_box", "high_fee_high_volume_near_flat_qts"

    train_return = _optional_decimal(entry_state.get("train_price_return", ""))
    flow_pct = _optional_decimal(entry_state.get("entry_flow_pct", ""))
    high_flow_or_fee = (flow_pct is not None and flow_pct >= HIGH_FLOW_PCT) or (
        fee_intensity is not None and fee_intensity >= HIGH_FEE_INTENSITY_PCT
    )
    if (
        train_return is not None
        and train_return <= 0
        and high_flow_or_fee
        and predicted_20 is not None
        and predicted_20 >= 0
    ):
        return "dip_accumulator", "flat_down_train_high_flow_or_fee_nonnegative_qts"

    return "no_position", "no_directional_rule"


def route_directional_windows(entry_states: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for state in entry_states:
        archetype, reason = route_directional_archetype(state)
        rows.append(
            {
                **state,
                "directional_archetype": archetype,
                "directional_route_reason": reason,
                DIRECTIONAL_ACTIVE_GATE: "0" if archetype == "no_position" else "1",
            }
        )
    return rows


def load_reference_price_csv(path: Path) -> list[ReferencePricePoint]:
    rows = _read_csv(path)
    points: list[ReferencePricePoint] = []
    for row in rows:
        timestamp = _optional_int(_first_present(row, ("timestamp_ms", "ts")))
        price = _optional_decimal(
            _first_present(
                row,
                (
                    "reference_price",
                    "price",
                    "mid",
                    "binance_reference",
                    "ngn_per_usdt",
                    "usdt_ngn",
                ),
            )
        )
        if timestamp is None:
            raise ValueError(f"{path} has a reference row without timestamp_ms or ts")
        if price is None or price <= 0:
            raise ValueError(f"{path} has a reference row without a positive price")
        points.append(ReferencePricePoint(timestamp_ms=timestamp, price=price))
    return sorted(points, key=lambda point: point.timestamp_ms)


def external_reference_hold_cngn_rows(
    entry_states: Sequence[dict[str, str]],
    reference_prices: Sequence[ReferencePricePoint],
    *,
    source_name: str,
    max_age_ms: int,
) -> list[dict[str, str]]:
    if max_age_ms < 0:
        raise ValueError("max_age_ms must be non-negative")
    config_name = f"mark_to_{_required_source_name(source_name)}"
    points = sorted(reference_prices, key=lambda point: point.timestamp_ms)
    timestamps = [point.timestamp_ms for point in points]
    rows: list[dict[str, str]] = []

    for state in entry_states:
        window_index = _required_clean(state, "window_index")
        start_ts = _required_int(state, "validation_start_timestamp_ms")
        end_ts = _required_int(state, "validation_end_timestamp_ms")
        start = _previous_reference_point(
            points,
            timestamps,
            timestamp_ms=start_ts,
            max_age_ms=max_age_ms,
            window_index=window_index,
            edge="start",
        )
        end = _previous_reference_point(
            points,
            timestamps,
            timestamp_ms=end_ts,
            max_age_ms=max_age_ms,
            window_index=window_index,
            edge="end",
        )
        net_return = end.price / start.price - Decimal("1")
        rows.append(
            {
                "window_index": window_index,
                "window_start": str(start_ts),
                "window_end": str(end_ts),
                "strategy": EXTERNAL_HOLD_STRATEGY,
                "config": config_name,
                "archetype": "",
                "route_reason": "",
                "center_offset_pct": "",
                "lower_width_pct": "",
                "upper_width_pct": "",
                "profit_take_pnl_mode": "",
                "downward_range_fraction": "",
                "validation_net_return": _format_reference_decimal(net_return),
                "validation_total_fees": "0",
                "validation_total_transaction_cost": "0",
                "validation_rebalance_count": "0",
                "validation_max_drawdown": _format_reference_decimal(
                    max(-net_return, Decimal("0"))
                ),
                "validation_final_value": "",
                "validation_fee_to_transaction_cost_ratio": "0",
            }
        )
    return rows


def build_directional_attribution_rows(
    result_rows: Sequence[dict[str, str]],
    entry_states: Sequence[dict[str, str]],
    summary_rows: Sequence[dict[str, str]],
    *,
    gate_field: str,
    initial_capital_usd: float,
) -> list[dict[str, str]]:
    selected_configs = _selected_attribution_configs(summary_rows, gate_field)
    external_config = _selected_optional_attribution_config(
        summary_rows,
        gate_field,
        EXTERNAL_HOLD_STRATEGY,
    )
    result_by_identity = _result_rows_by_identity(result_rows)
    rows: list[dict[str, str]] = []

    for state in entry_states:
        if _clean(state.get(gate_field, "")) != "1":
            continue
        window_index = _required_clean(state, "window_index")
        directional = _require_result_row(
            result_by_identity,
            window_index,
            DIRECTIONAL_POLICY_STRATEGY,
            selected_configs[DIRECTIONAL_POLICY_STRATEGY],
        )
        static = _require_result_row(
            result_by_identity,
            window_index,
            "passive_static_lp",
            selected_configs["passive_static_lp"],
        )
        hold = _require_result_row(
            result_by_identity,
            window_index,
            "hold_cngn",
            selected_configs["hold_cngn"],
        )
        no_position = _require_result_row(
            result_by_identity,
            window_index,
            "no_position",
            selected_configs["no_position"],
        )
        external_hold = (
            _require_result_row(
                result_by_identity,
                window_index,
                EXTERNAL_HOLD_STRATEGY,
                external_config,
            )
            if external_config is not None
            else None
        )

        directional_return = _required_decimal(directional, "validation_net_return")
        static_return = _required_decimal(static, "validation_net_return")
        hold_return = _required_decimal(hold, "validation_net_return")
        no_position_return = _required_decimal(no_position, "validation_net_return")
        external_return = (
            _required_decimal(external_hold, "validation_net_return")
            if external_hold is not None
            else None
        )
        validation_price_return = _required_decimal(state, "validation_price_return")
        directional_fees = _required_decimal(directional, "validation_total_fees")
        directional_tx_cost = _required_decimal(
            directional,
            "validation_total_transaction_cost",
        )
        fee_component = _return_component(
            directional_fees - directional_tx_cost,
            initial_capital_usd,
        )
        down_window = validation_price_return < 0
        directional_minus_static = directional_return - static_return
        directional_minus_hold = directional_return - hold_return

        rows.append(
            {
                "gate": gate_field,
                "window_index": window_index,
                "directional_archetype": _clean(state.get("directional_archetype", "")),
                "directional_route_reason": _clean(
                    state.get("directional_route_reason", "")
                ),
                "directional_config": selected_configs[DIRECTIONAL_POLICY_STRATEGY],
                "static_config": selected_configs["passive_static_lp"],
                "validation_price_return": _format_fixed(validation_price_return),
                "down_window": "1" if down_window else "0",
                "directional_net_return": _format_fixed(directional_return),
                "static_net_return": _format_fixed(static_return),
                "hold_mark_return": _format_fixed(hold_return),
                "hold_external_config": external_config or "",
                "hold_external_return": _format_optional_fixed(external_return),
                "no_position_return": _format_fixed(no_position_return),
                "directional_minus_static": _format_fixed(directional_minus_static),
                "directional_minus_hold_mark": _format_fixed(directional_minus_hold),
                "directional_minus_hold_external": _format_optional_fixed(
                    directional_return - external_return
                    if external_return is not None
                    else None
                ),
                "directional_minus_no_position": _format_fixed(
                    directional_return - no_position_return
                ),
                "down_window_directional_minus_static": _format_fixed(
                    directional_minus_static
                )
                if down_window
                else "not_applicable",
                "down_window_directional_minus_hold_mark": _format_fixed(
                    directional_minus_hold
                )
                if down_window
                else "not_applicable",
                "directional_total_fees_usd": _format_fixed(directional_fees),
                "directional_total_transaction_cost_usd": _format_fixed(directional_tx_cost),
                "directional_fee_net_return_component": _format_fixed(fee_component),
                "directional_range_inventory_return_component": _format_fixed(
                    directional_return - fee_component
                ),
            }
        )

    return rows


def evaluate_pool(
    experiment: PoolExperiment,
    *,
    out_dir: Path,
    max_windows: int | None,
    external_reference_prices: Sequence[ReferencePricePoint] | None = None,
    external_reference_source: str = "binance_reference",
    external_reference_max_age_ms: int = 3_600_000,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    mint_gas_usd, remove_gas_usd = resolve_gas_costs(experiment.pool, None, None)
    feature_rows = _read_csv(experiment.feature_csv)
    qts_rows = _read_csv(experiment.qts_feature_csv)
    entry_states = route_directional_windows(
        build_entry_states(
            feature_rows,
            qts_rows=qts_rows,
            train_swaps=experiment.train_swaps,
            val_swaps=experiment.val_swaps,
            stride_swaps=experiment.val_swaps,
            flow_threshold=Decimal("0.90"),
            train_return_max=Decimal("0"),
            max_windows=max_windows,
        )
    )
    _write_csv(out_dir / "entry_state_windows.csv", entry_states)

    events = load_v4_events(
        str(experiment.history_csv),
        pool_id=experiment.pool_config.pool_address,
    )
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=experiment.train_swaps,
        val_swaps=experiment.val_swaps,
        stride_swaps=experiment.val_swaps,
        min_train_swaps=experiment.train_swaps,
        min_train_liquidity_events=0,
        min_val_swaps=experiment.val_swaps,
    )
    directional_configs = directional_archetype_configs(
        initial_capital_usd=experiment.initial_capital_usd,
        mint_gas_usd=mint_gas_usd,
        remove_gas_usd=remove_gas_usd,
    )
    static_configs = [
        ("passive_static_lp", name, params)
        for name, params in static_lp_configs(
            initial_capital_usd=experiment.initial_capital_usd,
            mint_gas_usd=mint_gas_usd,
            remove_gas_usd=remove_gas_usd,
        )
    ]

    result_rows = _normalize_result_rows(
        [
            *no_position_rows(entry_states),
            *hold_cngn_rows(entry_states),
            *(
                external_reference_hold_cngn_rows(
                    entry_states,
                    external_reference_prices,
                    source_name=external_reference_source,
                    max_age_ms=external_reference_max_age_ms,
                )
                if external_reference_prices is not None
                else []
            ),
            *_simulate_static_rows(
                events,
                experiment.pool_config,
                spec,
                static_configs,
                max_windows=max_windows,
            ),
            *_simulate_directional_component_rows(
                events,
                experiment.pool_config,
                spec,
                directional_configs,
                max_windows=max_windows,
            ),
            *_simulate_directional_policy_rows(
                events,
                experiment.pool_config,
                spec,
                entry_states,
                directional_configs,
                max_windows=max_windows,
            ),
        ]
    )
    _write_csv(out_dir / "directional_paper_window_results.csv", result_rows)

    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=DIRECTIONAL_GATE_FIELDS,
        identity_fields=("strategy", "config"),
    )
    _write_csv(out_dir / "directional_paper_gate_summary.csv", summary_rows)

    attribution_rows = build_directional_attribution_rows(
        result_rows,
        entry_states,
        summary_rows,
        gate_field=DIRECTIONAL_ACTIVE_GATE,
        initial_capital_usd=experiment.initial_capital_usd,
    )
    _write_csv(out_dir / "directional_paper_attribution.csv", attribution_rows)
    (out_dir / "directional_paper_report.md").write_text(
        render_markdown(
            pool=experiment.pool,
            entry_states=entry_states,
            summary_rows=summary_rows,
            attribution_rows=attribution_rows,
        )
    )
    print(f"{experiment.pool}: wrote {out_dir}")


def render_markdown(
    *,
    pool: str,
    entry_states: Sequence[dict[str, str]],
    summary_rows: Sequence[dict[str, str]],
    attribution_rows: Sequence[dict[str, str]],
) -> str:
    route_counts: dict[str, int] = {}
    for state in entry_states:
        archetype = _clean(state.get("directional_archetype", "no_position"))
        route_counts[archetype] = route_counts.get(archetype, 0) + 1

    lines = [
        f"# Directional Paper LP Report: {pool}",
        "",
        "Rows are costed validation-window summaries for a reduced directional "
        "paper LP family. Routing uses only causal train-window and entry-row "
        "features.",
        "",
        "## Route Counts",
        "",
        "| archetype | windows |",
        "| --- | ---: |",
    ]
    for archetype, count in sorted(route_counts.items()):
        lines.append(f"| {archetype} | {count} |")
    lines.extend(["", "## Gate Summary", ""])
    seen_gates = {_clean(row.get("gate", "")) for row in summary_rows}
    ordered_gates = [gate for gate in DIRECTIONAL_GATE_FIELDS if gate in seen_gates]
    ordered_gates.extend(sorted(seen_gates.difference(ordered_gates)))
    for gate in ordered_gates:
        if gate == "":
            continue
        gate_rows = [row for row in summary_rows if _clean(row.get("gate", "")) == gate]
        lines.extend([f"## {gate}", "", _summary_table(gate_rows[:12]), ""])
    if attribution_rows:
        fields = [
            "window_index",
            "directional_archetype",
            "validation_price_return",
            "down_window",
            "directional_net_return",
            "static_net_return",
            "hold_mark_return",
            "hold_external_return",
            "directional_minus_static",
            "directional_minus_hold_mark",
            "directional_minus_hold_external",
            "directional_fee_net_return_component",
            "directional_range_inventory_return_component",
        ]
        lines.extend(
            [
                "## Directional Attribution",
                "",
                "| " + " | ".join(fields) + " |",
                "| " + " | ".join("---" for _field in fields) + " |",
            ]
        )
        for row in attribution_rows:
            lines.append("| " + " | ".join(row.get(field, "") for field in fields) + " |")
        lines.append("")
    return "\n".join(lines)


def _simulate_static_rows(
    events: list[Event],
    pool_config: PoolConfig,
    spec: WindowSpec,
    configs: Sequence[tuple[str, str, BacktestParams]],
    *,
    max_windows: int | None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for window_slice in _iter_window_slices(events, spec, max_windows=max_windows):
        if window_slice.skipped_reason is not None:
            continue
        initial_pool_state = _build_pool_state(window_slice.train_events)
        for strategy, config_name, params in configs:
            sim = simulate_pool(
                window_slice.val_events,
                params,
                pool_config,
                params.initial_capital_usd,
                initial_pool_state=initial_pool_state,
                settle_to_cash=False,
            )
            rows.append(
                _metrics_result_row(
                    window_index=str(window_slice.window.index),
                    window_start=window_slice.window.val_start.isoformat(),
                    window_end=window_slice.window.val_end.isoformat(),
                    strategy=strategy,
                    config=config_name,
                    metrics=_compute_metrics(sim, params.initial_capital_usd),
                )
            )
    return rows


def _simulate_directional_component_rows(
    events: list[Event],
    pool_config: PoolConfig,
    spec: WindowSpec,
    configs: Sequence[DirectionalArchetypeConfig],
    *,
    max_windows: int | None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for window_slice in _iter_window_slices(events, spec, max_windows=max_windows):
        if window_slice.skipped_reason is not None:
            continue
        initial_pool_state = _build_pool_state(window_slice.train_events)
        for config in configs:
            sim = simulate_pool(
                window_slice.val_events,
                config.params,
                pool_config,
                config.params.initial_capital_usd,
                initial_pool_state=initial_pool_state,
                settle_to_cash=False,
            )
            rows.append(
                _metrics_result_row(
                    window_index=str(window_slice.window.index),
                    window_start=window_slice.window.val_start.isoformat(),
                    window_end=window_slice.window.val_end.isoformat(),
                    strategy=DIRECTIONAL_COMPONENT_STRATEGY,
                    config=config.name,
                    metrics=_compute_metrics(sim, config.params.initial_capital_usd),
                    archetype=config.archetype,
                    params=config.params,
                )
            )
    return rows


def _simulate_directional_policy_rows(
    events: list[Event],
    pool_config: PoolConfig,
    spec: WindowSpec,
    entry_states: Sequence[dict[str, str]],
    configs: Sequence[DirectionalArchetypeConfig],
    *,
    max_windows: int | None,
) -> list[dict[str, str]]:
    states_by_window = {_required_clean(state, "window_index"): state for state in entry_states}
    profiles = directional_policy_profiles(configs)
    rows: list[dict[str, str]] = []
    for window_slice in _iter_window_slices(events, spec, max_windows=max_windows):
        if window_slice.skipped_reason is not None:
            continue
        window_index = str(window_slice.window.index)
        state = states_by_window[window_index]
        archetype = _required_clean(state, "directional_archetype")
        route_reason = _required_clean(state, "directional_route_reason")
        initial_pool_state = _build_pool_state(window_slice.train_events)
        for profile, profile_configs in profiles.items():
            if archetype == "no_position":
                rows.append(
                    {
                        "window_index": window_index,
                        "window_start": window_slice.window.val_start.isoformat(),
                        "window_end": window_slice.window.val_end.isoformat(),
                        "strategy": DIRECTIONAL_POLICY_STRATEGY,
                        "config": profile,
                        "archetype": archetype,
                        "route_reason": route_reason,
                        "validation_net_return": "0",
                        "validation_total_fees": "0",
                        "validation_total_transaction_cost": "0",
                        "validation_rebalance_count": "0",
                        "validation_max_drawdown": "0",
                        "validation_final_value": "",
                        "validation_fee_to_transaction_cost_ratio": "0",
                    }
                )
                continue
            config = profile_configs[archetype]
            sim = simulate_pool(
                window_slice.val_events,
                config.params,
                pool_config,
                config.params.initial_capital_usd,
                initial_pool_state=initial_pool_state,
                settle_to_cash=False,
            )
            rows.append(
                _metrics_result_row(
                    window_index=window_index,
                    window_start=window_slice.window.val_start.isoformat(),
                    window_end=window_slice.window.val_end.isoformat(),
                    strategy=DIRECTIONAL_POLICY_STRATEGY,
                    config=profile,
                    metrics=_compute_metrics(sim, config.params.initial_capital_usd),
                    archetype=archetype,
                    route_reason=route_reason,
                    params=config.params,
                )
            )
    return rows


def _metrics_result_row(
    *,
    window_index: str,
    window_start: str,
    window_end: str,
    strategy: str,
    config: str,
    metrics: dict,
    archetype: str = "",
    route_reason: str = "",
    params: BacktestParams | None = None,
) -> dict[str, str]:
    row = {
        "window_index": window_index,
        "window_start": window_start,
        "window_end": window_end,
        "strategy": strategy,
        "config": config,
        "archetype": archetype,
        "route_reason": route_reason,
        "validation_net_return": str(metrics["net_return"]),
        "validation_total_fees": str(metrics["total_fees"]),
        "validation_total_transaction_cost": str(metrics["total_transaction_cost"]),
        "validation_rebalance_count": str(metrics["rebalance_count"]),
        "validation_max_drawdown": str(metrics["max_drawdown"]),
        "validation_final_value": str(metrics["final_value"]),
        "validation_fee_to_transaction_cost_ratio": str(metrics["fee_to_transaction_cost_ratio"]),
    }
    if params is not None:
        row.update(
            {
                "center_offset_pct": str(params.center_offset_pct),
                "lower_width_pct": str(params.lower_width_pct),
                "upper_width_pct": str(params.upper_width_pct),
                "profit_take_pnl_mode": params.profit_take_pnl_mode,
                "downward_range_fraction": str(params.downward_range_fraction or ""),
            }
        )
    return row


def _normalize_result_rows(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    return [{field: _clean(row.get(field, "")) for field in RESULT_FIELDS} for row in rows]


def _selected_attribution_configs(
    summary_rows: Sequence[dict[str, str]],
    gate_field: str,
) -> dict[str, str]:
    selected: dict[str, str] = {}
    for strategy in ATTRIBUTION_COMPARATOR_STRATEGIES:
        candidates = [
            row
            for row in summary_rows
            if _clean(row.get("gate", "")) == gate_field
            and _clean(row.get("strategy", "")) == strategy
        ]
        if not candidates:
            raise ValueError(f"missing attribution comparator {strategy} for {gate_field}")
        best = max(
            candidates,
            key=lambda row: (
                _required_decimal(row, "sum_active_return"),
                _required_decimal(row, "worst_active_return"),
            ),
        )
        selected[strategy] = _required_clean(best, "config")
    return selected


def _selected_optional_attribution_config(
    summary_rows: Sequence[dict[str, str]],
    gate_field: str,
    strategy: str,
) -> str | None:
    candidates = [
        row
        for row in summary_rows
        if _clean(row.get("gate", "")) == gate_field
        and _clean(row.get("strategy", "")) == strategy
    ]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda row: (
            _required_decimal(row, "sum_active_return"),
            _required_decimal(row, "worst_active_return"),
        ),
    )
    return _required_clean(best, "config")


def _result_rows_by_identity(
    result_rows: Sequence[dict[str, str]],
) -> dict[tuple[str, str, str], dict[str, str]]:
    rows: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in result_rows:
        key = (
            _required_clean(row, "window_index"),
            _required_clean(row, "strategy"),
            _required_clean(row, "config"),
        )
        if key in rows:
            window_index, strategy, config = key
            raise ValueError(
                "duplicate result row for "
                f"window={window_index} strategy={strategy} config={config}"
            )
        rows[key] = row
    return rows


def _require_result_row(
    rows: dict[tuple[str, str, str], dict[str, str]],
    window_index: str,
    strategy: str,
    config: str,
) -> dict[str, str]:
    row = rows.get((window_index, strategy, config))
    if row is None:
        raise ValueError(
            "missing result row for "
            f"window={window_index} strategy={strategy} config={config}"
        )
    return row


def _return_component(amount_usd: Decimal, initial_capital_usd: float) -> Decimal:
    capital = Decimal(str(initial_capital_usd))
    if capital <= 0:
        raise ValueError("initial_capital_usd must be positive")
    return amount_usd / capital


def _previous_reference_point(
    points: Sequence[ReferencePricePoint],
    timestamps: Sequence[int],
    *,
    timestamp_ms: int,
    max_age_ms: int,
    window_index: str,
    edge: str,
) -> ReferencePricePoint:
    index = bisect_right(timestamps, timestamp_ms) - 1
    if index < 0:
        raise ValueError(
            "missing external reference price for "
            f"window={window_index} edge={edge} timestamp_ms={timestamp_ms}"
        )
    point = points[index]
    if timestamp_ms - point.timestamp_ms > max_age_ms:
        raise ValueError(
            "missing external reference price for "
            f"window={window_index} edge={edge} timestamp_ms={timestamp_ms} "
            f"max_age_ms={max_age_ms}"
        )
    return point


def _first_present(row: dict[str, str], fields: Sequence[str]) -> str:
    for field in fields:
        cleaned = _clean(row.get(field, ""))
        if cleaned != "":
            return cleaned
    return ""


def _required_int(row: dict[str, str], field: str) -> int:
    value = _optional_int(row.get(field, ""))
    if value is None:
        raise ValueError(f"missing required integer field {field}")
    return value


def _optional_int(value: str | None) -> int | None:
    cleaned = _clean(value)
    if cleaned == "":
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid integer value {cleaned}") from exc


def _required_source_name(value: str) -> str:
    cleaned = _clean(value)
    if cleaned == "":
        raise ValueError("source_name must not be empty")
    return cleaned.replace("-", "_")


def _format_optional_fixed(value: Decimal | None) -> str:
    return "" if value is None else _format_fixed(value)


def _format_reference_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _summary_table(rows: Sequence[dict[str, str]]) -> str:
    if not rows:
        return "none"
    fields = [
        "strategy",
        "config",
        "active_windows",
        "sum_active_return",
        "mean_all_window_return",
        "worst_active_return",
        "positive_active_rate",
        "fee_cost_ratio",
        "leave_one_active_window_out_min_return",
        "active_minus_hold_cngn",
    ]
    lines = [
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _field in fields) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row.get(field, "") for field in fields) + " |")
    return "\n".join(lines)


def _is_positive(value: Decimal | None) -> bool:
    return value is not None and value > 0


def _optional_decimal(value: str | None) -> Decimal | None:
    cleaned = _clean(value)
    if cleaned == "":
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value {cleaned}") from exc


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        return [dict(row) for row in reader]


def _write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=[*POOL_EXPERIMENTS.keys(), "all"], default="all")
    parser.add_argument("--out-root", default="research/results/flow_gated_lp")
    parser.add_argument("--max-windows", type=int)
    parser.add_argument(
        "--external-reference-csv",
        help="Optional Binance/external reference CSV for non-pool cNGN hold marks.",
    )
    parser.add_argument("--external-reference-source", default="binance_reference")
    parser.add_argument("--external-reference-max-age-seconds", type=int, default=3600)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    pools = POOL_EXPERIMENTS.keys() if args.pool == "all" else (args.pool,)
    out_root = Path(args.out_root)
    external_reference_prices = (
        load_reference_price_csv(Path(args.external_reference_csv))
        if args.external_reference_csv
        else None
    )
    for pool in pools:
        evaluate_pool(
            POOL_EXPERIMENTS[pool],
            out_dir=out_root / pool.replace("-", "_"),
            max_windows=args.max_windows,
            external_reference_prices=external_reference_prices,
            external_reference_source=args.external_reference_source,
            external_reference_max_age_ms=args.external_reference_max_age_seconds * 1000,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
