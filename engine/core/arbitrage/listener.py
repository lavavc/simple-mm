import asyncio
import json
import time
import websockets
import structlog
from typing import Callable, Any

from engine.config import settings
from engine.venues.dex.aerodrome import AERODROME_POOL_READ_CONFIG
from engine.venues.dex.pancakeswap import PANCAKESWAP_POOL_READ_CONFIG
from engine.core.arbitrage.simulator import generate_v3_profit_curve

logger = structlog.get_logger()

# Uniswap V3 Swap event topic
# Keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)")
SWAP_EVENT_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

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
                self._listen_to_chain("base", settings.base_wss_url, AERODROME_POOL_READ_CONFIG)
            ))
            
        if settings.bsc_wss_url:
            self._tasks.append(asyncio.create_task(
                self._listen_to_chain("bsc", settings.bsc_wss_url, PANCAKESWAP_POOL_READ_CONFIG)
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
                    logger.info("wss_connected", chain=chain_name)
                    self.active_connections.add(chain_name)
                    backoff = 1 # Reset backoff
                    
                    # Create subscription payload
                    payload = {
                        "id": 1,
                        "jsonrpc": "2.0",
                        "method": "eth_subscribe",
                        "params": [
                            "logs",
                            {
                                "address": pool_config.pool_address,
                                "topics": [SWAP_EVENT_TOPIC]
                            }
                        ]
                    }
                    
                    await ws.send(json.dumps(payload))
                    
                    # Wait for subscription confirmation
                    response = await ws.recv()
                    logger.debug("wss_subscribed", chain=chain_name, response=response)
                    
                    # Listen for incoming events
                    while self._running:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        # Verify it's actually an event notification
                        if data.get("method") == "eth_subscription":
                            self._trigger_curve_calculation(chain_name, wss_url, pool_config)
                            
            except websockets.exceptions.ConnectionClosed as e:
                self.active_connections.discard(chain_name)
                logger.warning("wss_connection_closed", chain=chain_name, code=e.code, reason=e.reason)
            except Exception as e:
                self.active_connections.discard(chain_name)
                logger.error("wss_connection_error", chain=chain_name, error=str(e))
                
            if self._running:
                logger.info("wss_reconnecting", chain=chain_name, backoff_seconds=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60) # Max 60s backoff

    def _trigger_curve_calculation(self, source_chain: str, rpc_url: str, pool_config):
        """Debounced trigger for the profit curve."""
        logger.debug("swap_event_detected", chain=source_chain)
        
        now = time.time()
        
        # If there's already a pending calculation, we just let it ride 
        # (it will catch this newest state change when it executes)
        if self._pending_calculation and not self._pending_calculation.done():
            return
            
        # Schedule a new calculation after the debounce delay
        self._pending_calculation = asyncio.create_task(self._delayed_calculation(rpc_url, pool_config))

    async def _delayed_calculation(self, rpc_url: str, pool_config):
        """Waits for the debounce window, fetches newest trigger state, calculates, and broadcasts."""
        await asyncio.sleep(self._debounce_delay)
        
        try:
            logger.info("executing_event_driven_curve_calc")
            from engine.core.arbitrage.simulator import generate_v3_profit_curve, update_single_pool_state
            
            # Fetch the newest state of the chain that triggered the event.
            # _fetch_fee_with_retry already attempts 3× with backoff internally.
            # If it still fails, generate_v3_profit_curve is the gate that blocks
            # execution and fires a background seed retry — no inline retry needed.
            http_url = rpc_url.replace("wss://", "https://")
            if not await update_single_pool_state(pool_config, rpc_url_override=http_url):
                logger.warning("pool_state_fetch_incomplete", pool=pool_config.pool_address)

            # Fire the instant callback so the dashboard receives the newly cached spot price
            if self.on_update:
                await self.on_update()
            
            # Now calculate the global curve with the globally cached states
            curve_data = await generate_v3_profit_curve()
            
            if curve_data:
                # Save price snapshots to database so charts don't break
                from engine.api.schemas import PriceQuote
                from engine.db.database import get_db
                import time

                db = await get_db()
                now_ms = int(time.time() * 1000)

                if "pancakeswap" in curve_data.get("prices", {}):
                    await db.insert_price_snapshot(PriceQuote(
                        source="pancakeswap_pool",
                        timestamp=now_ms,
                        bid=curve_data["prices"]["pancakeswap"],
                        ask=curve_data["prices"]["pancakeswap"],
                        mid=curve_data["prices"]["pancakeswap"],
                    ))
                if "aerodrome" in curve_data.get("prices", {}):
                    await db.insert_price_snapshot(PriceQuote(
                        source="aerodrome_pool",
                        timestamp=now_ms,
                        bid=curve_data["prices"]["aerodrome"],
                        ask=curve_data["prices"]["aerodrome"],
                        mid=curve_data["prices"]["aerodrome"],
                    ))
                if "assetchain" in curve_data.get("prices", {}):
                    await db.insert_price_snapshot(PriceQuote(
                        source="assetchain_pool",
                        timestamp=now_ms,
                        bid=curve_data["prices"]["assetchain"],
                        ask=curve_data["prices"]["assetchain"],
                        mid=curve_data["prices"]["assetchain"],
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
        
        # Expire old ones
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
                pancake_price=curve_data.get("prices", {}).get("pancakeswap"),
                aerodrome_price=curve_data.get("prices", {}).get("aerodrome"),
                slippage_tolerance_bps=optimal.get("slippage_tolerance_bps"),
                pancake_fee_bps=optimal.get("pancake_fee_bps"),
                aerodrome_fee_bps=optimal.get("aerodrome_fee_bps"),
                estimated_gas_usd=optimal.get("estimated_gas_usd")
            )
            await db.insert_dex_arbitrage_opportunity(opportunity)

        broadcast_data = optimal.copy()
        broadcast_data["id"] = opp_id

        self.broadcast({
            "type": "dex_arb_opportunity",
            "data": broadcast_data
        })
