import importlib
import sys


def test_shared_dex_math_import_does_not_load_live_v4_adapter():
    sys.modules.pop("engine.venues.dex.shared", None)
    sys.modules.pop("engine.venues.dex.v4", None)
    sys.modules.pop("engine.venues.dex", None)

    shared = importlib.import_module("engine.venues.dex.shared")

    assert shared._Q96 == 2**96
    assert "engine.venues.dex.v4" not in sys.modules


def test_backtester_clmm_math_import_does_not_load_live_v4_adapter():
    sys.modules.pop("research.backtester.clmm_math", None)
    sys.modules.pop("engine.venues.dex.shared", None)
    sys.modules.pop("engine.venues.dex.v4", None)
    sys.modules.pop("engine.venues.dex", None)

    clmm_math = importlib.import_module("research.backtester.clmm_math")

    assert clmm_math.tick_to_sqrt_price_x96(0) == 2**96
    assert "engine.venues.dex.v4" not in sys.modules
