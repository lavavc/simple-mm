import asyncio
import json
import time
import websockets
import structlog
from typing import Callable, Any

from engine.config import settings
from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
from engine.core.arbitrage.detector import generate_v3_profit_curve
from engine.core.arbitrage.pool_state import update_pool_state_from_event

logger = structlog.get_logger()

# Uniswap V4 Swap event topic
# Keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"

class ArbitrageWebSocketListener:
    """Listens for DEX Swaps via WebSockets to trigger curve calculations instantly."""

    def __init__(self, broadcast: Callable[[dict], Any], on_update: Callable[[], Any] | None = None):
        self.broadcast = broadcast
        self.on_update = on_update
        self._running = False

        self._tasks: list[asyncio.Task] = []

        # Debounce tracking
        self._last_trigger_time = 0.0
        self._debounce_delay = 0.25 # seconds to wait before calculating curve
        self._pending_calculation: asyncio.Task | None = None
        self.active_connections: set[str] = set()

    async def start(self):
        """Start listening to all supported WebSocket endpoints."""
        if self._running:
            return

        self._running = True
        logger.info("arbitrage_websocket_listener_starting")

        if settings.base_wss_url:
            self._tasks.append(asyncio.create_task(
                self._listen_to_chain("base", settings.base_wss_url, UNISWAP_BASE_POOL_READ_CONFIG)
            ))

        if settings.bsc_wss_url:
            self._tasks.append(asyncio.create_task(
                self._listen_to_chain("bsc", settings.bsc_wss_url, UNISWAP_BSC_POOL_READ_CONFIG)
            ))

    async def stop(self):
        """Stop all listeners."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._pending_calculation:
            self._pending_calculation.cancel()
        logger.info("arbitrage_websocket_listener_stopped")

    async def _listen_to_chain(self, chain_name: str, wss_url: str, pool_config):
        """Persistent wss connection loop for a specific chain."""
        backoff = 1

        while self._running:
            try:
                async with websockets.connect(wss_url) as ws:
                    logger.info("wss_connected", chain=chain_name, pool_manager=pool_config.pool_manager)
                    self.active_connections.add(chain_name)
                    backoff = 1 # Reset backoff

                    payload = {
                        "id": 1,
                        "jsonrpc": "2.0",
                        "method": "eth_subscribe",
                        "params": [
                            "logs",
                            {
                                "address": pool_config.pool_manager,
                                "topics": [V4_SWAP_TOPIC, pool_config.pool_address]
                            }
                        ]
                    }

                    await ws.send(json.dumps(payload))

                    response = await ws.recv()
                    logger.debug("wss_subscribed", chain=chain_name, response=response)

                    while self._running:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        if data.get("method") == "eth_subscription":
                            log = data.get("params", {}).get("result", {})
                            self._parse_and_update_state(log, pool_config)
                            self._trigger_curve_calculation(chain_name)

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

    def _parse_and_update_state(self, log: dict, pool_config):
        """Parse V4 Swap event data and update the pool cache — zero RPC calls."""
        try:
            raw_data = log.get("data", "0x")
            data_bytes = bytes.fromhex(raw_data[2:] if raw_data.startswith("0x") else raw_data)

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
            logger.debug("v4_swap_state_updated", pool=pool_config.pool_address, tick=tick)
        except Exception as e:
            logger.error("v4_swap_event_parse_failed", error=str(e))

    def _trigger_curve_calculation(self, source_chain: str):
        """Debounced trigger for the profit curve."""
        logger.debug("swap_event_detected", chain=source_chain)

        if self._pending_calculation and not self._pending_calculation.done():
            return

        self._pending_calculation = asyncio.create_task(self._delayed_calculation())

    async def _delayed_calculation(self):
        """Waits for the debounce window, then calculates and broadcasts the curve.

        V4 state is already updated synchronously from the event — no RPC calls needed.
        """
        await asyncio.sleep(self._debounce_delay)

        try:
            logger.info("executing_event_driven_curve_calc")

            if self.on_update:
                await self.on_update()

            curve_data = await generate_v3_profit_curve()

            if curve_data:
                from engine.api.schemas import PriceQuote
                from engine.db.database import get_db
                import time

                db = await get_db()
                now_ms = int(time.time() * 1000)

                for key in ("uni-bsc", "uni-base", "assetchain"):
                    price_val = curve_data.get("prices", {}).get(key)
                    if price_val is not None:
                        await db.insert_price_snapshot(PriceQuote(
                            source=f"{key}_pool",
                            timestamp=now_ms,
                            bid=price_val,
                            ask=price_val,
                            mid=price_val,
                        ))

                self.broadcast({
                    "type": "dex_arb_curve",
                    "data": curve_data
                })

                optimal = curve_data.get("optimal_arb", {})
                if optimal.get("expected_profit_usd", -1) > 0:
                    await self._record_and_broadcast_opportunity(curve_data, optimal)

        except Exception as e:
            logger.error("event_driven_curve_calc_failed", error=str(e))

    async def _record_and_broadcast_opportunity(self, curve_data: dict, optimal: dict):
        """Records a profitable opp to DB and broadcasts it."""
        import uuid
        from engine.api.schemas import DexArbOpportunity
        from engine.db.database import get_db

        db = await get_db()

        cutoff_ts = int(time.time() * 1000) - 60000
        await db.expire_old_dex_arbitrage_opportunities(cutoff_ts)

        direction = optimal["direction"]
        existing_active = await db._conn.execute(
            "SELECT id FROM dex_arbitrage_opportunities WHERE status IN ('detected', 'executing') AND direction = ? ORDER BY timestamp DESC LIMIT 1",
            (direction,)
        )
        existing_row = await existing_active.fetchone()

        if existing_row:
            opp_id = existing_row['id']
        else:
            opp_id = f"dex-arb-{uuid.uuid4()}"
            opportunity = DexArbOpportunity(
                id=opp_id,
                timestamp=int(time.time() * 1000),
                direction=direction,
                optimal_size_usd=optimal["optimal_size_usd"],
                expected_profit_usd=optimal["expected_profit_usd"],
                cngn_transferred=optimal["cngn_transferred"],
                expected_usd_out=optimal["expected_usd_out"],
                status="detected",
                net_spread_bps=optimal.get("net_spread_bps", 0),
                pancake_price=curve_data.get("prices", {}).get("uni-bsc"),
                aerodrome_price=curve_data.get("prices", {}).get("uni-base"),
                slippage_tolerance_bps=optimal.get("slippage_tolerance_bps"),
                pancake_fee_bps=optimal.get("uni_bsc_fee_bps"),
                aerodrome_fee_bps=optimal.get("uni_base_fee_bps"),
                estimated_gas_usd=optimal.get("estimated_gas_usd")
            )
            await db.insert_dex_arbitrage_opportunity(opportunity)

        broadcast_data = optimal.copy()
        broadcast_data["id"] = opp_id

        self.broadcast({
            "type": "dex_arb_opportunity",
            "data": broadcast_data
        })
