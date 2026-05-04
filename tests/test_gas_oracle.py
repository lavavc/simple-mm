from decimal import Decimal
from unittest.mock import patch

import engine.market.gas_oracle as gas_oracle


def test_gas_oracle_values_stay_fresh_until_monotonic_expiry(monkeypatch):
    monkeypatch.setattr(
        gas_oracle,
        "_state",
        {
            "gas_usd_base": Decimal("0.003"),
            "gas_usd_bsc": Decimal("0.005"),
            "last_updated_monotonic": 100.0,
        },
    )

    with patch("engine.market.gas_oracle.time.monotonic", return_value=350.0):
        assert gas_oracle.gas_usd_base() == Decimal("0.003")
        assert gas_oracle.gas_usd_bsc() == Decimal("0.005")


def test_gas_oracle_values_expire_when_monotonic_window_is_exceeded(monkeypatch):
    monkeypatch.setattr(
        gas_oracle,
        "_state",
        {
            "gas_usd_base": Decimal("0.003"),
            "gas_usd_bsc": Decimal("0.005"),
            "last_updated_monotonic": 100.0,
        },
    )

    with patch("engine.market.gas_oracle.time.monotonic", return_value=401.0):
        assert gas_oracle.gas_usd_base() is None
        assert gas_oracle.gas_usd_bsc() is None
