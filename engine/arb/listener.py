import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import structlog
import websockets

from engine.config import settings
from engine.market.pool_state import update_single_v4_pool_state
from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
from engine.market.dex_volume import (
    V4_SWAP_TOPIC,
    event_id_from_log,
    record_live_v4_swap_volume,
    sync_pool_volume_24h,
)
from engine.market.pool_state import update_pool_state_from_event
from engine.web3_utils import coerce_hex_bytes

logger = structlog.get_logger()
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_WSS_IDLE_RECV_TIMEOUT_SECONDS = 30
_WSS_PING_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WalletActivitySubscription:
    """Wallet + token pair to watch for executable inventory changes."""

    venue_name: str
    wallet_address: str
    token_address: str


def _normalize_address(value: str | None) -> str | None:
    if not value:
        return None
    return value.lower().removeprefix("0x")


def _topic_address(topic: str | None) -> str | None:
    if not topic:
        return None
    normalized = topic.lower().removeprefix("0x")
    if len(normalized) < 40:
        return None
    return normalized[-40:]


def _topic_filter_address(address: str | None) -> str | None:
    normalized = _normalize_address(address)
    if normalized is None:
        return None
    return "0x" + ("0" * 24) + normalized


def build_wallet_transfer_filters(
    subscriptions: Iterable[WalletActivitySubscription],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Build wallet-targeted Transfer subscriptions instead of token-wide firehoses."""
    subs_by_token: dict[str, list[WalletActivitySubscription]] = defaultdict(list)
    for sub in subscriptions:
        token_address = _normalize_address(sub.token_address)
        if token_address is None:
            continue
        subs_by_token[token_address].append(sub)

    filters: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for token_address, token_subs in subs_by_token.items():
        filter_token_address = f"0x{token_address}"
        wallet_topics = sorted(
            {
                topic
                for sub in token_subs
                if (topic := _topic_filter_address(sub.wallet_address)) is not None
            }
        )
        if not wallet_topics:
            continue

        filters.append(
            (
                {
                    "address": filter_token_address,
                    "topics": [ERC20_TRANSFER_TOPIC, wallet_topics],
                },
                {
                    "kind": "wallet_transfer",
                    "direction": "outgoing",
                    "token_address": filter_token_address,
                    "wallet_subscriptions": token_subs,
                },
            )
        )
        filters.append(
            (
                {
                    "address": filter_token_address,
                    "topics": [ERC20_TRANSFER_TOPIC, None, wallet_topics],
                },
                {
                    "kind": "wallet_transfer",
                    "direction": "incoming",
                    "token_address": filter_token_address,
                    "wallet_subscriptions": token_subs,
                },
            )
        )

    return filters


def matching_wallet_venues(
    log: dict[str, Any],
    subscriptions: Iterable[WalletActivitySubscription],
) -> set[str]:
    """Return affected venue names when a Transfer log touches tracked wallets."""
    token_address = _normalize_address(log.get("address"))
    topics = log.get("topics") or []
    if token_address is None or len(topics) < 3:
        return set()

    from_address = _topic_address(topics[1])
    to_address = _topic_address(topics[2])
    if from_address is None and to_address is None:
        return set()

    matched: set[str] = set()
    for sub in subscriptions:
        if _normalize_address(sub.token_address) != token_address:
            continue
        wallet_address = _normalize_address(sub.wallet_address)
        if wallet_address in (from_address, to_address):
            matched.add(sub.venue_name)
    return matched

class ArbitrageWebSocketListener:
    """Listens for DEX Swaps via WebSockets to trigger curve calculations instantly."""

    def __init__(
        self,
        broadcast: Callable[[dict[str, Any]], Any],
        on_update: Callable[[], Any] | None = None,
        on_dex_event: Callable[[], Any] | None = None,
        on_wallet_event: Callable[[list[str]], Any] | None = None,
        wallet_subscriptions: dict[str, list[WalletActivitySubscription]] | None = None,
    ):
        self.broadcast = broadcast
        self.on_update = on_update
        self.on_dex_event = on_dex_event
        self.on_wallet_event = on_wallet_event
        self.wallet_subscriptions = wallet_subscriptions or {}
        self._running = False

        self._tasks: list[asyncio.Task[Any]] = []

        # Debounce tracking
        self._debounce_delay = 0.25 # seconds to wait before calculating curve
        self._pending_calculation: asyncio.Task[Any] | None = None
        self._pending_market_update = False
        self._pending_wallet_venues: set[str] = set()
        self.active_connections: set[str] = set()

    async def start(self) -> None:
        """Start listening to all supported WebSocket endpoints."""
        if self._running:
            return

        self._running = True
        logger.info("arbitrage_websocket_listener_starting")

        if settings.base_wss_url:
            self._tasks.append(asyncio.create_task(
                self._listen_to_chain(
                    "base",
                    settings.base_wss_url,
                    UNISWAP_BASE_POOL_READ_CONFIG,
                    self.wallet_subscriptions.get("base", []),
                )
            ))

        if settings.bsc_wss_url:
            self._tasks.append(asyncio.create_task(
                self._listen_to_chain(
                    "bsc",
                    settings.bsc_wss_url,
                    UNISWAP_BSC_POOL_READ_CONFIG,
                    self.wallet_subscriptions.get("bsc", []),
                )
            ))

    async def stop(self) -> None:
        """Stop all listeners."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._pending_calculation:
            self._pending_calculation.cancel()
        logger.info("arbitrage_websocket_listener_stopped")

    async def _refresh_pool_state(self, chain_name: str, pool_config: Any) -> None:
        """Refresh pool cache from RPC after (re)connect so prices aren't stuck stale."""
        try:
            if await update_single_v4_pool_state(pool_config):
                logger.info("wss_pool_state_refreshed", chain=chain_name, pool=pool_config.pool_address)
                self._trigger_market_update(chain_name)
        except Exception as e:
            logger.warning("wss_pool_state_refresh_failed", chain=chain_name, error=str(e))

    async def _recv_with_keepalive(self, ws: Any, chain_name: str) -> str:
        """Wait for the next WS message, using ping/pong to detect zombie sockets."""
        while self._running:
            try:
                return await asyncio.wait_for(ws.recv(), timeout=_WSS_IDLE_RECV_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("wss_idle_timeout", chain=chain_name)
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=_WSS_PING_TIMEOUT_SECONDS)
                logger.debug("wss_keepalive_ok", chain=chain_name)
        raise RuntimeError("listener stopped")

    async def _listen_to_chain(
        self,
        chain_name: str,
        wss_url: str,
        pool_config: Any,
        wallet_subscriptions: list[WalletActivitySubscription],
    ) -> None:
        """Persistent wss connection loop for a specific chain."""
        backoff = 1

        while self._running:
            try:
                async with websockets.connect(wss_url) as ws:
                    logger.info("wss_connected", chain=chain_name, pool_manager=pool_config.pool_manager)
                    self.active_connections.add(chain_name)
                    backoff = 1 # Reset backoff

                    subscriptions: dict[str, dict[str, Any]] = {}

                    def _handle_subscription_event(subscription: dict[str, Any], log: dict[str, Any]) -> None:
                        if subscription["kind"] == "pool_swap":
                            self._parse_and_update_state(log, subscription["pool_config"])
                            self._trigger_market_update(chain_name)
                            return

                        affected_venues = matching_wallet_venues(log, subscription["wallet_subscriptions"])
                        if affected_venues:
                            self._trigger_wallet_update(
                                chain_name,
                                affected_venues,
                                token_address=subscription["token_address"],
                            )

                    async def _subscribe(filter_params: dict[str, Any], metadata: dict[str, Any], request_id: int) -> None:
                        payload = {
                            "id": request_id,
                            "jsonrpc": "2.0",
                            "method": "eth_subscribe",
                            "params": ["logs", filter_params],
                        }
                        await ws.send(json.dumps(payload))
                        while True:
                            response = json.loads(await ws.recv())
                            if response.get("id") == request_id:
                                subscription_id = response.get("result")
                                if not subscription_id:
                                    raise RuntimeError(f"missing subscription id for {metadata['kind']}: {response}")
                                break

                            # Events that arrive before the handshake response for *this*
                            # subscription are dispatched to already-registered subscriptions.
                            # Events for the current (not-yet-registered) subscription id are
                            # silently dropped — they can't be handled before metadata is stored.
                            if response.get("method") == "eth_subscription":
                                subscription_id = response.get("params", {}).get("subscription")
                                subscription = subscriptions.get(subscription_id)
                                if subscription:
                                    _handle_subscription_event(
                                        subscription,
                                        response.get("params", {}).get("result", {}),
                                    )
                        subscriptions[subscription_id] = metadata
                        logger.debug(
                            "wss_subscribed",
                            chain=chain_name,
                            kind=metadata["kind"],
                            subscription_id=subscription_id,
                        )

                    await _subscribe(
                        {
                            "address": pool_config.pool_manager,
                            "topics": [V4_SWAP_TOPIC, pool_config.pool_address],
                        },
                        {"kind": "pool_swap", "pool_config": pool_config},
                        request_id=1,
                    )

                    request_id = 2
                    for filter_params, metadata in build_wallet_transfer_filters(wallet_subscriptions):
                        await _subscribe(
                            filter_params,
                            metadata,
                            request_id=request_id,
                        )
                        request_id += 1

                    await sync_pool_volume_24h(pool_config)
                    await self._refresh_pool_state(chain_name, pool_config)

                    while self._running:
                        msg = await self._recv_with_keepalive(ws, chain_name)
                        data = json.loads(msg)

                        if data.get("method") == "eth_subscription":
                            subscription_id = data.get("params", {}).get("subscription")
                            subscription = subscriptions.get(subscription_id)
                            if not subscription:
                                continue
                            log = data.get("params", {}).get("result", {})
                            _handle_subscription_event(subscription, log)

            except websockets.exceptions.ConnectionClosed as e:
                self.active_connections.discard(chain_name)
                logger.warning("wss_connection_closed", chain=chain_name, code=e.code, reason=e.reason)
            except Exception as e:
                self.active_connections.discard(chain_name)
                logger.error("wss_connection_error", chain=chain_name, error=str(e))

            if self._running:
                logger.info("wss_reconnecting", chain=chain_name, backoff_seconds=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _parse_and_update_state(self, log: dict[str, Any], pool_config: Any) -> None:
        """Parse V4 Swap event data and update the pool cache — zero RPC calls."""
        try:
            data_bytes = coerce_hex_bytes(log.get("data", "0x"))

            if len(data_bytes) < 192:
                logger.warning("v4_swap_event_data_too_short", length=len(data_bytes))
                return

            # V4 Swap non-indexed data layout (32 bytes each):
            # [0:32]   amount0 (int128)
            # [32:64]  amount1 (int128)
            # [64:96]  sqrtPriceX96 (uint160)
            # [96:128] liquidity (uint128)
            # [128:160] tick (int24, signed)
            # [160:192] fee (uint24)
            sqrt_p = int.from_bytes(data_bytes[64:96], "big")
            liquidity = int.from_bytes(data_bytes[96:128], "big")
            tick = int.from_bytes(data_bytes[128:160], "big", signed=True)
            fee = int.from_bytes(data_bytes[160:192], "big")

            update_pool_state_from_event(pool_config.pool_address, sqrt_p, liquidity, tick, fee)
            record_live_v4_swap_volume(
                pool_config,
                data_bytes,
                event_id=event_id_from_log(log),
            )
            logger.debug("v4_swap_state_updated", pool=pool_config.pool_address, tick=tick)
        except Exception as e:
            logger.error("v4_swap_event_parse_failed", error=str(e))

    def _ensure_pending_calculation(self) -> None:
        if self._pending_calculation and not self._pending_calculation.done():
            return
        self._pending_calculation = asyncio.create_task(self._delayed_calculation())

    def _trigger_market_update(self, source_chain: str) -> None:
        """Debounced trigger for market-state updates from pool swaps."""
        logger.debug("swap_event_detected", chain=source_chain)
        self._pending_market_update = True
        self._ensure_pending_calculation()

    def _trigger_wallet_update(
        self,
        source_chain: str,
        venue_names: Iterable[str],
        *,
        token_address: str,
    ) -> None:
        """Debounced trigger for wallet inventory refreshes."""
        venues = sorted(set(venue_names))
        if not venues:
            return
        logger.debug(
            "wallet_activity_detected",
            chain=source_chain,
            venues=venues,
            token_address=token_address,
        )
        self._pending_wallet_venues.update(venues)
        self._ensure_pending_calculation()

    async def _delayed_calculation(self) -> None:
        """Wait for debounce, then recompute once for all accumulated signals.

        Loops so that signals arriving *during* processing are batched into the
        next wake-up rather than dropped, but exits as soon as both pending
        sets are empty after processing — draining bursts without busy-waiting.
        """
        try:
            while self._running:
                await asyncio.sleep(self._debounce_delay)

                market_update = self._pending_market_update
                wallet_venues = sorted(self._pending_wallet_venues)
                self._pending_market_update = False
                self._pending_wallet_venues.clear()

                if not market_update and not wallet_venues:
                    break

                logger.info(
                    "executing_event_driven_arb_calc",
                    market_update=market_update,
                    wallet_venues=wallet_venues,
                )
                if market_update and self.on_update:
                    await self.on_update()
                if wallet_venues and self.on_wallet_event:
                    await self.on_wallet_event(wallet_venues)
                if (market_update or wallet_venues) and self.on_dex_event:
                    await self.on_dex_event()

                if not self._pending_market_update and not self._pending_wallet_venues:
                    break
        finally:
            self._pending_calculation = None
