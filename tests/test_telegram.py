from types import SimpleNamespace

from engine.bot.telegram import _default_orders_venue, _resolve_operator_venue


def test_default_orders_venue_prefers_quidax_lp_for_order_views():
    trade = object()
    lp = object()
    runtime = SimpleNamespace(venues={"quidax": trade, "quidax-lp": lp})

    venue_name, venue = _default_orders_venue(runtime)

    assert venue_name == "quidax-lp"
    assert venue is lp


def test_operator_venue_resolution_keeps_quidax_trade_exact_when_lp_exists():
    trade = object()
    lp = object()
    runtime = SimpleNamespace(venues={"quidax": trade, "quidax-lp": lp})

    venue_name, venue = _resolve_operator_venue(runtime, "quidax")

    assert venue_name == "quidax"
    assert venue is trade
