import asyncio
import time
import json
from decimal import Decimal
from pathlib import Path

import pytest
import engine.core.arbitrage.dex_volume as dex_volume
from engine.core.arbitrage.dex_volume import (
    RollingDexVolumeStore,
    _rpc_candidates,
    _refresh_pool,
    event_id_from_log,
    stable_volume_usd_from_v4_swap,
)
from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG


def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big", signed=True)


def test_stable_volume_from_base_swap_uses_amount1():
    amount0 = -103_427_340_000  # cNGN raw (6 decimals)
    amount1 = 73_649_100        # USDC raw (6 decimals)
    payload = b"".join([
        _word(amount0),
        _word(amount1),
        bytes(32),
        bytes(32),
        bytes(32),
        bytes(32),
    ])

    volume = stable_volume_usd_from_v4_swap(payload, UNISWAP_BASE_POOL_READ_CONFIG)
    assert volume == Decimal("73.6491")


def test_stable_volume_from_bsc_swap_uses_amount0():
    amount0 = 73_577_300_000_000_000_000  # USDT raw (18 decimals)
    amount1 = -103_427_340_000            # cNGN raw (6 decimals)
    payload = b"".join([
        _word(amount0),
        _word(amount1),
        bytes(32),
        bytes(32),
        bytes(32),
        bytes(32),
    ])

    volume = stable_volume_usd_from_v4_swap(payload, UNISWAP_BSC_POOL_READ_CONFIG)
    assert volume == Decimal("73.5773")


def test_rolling_store_evicts_old_swaps_and_persists_seeded_state(tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)
    too_old = now_ms - (24 * 60 * 60 * 1000) - 1

    store.record("pool", Decimal("10"), timestamp_ms=too_old, event_id="tx1:0", allow_unseeded=True)
    store.record("pool", Decimal("25"), timestamp_ms=now_ms, event_id="tx2:0", allow_unseeded=True)
    store.mark_seeded("pool")

    store.maybe_save(force=True)
    reloaded = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    reloaded.load()

    assert reloaded.get_24h_volume_usd("pool") == Decimal("25")
    assert reloaded.is_seeded("pool") is True


def test_rpc_candidates_include_public_chain_fallbacks():
    base_candidates = _rpc_candidates(UNISWAP_BASE_POOL_READ_CONFIG)
    bsc_candidates = _rpc_candidates(UNISWAP_BSC_POOL_READ_CONFIG)

    assert "https://mainnet.base.org" in base_candidates
    assert "https://bsc-dataseed.binance.org" in bsc_candidates


def test_unseeded_pool_volume_stays_hidden_and_live_updates_are_ignored(tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)

    store.record("pool", Decimal("99.99"), timestamp_ms=now_ms)
    assert store.get_24h_volume_usd("pool") == Decimal("0")
    assert store.is_seeded("pool") is False

    store.record("pool", Decimal("99.99"), timestamp_ms=now_ms, event_id="tx1:0", allow_unseeded=True)
    assert store.get_24h_volume_usd("pool") == Decimal("0")
    store.mark_seeded("pool")
    assert store.get_24h_volume_usd("pool") == Decimal("99.99")


def test_reset_clears_partial_unseeded_state(tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)

    store.record("pool", Decimal("25"), timestamp_ms=now_ms, event_id="tx1:0", allow_unseeded=True)
    assert store.is_seeded("pool") is False

    store.reset("pool")
    assert store.is_seeded("pool") is False
    assert store.get_24h_volume_usd("pool") == Decimal("0")


def test_duplicate_event_id_is_not_double_counted(tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)

    store.record("pool", Decimal("50"), timestamp_ms=now_ms, event_id="tx1:0", allow_unseeded=True)
    store.mark_seeded("pool")
    store.record("pool", Decimal("50"), timestamp_ms=now_ms, event_id="tx1:0")

    assert store.get_24h_volume_usd("pool") == Decimal("50")


