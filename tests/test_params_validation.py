"""DexParams serialisation: Decimal fields must survive the DB round-trip and JSON serialisation.

Pydantic field validation is Pydantic's job. These pin the non-obvious behaviour:
a DexParams stored via model_dump(mode='json') and re-read at startup must
reconstruct exactly, and Decimal values must be JSON-serialisable (not raw Python objects).
"""

import json
import pytest
from decimal import Decimal

from engine.config import DexParams
from tests.conftest_params import make_dex_params
from tests.fakes import FakeDexAdapter


class TestDexParamsDbRoundTrip:
    """DexParams stored via model_dump(mode='json') must reconstruct exactly on startup."""

    @pytest.mark.asyncio
    async def test_persisted_params_restored_on_startup(self):
        venue = FakeDexAdapter()
        original = make_dex_params(sd_multiplier=Decimal("4.5"), downside_skew=Decimal("0.6"))
        stored = original.model_dump(mode="json")

        venue.params = DexParams(**stored)

        assert venue.params.sd_multiplier == Decimal("4.5")
        assert venue.params.downside_skew == Decimal("0.6")

    def test_model_dump_json_survives_json_dumps_and_roundtrip(self):
        """Decimal fields must be JSON-serialisable and reconstruct to the same value."""
        params = make_dex_params(sd_multiplier=Decimal("3.14"), ewma_lambda=Decimal("0.975"))
        serialised = params.model_dump(mode="json")
        json.dumps(serialised)  # must not raise
        restored = DexParams(**serialised)
        assert restored.sd_multiplier == params.sd_multiplier
        assert restored.ewma_lambda == params.ewma_lambda
