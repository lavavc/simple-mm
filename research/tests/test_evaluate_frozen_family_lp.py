from research.scripts.evaluate_frozen_family_lp import (
    frozen_paper_configs,
    hold_cngn_rows,
    no_position_rows,
    static_lp_configs,
)


def test_frozen_paper_configs_match_handoff_family() -> None:
    configs = frozen_paper_configs(
        initial_capital_usd=1200.0,
        mint_gas_usd=0.073,
        remove_gas_usd=0.022,
    )
    config_by_name = {name: params for name, params in configs}

    assert len(configs) == 40
    assert "paper_spot_w0100_pt0050_sl-0025" in config_by_name
    params = config_by_name["paper_spot_w0100_pt0050_sl-0025"]
    assert params.strategy_mode == "paper"
    assert params.center_mode == "spot"
    assert params.fixed_width_pct == 0.01
    assert params.harvest_upward_range_fraction == 0.04
    assert params.exit_confirmation_swaps == 1
    assert params.exit_price_mode == "spot"
    assert params.out_of_range_overshoot_fraction == 0.0
    assert params.transaction_costs.mint_gas_usd == 0.073


def test_static_lp_configs_vary_width_only() -> None:
    configs = static_lp_configs(initial_capital_usd=450.0, mint_gas_usd=0.015, remove_gas_usd=0.015)

    assert [name for name, _params in configs] == [
        "static_spot_w0025",
        "static_spot_w0050",
        "static_spot_w0100",
        "static_spot_w0150",
        "static_spot_w0200",
    ]
    assert all(params.strategy_mode == "static" for _name, params in configs)
    assert all(params.harvest_upward_range_fraction is None for _name, params in configs)


def test_baseline_rows_are_window_level_comparators() -> None:
    entry_states = [
        {"window_index": "0", "validation_price_return": "0.015"},
        {"window_index": "1", "validation_price_return": "-0.020"},
    ]

    idle_rows = no_position_rows(entry_states)
    hold_rows = hold_cngn_rows(entry_states)

    assert idle_rows[0]["strategy"] == "no_position"
    assert idle_rows[0]["config"] == "idle_cash"
    assert idle_rows[0]["validation_net_return"] == "0"
    assert idle_rows[1]["validation_max_drawdown"] == "0"
    assert hold_rows[0]["strategy"] == "hold_cngn"
    assert hold_rows[0]["config"] == "mark_to_pool_sqrt_mid"
    assert hold_rows[0]["validation_net_return"] == "0.015"
    assert hold_rows[1]["validation_net_return"] == "-0.020"
