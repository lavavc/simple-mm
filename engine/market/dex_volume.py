"""Rolling 24h DEX volume tracking for V4 pools."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Deque
from collections.abc import Mapping

import structlog
from eth_typing import HexStr
from web3.types import LogReceipt
from web3 import AsyncWeb3
from web3.middleware import async_geth_poa_middleware  # type: ignore[attr-defined]

from engine.config import settings
from engine.venues.dex.shared import V4PoolReadConfig
from engine.web3_utils import as_hexstr, coerce_hex_bytes, coerce_hex_str

logger = structlog.get_logger()

V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
_WINDOW_MS = 24 * 60 * 60 * 1000
_DEFAULT_STORE_PATH = Path(settings.db_path).resolve().parent / "dex_volume_24h.json"
_PUBLIC_RPC_FALLBACKS = {
    "base": "https://mainnet.base.org",
    "bsc": "https://bsc-dataseed.binance.org",
}


def _int128_from_word(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=True)


def stable_volume_usd_from_v4_swap(raw_data: bytes | str, pool_config: V4PoolReadConfig) -> Decimal:
    """Return absolute stable-side USD volume from a V4 swap event payload."""
    data_bytes = coerce_hex_bytes(raw_data)

    if len(data_bytes) < 64:
        return Decimal("0")

    amount0 = _int128_from_word(data_bytes[0:32])
    amount1 = _int128_from_word(data_bytes[32:64])

    if pool_config.token0_symbol in {"USDT", "USDC"}:
        stable_raw = abs(amount0)
        decimals = pool_config.token0_decimals
    elif pool_config.token1_symbol in {"USDT", "USDC"}:
        stable_raw = abs(amount1)
        decimals = pool_config.token1_decimals
    else:
        return Decimal("0")

    return Decimal(stable_raw) / Decimal(10 ** decimals)


@dataclass
class _PoolVolumeState:
    swaps: Deque[tuple[int, Decimal, str | None]] = field(default_factory=deque)
    total_usd: Decimal = Decimal("0")
    seeded: bool = False
    event_ids: set[str] = field(default_factory=set)


class RollingDexVolumeStore:
    """Small rolling 24h volume store keyed by pool id."""

    def __init__(self, path: Path):
        self.path = path
        self._pools: dict[str, _PoolVolumeState] = {}
        self._loaded = False
        self._last_save = 0.0

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        if not self.path.exists():
            return

        try:
            payload = json.loads(self.path.read_text())
            for pool, data in payload.items():
                state = self._state(pool)
                state.seeded = bool(data.get("seeded", False))
                has_legacy_entries = False
                for entry in data.get("swaps", []):
                    if len(entry) >= 3:
                        ts_ms, usd_str, event_id = entry[0], entry[1], entry[2]
                    else:
                        ts_ms, usd_str = entry[0], entry[1]
                        event_id = None
                        has_legacy_entries = True
                    usd_volume = Decimal(str(usd_str))
                    state.swaps.append((int(ts_ms), usd_volume, event_id))
                    state.total_usd += usd_volume
                    if event_id:
                        state.event_ids.add(str(event_id))
                if has_legacy_entries and state.seeded:
                    state.seeded = False
            self._evict_all()
        except Exception as exc:
            logger.warning("dex_volume_store_load_failed", error=str(exc), path=str(self.path))

    def maybe_save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 30:
            return

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                pool: {
                    "seeded": state.seeded,
                    "swaps": [[ts_ms, str(usd), event_id] for ts_ms, usd, event_id in state.swaps],
                }
                for pool, state in self._pools.items()
            }
            self.path.write_text(json.dumps(payload))
            self._last_save = now
        except Exception as exc:
            logger.warning("dex_volume_store_save_failed", error=str(exc), path=str(self.path))

    def record(
        self,
        pool: str,
        usd_volume: Decimal,
        timestamp_ms: int | None = None,
        event_id: str | None = None,
        *,
        allow_unseeded: bool = False,
    ) -> None:
        if usd_volume <= 0:
            return

        self.load()
        state = self._state(pool)
        if not state.seeded and not allow_unseeded:
            return
        if event_id and event_id in state.event_ids:
            return
        ts_ms = timestamp_ms or int(time.time() * 1000)
        state.swaps.append((ts_ms, usd_volume, event_id))
        state.total_usd += usd_volume
        if event_id:
            state.event_ids.add(event_id)
        self._evict(pool)
        self.maybe_save()

    def mark_seeded(self, pool: str) -> None:
        self.load()
        state = self._state(pool)
        state.seeded = True
        self._evict(pool)
        self.maybe_save(force=True)

    def replace_seeded(self, pool: str, state: _PoolVolumeState) -> None:
        self.load()
        state.seeded = True
        self._pools[pool] = state
        self._evict(pool)
        self.maybe_save(force=True)

    def reset(self, pool: str) -> None:
        self.load()
        state = self._state(pool)
        state.swaps.clear()
        state.total_usd = Decimal("0")
        state.seeded = False
        state.event_ids.clear()
        self.maybe_save(force=True)

    def is_seeded(self, pool: str) -> bool:
        self.load()
        return self._state(pool).seeded

    def get_24h_volume_usd(self, pool: str) -> Decimal:
        self.load()
        if not self._state(pool).seeded:
            return Decimal("0")
        self._evict(pool)
        return self._state(pool).total_usd

    def _state(self, pool: str) -> _PoolVolumeState:
        return self._pools.setdefault(pool, _PoolVolumeState())

    def _evict(self, pool: str) -> None:
        state = self._state(pool)
        cutoff = int(time.time() * 1000) - _WINDOW_MS
        while state.swaps and state.swaps[0][0] < cutoff:
            _, usd_volume, event_id = state.swaps.popleft()
            state.total_usd -= usd_volume
            if event_id:
                state.event_ids.discard(event_id)
        if state.total_usd < 0:
            state.total_usd = Decimal("0")

    def _evict_all(self) -> None:
        for pool in list(self._pools):
            self._evict(pool)


_STORE = RollingDexVolumeStore(_DEFAULT_STORE_PATH)


def _rpc_candidates(config: V4PoolReadConfig) -> list[str]:
    candidates = [config.rpc_url]
    fallback = _PUBLIC_RPC_FALLBACKS.get(config.chain_id_str)
    if fallback and fallback not in candidates:
        candidates.append(fallback)
    return candidates


def _make_async_w3(config: V4PoolReadConfig, rpc_url: str) -> AsyncWeb3:
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    if config.chain_id_str == "bsc":
        w3.middleware_onion.inject(async_geth_poa_middleware, layer=0)
    return w3


def _log_chunk_size(config: V4PoolReadConfig) -> int:
    if config.chain_id_str == "bsc":
        return 5_000
    return 50_000


def event_id_from_log(log: Mapping[str, Any] | LogReceipt) -> str | None:
    tx_hash = log.get("transactionHash")
    if tx_hash is None:
        return None
    tx_hash_str = coerce_hex_str(tx_hash)

    log_index = log.get("logIndex")
    if log_index is None:
        return None
    if isinstance(log_index, str):
        log_index = int(log_index, 16)
    else:
        log_index = int(log_index)
    return f"{tx_hash_str}:{log_index}"


async def _block_timestamp_ms(w3: AsyncWeb3, block_number: int, cache: dict[int, int]) -> int:
    if block_number not in cache:
        block = await w3.eth.get_block(block_number)
        cache[block_number] = int(block["timestamp"]) * 1000
    return cache[block_number]


async def _find_start_block_24h_ago(w3: AsyncWeb3) -> int:
    latest = await w3.eth.get_block("latest")
    latest_number = int(latest["number"])
    target_ts = int(latest["timestamp"]) - 86400

    lo = 0
    hi = latest_number
    while lo < hi:
        mid = (lo + hi) // 2
        block = await w3.eth.get_block(mid)
        if int(block["timestamp"]) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


async def _scan_pool_window_from_rpc(
    config: V4PoolReadConfig,
    rpc_url: str,
) -> _PoolVolumeState:
    w3 = _make_async_w3(config, rpc_url)
    latest = await w3.eth.get_block("latest")
    latest_number = int(latest["number"])
    state = _PoolVolumeState()

    start_24h_block = await _find_start_block_24h_ago(w3)
    if start_24h_block > latest_number:
        return state

    block_ts_cache: dict[int, int] = {}
    chunk_size = _log_chunk_size(config)
    for chunk_start in range(start_24h_block, latest_number + 1, chunk_size):
        chunk_end = min(chunk_start + chunk_size - 1, latest_number)
        logs = await w3.eth.get_logs({
            "address": AsyncWeb3.to_checksum_address(config.pool_manager),
            "topics": [as_hexstr(V4_SWAP_TOPIC), as_hexstr(config.pool_address)],
            "fromBlock": chunk_start,
            "toBlock": chunk_end,
        })

        for log in logs:
            usd_volume = stable_volume_usd_from_v4_swap(coerce_hex_bytes(log["data"]), config)
            if usd_volume <= 0:
                continue
            block_number = int(log["blockNumber"])
            ts_ms = await _block_timestamp_ms(w3, block_number, block_ts_cache)
            event_id = event_id_from_log(log)
            if event_id and event_id in state.event_ids:
                continue
            state.swaps.append((ts_ms, usd_volume, event_id))
            state.total_usd += usd_volume
            if event_id:
                state.event_ids.add(event_id)

    return state


async def _refresh_pool(config: V4PoolReadConfig) -> None:
    last_error: Exception | None = None
    was_seeded = _STORE.is_seeded(config.pool_address)
    for rpc_url in _rpc_candidates(config):
        try:
            state = await _scan_pool_window_from_rpc(config, rpc_url)
            if was_seeded:
                logger.info("dex_volume_resync_succeeded", pool=config.pool_address, rpc=rpc_url)
            else:
                logger.info("dex_volume_backfill_succeeded", pool=config.pool_address, rpc=rpc_url)
            _STORE.replace_seeded(config.pool_address, state)
            return
        except Exception as exc:
            last_error = exc
            logger.warning("dex_volume_backfill_rpc_failed", pool=config.pool_address, rpc=rpc_url, error=str(exc))
    _STORE.reset(config.pool_address)
    if last_error:
        raise last_error


async def seed_dex_volume_24h(configs: list[V4PoolReadConfig] | None = None) -> None:
    """Restore and backfill rolling 24h volume for tracked DEX pools."""
    if configs is None:
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        configs = [UNISWAP_BSC_POOL_READ_CONFIG, UNISWAP_BASE_POOL_READ_CONFIG]

    _STORE.load()

    for config in configs:
        try:
            await _refresh_pool(config)
        except Exception as exc:
            logger.warning("dex_volume_backfill_failed", pool=config.pool_address, error=str(exc))


async def sync_pool_volume_24h(config: V4PoolReadConfig) -> None:
    """Refresh one pool's rolling volume window, filling missed swaps if possible."""
    _STORE.load()
    try:
        await _refresh_pool(config)
    except Exception as exc:
        logger.warning("dex_volume_sync_failed", pool=config.pool_address, error=str(exc))


def record_live_v4_swap_volume(
    pool_config: V4PoolReadConfig,
    raw_data: bytes | str,
    event_id: str | None = None,
) -> None:
    usd_volume = stable_volume_usd_from_v4_swap(raw_data, pool_config)
    _STORE.record(
        pool_config.pool_address,
        usd_volume,
        timestamp_ms=int(time.time() * 1000),
        event_id=event_id,
    )


def get_pool_volume_24h_usd(pool_address: str) -> Decimal | None:
    volume = _STORE.get_24h_volume_usd(pool_address)
    return volume if volume > 0 else None
