"""Pure + seeded-cache tests for pool_state.py."""

import time
from decimal import Decimal

import pytest

import engine.market.pool_state as pool_state
from engine.market.pool_state import (
    _fetch_fee_with_retry,
    get_cached_pool_state,
    update_single_pool_state,
    swap_token0_for_token1,
    swap_token1_for_token0,
    update_pool_state_from_event,
    Q96,
)


# =============================================================================
# Swap math (pure — no cache dependency)
# =============================================================================

# Use realistic Base pool values (cNGN/USDC, 6/6 dec, price ≈ 0.000606)
import math as _math

_BASE_SQRT_X96 = Decimal(int(_math.sqrt(0.000606) * (2 ** 96)))
_LIQ = Decimal(10 ** 18)
_FEE = Decimal("0.0005")
_RPC_SECRET = "fixture-secret-that-must-not-be-logged"
_SECRET_URL = f"https://example.invalid/v2/{_RPC_SECRET}"


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.records.append(("warning", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.records.append(("error", event, fields))

    def debug(self, event: str, **fields: object) -> None:
        self.records.append(("debug", event, fields))

    def info(self, event: str, **fields: object) -> None:
        self.records.append(("info", event, fields))


class _FailingEth:
    async def call(self, _payload: object) -> bytes:
        raise RuntimeError(f"RPC request failed at {_SECRET_URL}")


class _FailingAsyncWeb3:
    AsyncHTTPProvider = staticmethod(lambda _url: object())

    def __init__(self, _provider: object) -> None:
        self.eth = _FailingEth()

    @staticmethod
    def to_checksum_address(address: str) -> str:
        return address


@pytest.mark.asyncio
async def test_pool_state_failure_logs_no_rpc_endpoint_or_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _RecordingLogger()
    monkeypatch.setattr(pool_state, "logger", recording)
    monkeypatch.setattr(pool_state, "AsyncWeb3", _FailingAsyncWeb3)

    assert await update_single_pool_state(_SECRET_URL, "0xpool") is False

    level, event, fields = recording.records[-1]
    assert (level, event) == ("error", "pool_state_fetch_error")
    assert "rpc" not in fields
    assert _RPC_SECRET not in repr(fields)
    assert "[REDACTED]" in str(fields["error"])


@pytest.mark.asyncio
async def test_pool_fee_retry_redacts_every_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _RecordingLogger()
    monkeypatch.setattr(pool_state, "logger", recording)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(pool_state.asyncio, "sleep", _no_sleep)
    w3 = type("W3", (), {"eth": _FailingEth()})()

    assert await _fetch_fee_with_retry(w3, "0xpool", "0xpool") is None
    failures = [fields for _, _, fields in recording.records if "error" in fields]
    assert len(failures) == 3
    assert all(_RPC_SECRET not in repr(fields) for fields in failures)
    assert all("[REDACTED]" in str(fields["error"]) for fields in failures)


class TestSwapToken0ForToken1:
    """Swap cNGN (t0) → USDC (t1) on Base pool."""

    def test_zero_amount_returns_zero(self):
        out = swap_token0_for_token1(Decimal("0"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out < Decimal("1e-10")  # numerically zero (Decimal precision artifact)

    def test_zero_liquidity_returns_zero(self):
        out = swap_token0_for_token1(Decimal("1000"), _BASE_SQRT_X96, Decimal("0"), _FEE, 6, 6)
        assert out == Decimal("0")

    def test_nonzero_amount_gives_reasonable_output(self):
        # Swap 1,000,000 cNGN → USDC. At price 0.000606 we expect ≈ 606 USDC before slippage
        out = swap_token0_for_token1(Decimal("1000000"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out > Decimal("0")
        # Output should be in the right ballpark (within 50% of naive expectation)
        assert Decimal("300") < out < Decimal("900")

    def test_larger_amount_gets_less_per_unit(self):
        """Slippage: larger swap gets fewer tokens per unit due to price impact."""
        out_small = swap_token0_for_token1(Decimal("100"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        out_large = swap_token0_for_token1(Decimal("10000"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        rate_small = out_small / Decimal("100")
        rate_large = out_large / Decimal("10000")
        assert rate_large < rate_small  # price impact

    def test_fee_reduces_output(self):
        out_no_fee = swap_token0_for_token1(Decimal("1000"), _BASE_SQRT_X96, _LIQ, Decimal("0"), 6, 6)
        out_with_fee = swap_token0_for_token1(Decimal("1000"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out_with_fee < out_no_fee


class TestSwapToken1ForToken0:
    """Swap USDC (t1) → cNGN (t0) on Base pool."""

    def test_zero_amount_returns_zero(self):
        out = swap_token1_for_token0(Decimal("0"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out < Decimal("1e-10")  # numerically zero

    def test_nonzero_amount_gives_reasonable_output(self):
        # Swap 100 USDC → cNGN. At price 0.000606, expect ≈ 165,000 cNGN before slippage
        out = swap_token1_for_token0(Decimal("100"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out > Decimal("0")
        assert Decimal("50000") < out < Decimal("300000")

    def test_fee_reduces_output(self):
        out_no_fee = swap_token1_for_token0(Decimal("100"), _BASE_SQRT_X96, _LIQ, Decimal("0"), 6, 6)
        out_with_fee = swap_token1_for_token0(Decimal("100"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out_with_fee < out_no_fee


# =============================================================================
# update_pool_state_from_event
# =============================================================================


class TestUpdatePoolStateFromEvent:
    """update_pool_state_from_event mutates _POOL_CACHE correctly."""

    def test_creates_new_entry(self, monkeypatch):
        from engine.market import pool_state as _ps
        fake_cache: dict = {}
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

        update_pool_state_from_event(
            pool_id="0xdeadbeef",
            sqrt_p=int(_BASE_SQRT_X96),
            liquidity=int(_LIQ),
            tick=-276324,
            fee=500,  # 0.05% in 1e6 units
        )

        assert "0xdeadbeef" in fake_cache
        entry = fake_cache["0xdeadbeef"]
        assert entry["tick"] == -276324
        assert entry["liquidity"] == _LIQ
        assert entry["fee"] == Decimal("500") / Decimal(1000000)
        assert entry["sqrt_p"] == Decimal(int(_BASE_SQRT_X96))

    def test_overwrites_existing_entry(self, monkeypatch):
        from engine.market import pool_state as _ps
        fake_cache = {
            "0xpool": {
                "tick": 0, "liquidity": Decimal(1), "fee": Decimal("0"),
                "sqrt_p": Decimal(1), "timestamp": 0,
            }
        }
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

        update_pool_state_from_event("0xpool", int(_BASE_SQRT_X96), int(_LIQ), -100, 500)

        entry = fake_cache["0xpool"]
        assert entry["tick"] == -100

    def test_timestamp_set(self, monkeypatch):
        from engine.market import pool_state as _ps
        fake_cache: dict = {}
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

        before = time.time()
        update_pool_state_from_event("0xpool2", 1, 1, 0, 500)
        after = time.time()

        assert before <= fake_cache["0xpool2"]["timestamp"] <= after


# =============================================================================
# get_cached_pool_state
# =============================================================================


class TestGetCachedPoolState:
    """get_cached_pool_state reads from _POOL_CACHE without RPC calls."""

    def test_cache_hit_returns_state(self, seeded_pool_cache):
        base_key = seeded_pool_cache["uni-base"]
        sqrt_p, liq, ts, fee = get_cached_pool_state(base_key)
        assert sqrt_p is not None
        assert liq == Decimal(10 ** 18)
        assert fee == Decimal("0.0005")

    def test_cold_cache_returns_nones(self, monkeypatch):
        from engine.market import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = get_cached_pool_state("0xnonexistent")
        assert all(v is None for v in result)

    def test_bsc_pool_state_readable(self, seeded_pool_cache):
        bsc_key = seeded_pool_cache["uni-bsc"]
        sqrt_p, liq, ts, fee = get_cached_pool_state(bsc_key)
        assert sqrt_p is not None and sqrt_p > 0
        assert fee == Decimal("0.0005")

    def test_price_in_expected_range(self, seeded_pool_cache):
        """Seeded sqrtPriceX96 should decode to price ≈ 0.000606 for Base pool."""
        base_key = seeded_pool_cache["uni-base"]
        sqrt_p, *_ = get_cached_pool_state(base_key)
        price = (sqrt_p / Q96) ** 2  # 6/6 dec → no adjustment
        assert Decimal("0.0004") < price < Decimal("0.0009")
