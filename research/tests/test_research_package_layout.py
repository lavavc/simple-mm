import importlib


def test_research_backtester_imports_from_research_namespace():
    module = importlib.import_module("research.backtester.clmm_math")

    assert module.tick_to_sqrt_price_x96(0) == 2**96


def test_research_scripts_import_from_research_namespace():
    module = importlib.import_module("research.scripts.export_fair_price_markouts")

    assert module.DEFAULT_TARGET_USD > 0


def test_v3_exposes_canonical_integer_tick_math_used_by_shared():
    v3 = importlib.import_module("engine.math.v3")
    shared = importlib.import_module("engine.venues.dex.shared")

    assert v3._tick_to_sqrt_price_x96(0) == v3._Q96
    assert shared._tick_to_sqrt_price_x96 is v3._tick_to_sqrt_price_x96