def test_legacy_store_forces_reseed_before_exposing_volume(tmp_path):
    path = Path(tmp_path) / "dex_volume.json"
    path.write_text(json.dumps({
        "pool": {
            "seeded": True,
            "swaps": [[int(time.time() * 1000), "25"]],
        }
    }))

    store = RollingDexVolumeStore(path)
    assert store.is_seeded("pool") is False
    assert store.get_24h_volume_usd("pool") == Decimal("0")


def test_event_id_from_log_handles_hex_strings():
    log = {
        "transactionHash": "0xabc",
        "logIndex": "0x2",
    }
    assert event_id_from_log(log) == "0xabc:2"


def test_refresh_failure_hides_seeded_volume(monkeypatch, tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)
    store.record("pool", Decimal("25"), timestamp_ms=now_ms, event_id="tx1:0", allow_unseeded=True)
    store.mark_seeded("pool")

    monkeypatch.setattr(dex_volume, "_STORE", store)

    async def _boom(config, rpc_url: str):
        raise RuntimeError("fail")

    monkeypatch.setattr(dex_volume, "_scan_pool_window_from_rpc", _boom)
    monkeypatch.setattr(dex_volume, "_rpc_candidates", lambda config: ["rpc1"])

    class _Config:
        pool_address = "pool"
        rpc_url = "rpc1"
        chain_id_str = "base"

    with pytest.raises(RuntimeError, match="fail"):
        asyncio.run(_refresh_pool(_Config()))

    assert store.is_seeded("pool") is False
    assert store.get_24h_volume_usd("pool") == Decimal("0")


def test_refresh_failure_does_not_leave_partial_volume_visible(monkeypatch, tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)
    store.record("pool", Decimal("25"), timestamp_ms=now_ms, event_id="tx1:0", allow_unseeded=True)
    store.mark_seeded("pool")

    monkeypatch.setattr(dex_volume, "_STORE", store)

    async def _partial(config, rpc_url: str):
        staged = dex_volume._PoolVolumeState()
        staged.swaps.append((now_ms, Decimal("10"), "tx2:0"))
        staged.total_usd = Decimal("10")
        staged.event_ids.add("tx2:0")
        raise RuntimeError("midway fail")

    monkeypatch.setattr(dex_volume, "_scan_pool_window_from_rpc", _partial)
    monkeypatch.setattr(dex_volume, "_rpc_candidates", lambda config: ["rpc1"])

    class _Config:
        pool_address = "pool"
        rpc_url = "rpc1"
        chain_id_str = "base"

    with pytest.raises(RuntimeError, match="midway fail"):
        asyncio.run(_refresh_pool(_Config()))

    assert store.is_seeded("pool") is False
    assert store.get_24h_volume_usd("pool") == Decimal("0")


def test_refresh_keeps_last_good_volume_visible_until_new_window_is_ready(monkeypatch, tmp_path):
    store = RollingDexVolumeStore(tmp_path / "dex_volume.json")
    now_ms = int(time.time() * 1000)
    store.record("pool", Decimal("25"), timestamp_ms=now_ms, event_id="tx1:0", allow_unseeded=True)
    store.mark_seeded("pool")

    monkeypatch.setattr(dex_volume, "_STORE", store)
    observed: dict[str, Decimal | bool] = {}

    async def _scan(config, rpc_url: str):
        observed["seeded_during_scan"] = store.is_seeded("pool")
        observed["volume_during_scan"] = store.get_24h_volume_usd("pool")
        staged = dex_volume._PoolVolumeState()
        staged.swaps.append((now_ms, Decimal("40"), "tx2:0"))
        staged.total_usd = Decimal("40")
        staged.event_ids.add("tx2:0")
        return staged

    monkeypatch.setattr(dex_volume, "_scan_pool_window_from_rpc", _scan)
    monkeypatch.setattr(dex_volume, "_rpc_candidates", lambda config: ["rpc1"])

    class _Config:
        pool_address = "pool"
        rpc_url = "rpc1"
        chain_id_str = "base"

    asyncio.run(_refresh_pool(_Config()))

    assert observed["seeded_during_scan"] is True
    assert observed["volume_during_scan"] == Decimal("25")
    assert store.is_seeded("pool") is True
    assert store.get_24h_volume_usd("pool") == Decimal("40")
