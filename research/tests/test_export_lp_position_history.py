from decimal import Decimal

from research.scripts.export_lp_position_history import _fieldnames, _flatten_snapshot


def test_flatten_snapshot_exports_lp_fields_and_balances():
    rows = [
        {
            "timestamp": 1_700_000_000_000,
            "venue": "uni-base",
            "pair": "cNGN/USDC",
            "position_value_usd": Decimal("1234.56"),
            "volume_24h_usd": Decimal("7890"),
            "balances": {"cNGN": Decimal("100"), "USDC": Decimal("25.5")},
            "rates": {"buy": Decimal("0.001"), "sell": Decimal("0.002")},
            "lp_position": {
                "token_id": "42",
                "liquidity": "999",
                "tick_lower": -100,
                "tick_upper": 100,
                "range_min": Decimal("0.00061"),
                "range_max": Decimal("0.00073"),
                "current_price": Decimal("0.00068"),
                "price_position_fraction": Decimal("0.58"),
                "in_range": True,
                "our_share_pct": Decimal("12.3"),
                "snapshot_status": "live",
                "snapshot_message": None,
            },
        }
    ]

    fieldnames = _fieldnames(rows)
    flat = _flatten_snapshot(rows[0], ["USDC", "cNGN"], ["buy", "sell"])

    assert "balance_usdc" in fieldnames
    assert "balance_cngn" in fieldnames
    assert "rate_buy" in fieldnames
    assert "rate_sell" in fieldnames
    assert flat["venue"] == "uni-base"
    assert flat["token_id"] == "42"
    assert flat["liquidity"] == "999"
    assert flat["balance_usdc"] == "25.5"
    assert flat["balance_cngn"] == "100"
    assert flat["rate_buy"] == "0.001"
    assert flat["in_range"] == "True"
