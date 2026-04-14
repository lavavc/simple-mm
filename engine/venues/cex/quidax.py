"""Quidax CEX adapter for order ladder management."""

import asyncio
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Callable, Optional, cast

import httpx
import structlog

from engine.types import CexParams, OrderBookDepth, OrderBookLevel, Position, PriceQuote, VenueOrderSummary
from engine.db.backend import AlertStoreProtocol, SystemStateStoreProtocol
from engine.venues.base import VenueAdapter
from engine.venues.cex.ladder_planner import (
    LadderOrderTarget,
    build_ladder_order_targets,
    estimate_existing_anchor,
    get_requote_reason,
)
from engine.venues.cex.order_values import decimal_from_order_value
from engine.venues.cex.quidax_client import QuidaxApiClient
from engine.venues.cex.quidax_order_state import QuidaxTrackedOrderState
from engine.venues.cex.quidax_orders import (
    cancel_response_is_pending,
    extract_order_rows,
    is_order_open,
    is_order_terminal,
    normalize_order_summary,
    order_market_matches,
)

logger = structlog.get_logger()

_LADDER_PLACE_CONCURRENCY = 4
_ORDER_CANCEL_CONCURRENCY = 4
_PENDING_CANCEL_POLL_ATTEMPTS = 3
_PENDING_CANCEL_POLL_INTERVAL_SECONDS = 0.5


class QuidaxAdapter(VenueAdapter):
    """
    Quidax CEX adapter.

    Two independent roles:
    - Liquidity provision: sync_order_ladder() places limit orders across a price
      range to keep the CNGN/USDT book filled (market making).
    - Arb execution: place_market_order() hits the book immediately with a market
      order to capture a detected spread (taker, guaranteed fill).
    """

    def __init__(
        self,
        api_key: str,
        params: CexParams | None = None,
        market: str = "usdtcngn",
        base_url: str | None = None,
        order_api_base_url: str | None = None,
        order_user_id: str = "me",
        name: str = "quidax",
        funding_role: str = "quidax-trade-fund",
        alert_store: AlertStoreProtocol | None = None,
        system_state_store: SystemStateStoreProtocol | None = None,
        broadcast: Callable[[dict[str, Any]], Any] | None = None,
    ):
        """
        Initialize Quidax adapter.

        Args:
            api_key: Quidax secret key (used as Bearer token)
            params: Order ladder parameters
            market: Trading pair (lowercase, no underscore, e.g., "cngnusdt")
            base_url: Override public/openapi base URL (useful for testing)
            order_api_base_url: Override private order API base URL (useful for testing)
            order_user_id: Quidax user identifier for private order endpoints (`me` or sub-account id)
            name: Adapter name (used in logs and venue registry)
            funding_role: Account role for auto-funding ("quidax-trade-fund" | "quidax-lp")
        """
        self.name = name
        self.api_key = api_key
        self.params = params or CexParams()
        self.market = market
        self.base_url = (base_url or "https://openapi.quidax.io/exchange-open-api/api/v1/").rstrip("/")
        self.order_api_base_url = (order_api_base_url or "https://app.quidax.io/api/v1").rstrip("/")
        self.order_user_id = order_user_id or "me"
        self._funding_role = funding_role
        self._api = QuidaxApiClient(
            market=self.market,
            base_url=self.base_url,
            order_api_base_url=self.order_api_base_url,
            order_user_id=self.order_user_id,
            client_getter=lambda: self._get_client(),
        )
        self._order_state = QuidaxTrackedOrderState(
            venue_name=self.name,
            market=self.market,
            system_state_store=system_state_store,
        )
        self._client: Optional[httpx.AsyncClient] = None
        self._last_balances: dict[str, Decimal] = {}
        self._last_ladder_requote_at: float = 0
        self.enabled = True
        self.paused = False
        self._broadcast = broadcast
        if alert_store is None:
            raise ValueError("QuidaxAdapter requires an alert store")
        self.alert_store = alert_store

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with auth headers."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _resolve_balanced_order_sizes(self, reference_price: Decimal) -> tuple[Decimal, Decimal]:
        """Balance both ladder sides to the same notional when both are enabled.

        Returns:
            (effective_order_size_cngn, effective_order_size_usdt)
        """
        order_size_cngn = self.params.order_size_cngn
        order_size_usdt = self.params.order_size_usdt
        if reference_price <= 0 or order_size_cngn <= 0 or order_size_usdt <= 0:
            return order_size_cngn, order_size_usdt

        usdt_side_cngn_equiv = order_size_usdt * reference_price
        target_notional_cngn = min(order_size_cngn, usdt_side_cngn_equiv)
        effective_order_size_cngn = target_notional_cngn
        effective_order_size_usdt = target_notional_cngn / reference_price

        if (
            effective_order_size_cngn != order_size_cngn
            or effective_order_size_usdt != order_size_usdt
        ):
            logger.info(
                "quidax_ladder_sizes_balanced",
                reference_price_ngn=float(reference_price),
                configured_order_size_cngn=float(order_size_cngn),
                configured_order_size_usdt=float(order_size_usdt),
                effective_order_size_cngn=float(effective_order_size_cngn),
                effective_order_size_usdt=float(effective_order_size_usdt),
            )

        return effective_order_size_cngn, effective_order_size_usdt

    async def _emit_anchor_requote_alert(
        self,
        *,
        previous_anchor: Decimal | None,
        reference_price: Decimal,
        replaced_orders: int,
        orders_placed: int,
    ) -> None:
        previous_anchor_text = f"{previous_anchor:.2f}" if previous_anchor is not None else "unknown"
        message = (
            f"Quidax {self.name} anchor moved from {previous_anchor_text} to {reference_price:.2f} NGN; "
            f"replaced {replaced_orders} resting orders with {orders_placed} fresh ladder orders."
        )
        dedupe_key = (
            f"quidax_anchor_requote:{self.name}:{previous_anchor_text}:{reference_price:.2f}:{replaced_orders}"
        )
        alert_id = await self.alert_store.insert_alert(
            severity="warning",
            category="cex",
            message=message,
            dedup=True,
            dedupe_key=dedupe_key,
        )
        if alert_id and self._broadcast:
            self._broadcast(
                {
                    "type": "alert",
                    "severity": "warning",
                    "message": message,
                    "dedupe_key": dedupe_key,
                    "cooldown_s": 30,
                }
            )

    def _format_limit_order(self, side: str, price: Decimal, amount: Decimal) -> tuple[Decimal, Decimal]:
        """Apply Quidax market precision rules before submitting a limit order."""
        if self.market == "usdtcngn":
            price_precision = Decimal("0.01")
            volume_precision = Decimal("0.01")
            rounded_price = price.quantize(
                price_precision,
                rounding=ROUND_DOWN if side == "buy" else ROUND_UP,
            )
            rounded_amount = amount.quantize(volume_precision, rounding=ROUND_DOWN)
            return rounded_price, rounded_amount
        return price, amount

    async def _get_tracked_open_order_rows(self, live_order_ids: set[str] | None = None) -> list[dict[str, Any]]:
        return await self._order_state.get_open_order_rows(
            self._api.fetch_order_by_id,
            live_order_ids=live_order_ids,
        )

    async def _track_open_order(
        self,
        order_id: str,
        *,
        side: str,
        price: Decimal,
        volume: Decimal,
        created_at: Any = None,
    ) -> None:
        await self._order_state.track_open_order(
            order_id,
            side=side,
            price=price,
            volume=volume,
            created_at=created_at,
        )

    async def _remove_tracked_open_order(self, order_id: str) -> None:
        await self._order_state.remove_tracked_open_order(order_id)

    async def _update_tracked_open_order(self, order_id: str, **fields: Any) -> bool:
        return await self._order_state.update_tracked_open_order(order_id, **fields)

    async def _reconcile_tracked_open_orders_from_rows(self, rows: list[dict[str, Any]]) -> set[str]:
        return await self._order_state.reconcile_from_rows(rows)

    async def get_position(self) -> Position:
        """Fetch live cNGN and USDT balances from the Quidax wallet API."""
        balances: dict[str, Decimal] = {}
        for currency in ["cngn", "usdt"]:
            try:
                balances[currency] = await self._api.get_wallet_balance(currency)
            except Exception as e:
                logger.warning("quidax_balance_fetch_failed", currency=currency, error=str(e))
                balances[currency] = self._last_balances.get(currency, Decimal("0"))
        self._last_balances = balances
        return Position(
            venue=self.name,
            pair="CNGN/USDT",
            timestamp=int(time.time() * 1000),
            balances=balances,
        )

    async def get_order_book_depth(self, limit: int = 50) -> Optional[OrderBookDepth]:
        """Fetch Level 2 Order Book depth (Bids and Asks with volume)."""
        try:
            data = await self._api.get_order_book_payload(limit)

            if data.get("status") != "success":
                return None

            depth_data = data.get("data", {})
            
            bids = [
                OrderBookLevel(price=Decimal(str(p)), amount=Decimal(str(v)))
                for p, v in depth_data.get("bids", [])
                if Decimal(str(p)) > 0
            ]
            asks = [
                OrderBookLevel(price=Decimal(str(p)), amount=Decimal(str(v)))
                for p, v in depth_data.get("asks", [])
                if Decimal(str(p)) > 0
            ]

            return OrderBookDepth(
                venue=self.name,
                pair="CNGN/USDT",
                timestamp=depth_data.get("timestamp", int(time.time() * 1000)),
                bids=bids,
                asks=asks,
            )
        except Exception as e:
            logger.error("quidax_depth_fetch_failed", error=str(e))
            return None

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Fetch current cNGN/USDT price from the Quidax public market summary.

        Uses a plain client (no auth header) since this is a public endpoint.
        """
        try:
            data = await self._api.get_market_summary_payload()

            if data.get("status") != "success":
                return None

            pair_data = data.get("data", {}).get("CNGN_USDT")
            if not pair_data:
                return None

            bid = Decimal(str(pair_data.get("highest_bid", "0")))
            ask = Decimal(str(pair_data.get("lowest_ask", "0")))
            last = Decimal(str(pair_data.get("last_price", "0")))

            if bid == 0 and ask == 0:
                if last == 0:
                    return None
                bid = last
                ask = last

            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last

            return PriceQuote(
                source="quidax",
                timestamp=int(time.time() * 1000),
                bid=bid,
                ask=ask,
                mid=mid,
            )
        except Exception as e:
            logger.error("quidax_price_fetch_failed", error=str(e))
            return None

    async def get_open_orders(self) -> list[dict[str, Any]]:
        """Get all open orders."""
        attempts: tuple[dict[str, Any], ...] = (
            {"market": self.market, "state": "wait"},
            {"market": self.market},
            {},
        )
        fallback_rows: list[dict[str, Any]] = []

        for params in attempts:
            payload = await self._api.fetch_orders_payload(params)
            rows = extract_order_rows(payload)
            await self._reconcile_tracked_open_orders_from_rows(rows)
            if rows:
                fallback_rows = rows
            open_rows = [
                order
                for order in rows
                if order_market_matches(order, self.market) and is_order_open(order)
            ]
            if open_rows:
                tracked_rows = await self._get_tracked_open_order_rows(
                    live_order_ids={str(order.get("id", "")) for order in open_rows if order.get("id")}
                )
                tracked_by_id = {str(order.get("id", "")): order for order in tracked_rows}
                for order in open_rows:
                    tracked_by_id.pop(str(order.get("id", "")), None)
                open_rows.extend(tracked_by_id.values())
                if params != attempts[0]:
                    logger.info("quidax_open_orders_fallback_used", params=params, count=len(open_rows))
                return open_rows

        tracked_rows = await self._get_tracked_open_order_rows()
        if tracked_rows:
            logger.warning(
                "quidax_open_orders_api_empty_using_tracked_fallback",
                count=len(tracked_rows),
            )
            return tracked_rows

        if fallback_rows:
            logger.warning("quidax_open_orders_found_no_open_matches", rows=len(fallback_rows))
        return []

    async def get_orders_debug(self) -> dict[str, Any]:
        attempts: tuple[dict[str, Any], ...] = (
            {"market": self.market, "state": "wait"},
            {"market": self.market},
            {},
        )
        results: list[dict[str, Any]] = []

        for params in attempts:
            endpoint_results: list[dict[str, Any]] = []
            payload: dict[str, Any] | None = None
            for endpoint in self._api.order_collection_endpoints():
                try:
                    current_payload = await self._api.fetch_orders_payload_direct(endpoint, params)
                    endpoint_results.append(
                        {"endpoint": endpoint, "ok": True, "payload": current_payload}
                    )
                    if payload is None:
                        payload = current_payload
                except Exception as exc:
                    endpoint_results.append(
                        {"endpoint": endpoint, "ok": False, "error": str(exc)}
                    )

            payload = payload or {"status": "error", "message": "No payload returned", "data": []}
            rows = extract_order_rows(payload)
            open_rows = [
                order
                for order in rows
                if order_market_matches(order, self.market) and is_order_open(order)
            ]
            results.append(
                {
                    "params": params,
                    "payload": payload,
                    "endpoint_results": endpoint_results,
                    "row_count": len(rows),
                    "rows": rows,
                    "open_match_count": len(open_rows),
                    "open_matches": open_rows,
                }
            )

        return {"market": self.market, "attempts": results}

    async def get_open_order_summaries(self) -> list[VenueOrderSummary]:
        orders = await self.get_open_orders()
        summaries: list[VenueOrderSummary] = []
        for order in orders:
            if (summary := normalize_order_summary(order, market=self.market)) is not None:
                summaries.append(summary)
        summaries.sort(key=lambda order: order.created_at or 0, reverse=True)
        return summaries

    async def cancel_all_orders(self) -> int:
        """
        Cancel all open orders.

        Returns:
            Number of orders cancelled
        """
        orders = await self.get_open_orders()
        if not orders:
            logger.info("cancelled_orders", count=0, total=0, pending=0, remaining=0)
            return 0

        client = await self._get_client()
        pending_missing_marker_key = "_tracked_missing_lookup_seen_once"
        cancelable_orders: list[dict[str, Any]] = []

        for order in orders:
            order_id = str(order.get("id", ""))
            if not order_id:
                continue
            if not order.get(pending_missing_marker_key):
                cancelable_orders.append(order)
                continue

            resolution, confirmed_order = await self._api.fetch_order_by_id(order_id)
            if resolution == "missing":
                await self._remove_tracked_open_order(order_id)
                continue
            if resolution == "found" and isinstance(confirmed_order, dict):
                if is_order_terminal(confirmed_order):
                    await self._remove_tracked_open_order(order_id)
                    continue
                cancelable_orders.append(confirmed_order)
                continue
            cancelable_orders.append(order)

        if not cancelable_orders:
            logger.info("cancelled_orders", count=0, total=len(orders), pending=0, remaining=0)
            return 0

        terminal_cancelled_ids: set[str] = set()
        pending_cancel_ids: set[str] = set()
        failed_cancel_ids: set[str] = set()

        async def _track_settling_cancel(order: dict[str, Any]) -> None:
            order_id = str(order.get("id", ""))
            if not order_id:
                return

            if not await self._update_tracked_open_order(order_id, status="pending_cancel"):
                summary = normalize_order_summary(order, market=self.market)
                if summary is not None:
                    tracked_volume = (
                        summary.remaining_volume if summary.remaining_volume > 0 else summary.volume
                    )
                    await self._track_open_order(
                        order_id,
                        side=summary.side,
                        price=summary.price,
                        volume=tracked_volume,
                        created_at=summary.created_at,
                    )
                else:
                    await self._track_open_order(
                        order_id,
                        side=str(order.get("side") or "").lower() or "unknown",
                        price=decimal_from_order_value(order.get("price")),
                        volume=decimal_from_order_value(order.get("remaining_volume"))
                        or decimal_from_order_value(order.get("volume"))
                        or decimal_from_order_value(order.get("origin_volume")),
                        created_at=order.get("created_at") or int(time.time() * 1000),
                    )
                await self._update_tracked_open_order(order_id, status="pending_cancel")

        async def _cancel_order(order: dict[str, Any]) -> tuple[str | None, str]:
            order_id = str(order.get("id", ""))
            if not order_id:
                logger.warning("cancel_order_missing_id", order=order)
                return None, "failed"
            try:
                for endpoint in self._api.order_item_endpoints(order_id):
                    cancel_endpoint = f"{endpoint}/cancel"
                    try:
                        response = await client.post(cancel_endpoint)
                        response.raise_for_status()
                        result = cast(dict[str, Any], response.json())
                        if result.get("status") == "success":
                            if cancel_response_is_pending(result):
                                await _track_settling_cancel(order)
                                logger.info(
                                    "cancel_order_pending",
                                    endpoint=cancel_endpoint,
                                    order_id=order_id,
                                )
                                return order_id, "pending"
                            else:
                                await _track_settling_cancel(order)
                                return order_id, "terminal"
                        logger.warning(
                            "cancel_order_rejected",
                            endpoint=cancel_endpoint,
                            order_id=order_id,
                            detail=result.get("message") or result.get("errors") or "unknown",
                        )
                    except Exception as endpoint_exc:
                        logger.warning(
                            "cancel_order_endpoint_failed",
                            endpoint=cancel_endpoint,
                            order_id=order_id,
                            error=str(endpoint_exc),
                        )
                logger.warning("cancel_order_failed_all_endpoints", order_id=order_id)
            except Exception as e:
                logger.warning("cancel_order_failed", order_id=order_id, error=str(e))
            return order_id, "failed"

        cancel_semaphore = asyncio.Semaphore(min(_ORDER_CANCEL_CONCURRENCY, len(cancelable_orders)))

        async def _cancel_with_limit(order: dict[str, Any]) -> tuple[str | None, str]:
            async with cancel_semaphore:
                return await _cancel_order(order)

        cancel_results = await asyncio.gather(*[_cancel_with_limit(order) for order in cancelable_orders])
        for order_id, result in cancel_results:
            if not order_id:
                continue
            if result == "terminal":
                terminal_cancelled_ids.add(order_id)
            elif result == "pending":
                pending_cancel_ids.add(order_id)
            else:
                failed_cancel_ids.add(order_id)

        settling_cancel_ids = terminal_cancelled_ids | pending_cancel_ids
        remaining_settling_ids = set(settling_cancel_ids)
        if remaining_settling_ids:
            poll_semaphore = asyncio.Semaphore(min(_ORDER_CANCEL_CONCURRENCY, len(remaining_settling_ids)))

            async def _poll_cancel_settlement(order_id: str) -> tuple[str, bool]:
                async with poll_semaphore:
                    resolution, order = await self._api.fetch_order_by_id(order_id)
                    if resolution == "missing":
                        return order_id, True
                    if resolution == "found" and isinstance(order, dict):
                        if is_order_terminal(order):
                            return order_id, True
                        remote_status = str(order.get("state") or order.get("status") or "").lower()
                        if remote_status:
                            await self._update_tracked_open_order(order_id, status=remote_status)
                    return order_id, False

            for attempt in range(_PENDING_CANCEL_POLL_ATTEMPTS):
                if not remaining_settling_ids:
                    break
                if attempt > 0:
                    await asyncio.sleep(_PENDING_CANCEL_POLL_INTERVAL_SECONDS)
                poll_results = await asyncio.gather(
                    *[_poll_cancel_settlement(order_id) for order_id in remaining_settling_ids]
                )
                completed_ids = {
                    order_id for order_id, settled in poll_results if settled
                }
                remaining_settling_ids -= completed_ids

        completed_settling_ids = settling_cancel_ids - remaining_settling_ids
        for order_id in completed_settling_ids:
            await self._remove_tracked_open_order(order_id)

        cancelled = len(completed_settling_ids)
        logger.info(
            "cancelled_orders",
            count=cancelled,
            total=len(orders),
            pending=len(remaining_settling_ids),
            remaining=len(remaining_settling_ids),
        )
        return cancelled

    async def place_order(
        self,
        side: str,
        price: Decimal,
        amount: Decimal,
    ) -> dict[str, Any]:
        """
        Place a limit order.

        Args:
            side: "buy" or "sell"
            price: Limit price in cNGN per USDT for the configured Quidax market
            amount: Order volume in the market's base asset (USDT for usdtcngn)

        Returns:
            Order result from API
        """
        client = await self._get_client()
        formatted_price, formatted_amount = self._format_limit_order(side, price, amount)
        payload = {
            "market": self.market,
            "side": side,
            "ord_type": "limit",
            "price": str(formatted_price),
            "volume": str(formatted_amount),
        }
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        for endpoint in self._api.order_collection_endpoints():
            try:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                result = cast(dict[str, Any], response.json())
                if result.get("status") == "success" and result.get("data"):
                    break
                detail = result.get("message") or result.get("errors") or "Unknown Quidax error"
                raise ValueError(str(detail))
            except Exception as exc:
                last_error = exc
                logger.warning("place_order_endpoint_failed", endpoint=endpoint, error=str(exc))
                result = None
        if result is None:
            raise last_error or ValueError("Unknown Quidax error")

        logger.debug(
            "order_placed",
            side=side,
            price=float(formatted_price),
            amount=float(formatted_amount),
            success=bool(result.get("data")),
        )

        order_data = result.get("data")
        if isinstance(order_data, dict):
            order_id = str(order_data.get("id", "")).strip()
            if order_id:
                tracked_price = decimal_from_order_value(order_data.get("price"))
                tracked_volume = decimal_from_order_value(order_data.get("volume"))
                await self._track_open_order(
                    order_id,
                    side=str(order_data.get("side") or side).lower(),
                    price=tracked_price if tracked_price > 0 else formatted_price,
                    volume=tracked_volume if tracked_volume > 0 else formatted_amount,
                    created_at=order_data.get("created_at") or int(time.time() * 1000),
                )

        return result

    async def place_market_order(
        self,
        side: str,
        amount: Decimal,
    ) -> tuple[bool, Decimal, Decimal, str | None]:
        """
        Place a market order with up to 5 retries on network/5xx errors.
        Args:
            side: "buy" or "sell"
            amount: Order amount in CNGN
        Returns:
            (success, executed_cngn, avg_price_usdt, error)
        """
        client = await self._get_client()
        payload = {"market": self.market, "side": side, "ord_type": "market", "volume": str(amount)}
        last_error: str | None = None

        for attempt, delay in enumerate([0, 2, 4, 8, 16, 32]):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = None
                resp_json: dict[str, Any] = {}
                for endpoint in self._api.order_collection_endpoints():
                    try:
                        response = await client.post(endpoint, json=payload)
                        try:
                            resp_json = response.json()
                        except Exception:
                            resp_json = {}

                        if 400 <= response.status_code < 500:
                            detail = resp_json.get("message", response.text or "")
                            error_msg = f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"
                            last_error = error_msg
                            logger.warning(
                                "market_order_endpoint_rejected",
                                endpoint=endpoint,
                                side=side,
                                attempt=attempt,
                                error=error_msg,
                            )
                            continue

                        response.raise_for_status()
                        break
                    except Exception as endpoint_exc:
                        last_error = str(endpoint_exc)
                        logger.warning(
                            "market_order_endpoint_failed",
                            endpoint=endpoint,
                            side=side,
                            attempt=attempt,
                            error=last_error,
                        )
                        response = None
                        resp_json = {}
                        continue

                if response is None:
                    raise RuntimeError(last_error or "All Quidax market order endpoints failed")
                
                if resp_json.get("status") != "success":
                    error = str(resp_json.get("message", "Unknown Quidax Error"))
                    return False, Decimal("0"), Decimal("0"), error
                    
                data = resp_json.get("data", {})
                
                # Safely extract amounts handling both string and dict formats from Quidax
                vol_data = data.get("executed_volume", "0")
                executed_cngn = Decimal(str(vol_data.get("amount", "0") if isinstance(vol_data, dict) else vol_data))
                    
                price_data = data.get("avg_price", "0")
                avg_price = Decimal(str(price_data.get("amount", "0") if isinstance(price_data, dict) else price_data))
                    
                logger.debug("market_order_placed", side=side, amount=float(amount), attempt=attempt)
                return True, executed_cngn, avg_price, None
                
            except Exception as e:
                last_error = str(e)
                logger.warning("market_order_attempt_failed", side=side, attempt=attempt, error=last_error)

        await self.alert_store.insert_alert(
            severity="critical",
            category="cex",
            message=f"Quidax {self.name} market order failed after 5 retries: {last_error}",
        )
        return False, Decimal("0"), Decimal("0"), last_error

    async def sync_order_ladder(self, reference_price: Decimal) -> None:
        """
        Sync order ladder to current reference price.

        Places buy and sell limit orders at fixed NGN offsets from the current rate.

        Args:
            reference_price: Current NGN/USDT rate (e.g. 1600 = 1 USDT buys 1600 NGN/cNGN)
        """
        if self.paused or not self.params.ladder_enabled:
            logger.info("quidax_ladder_skipped", paused=self.paused, enabled=self.params.ladder_enabled)
            return

        effective_order_size_cngn, effective_order_size_usdt = self._resolve_balanced_order_sizes(
            reference_price
        )
        desired_orders = build_ladder_order_targets(
            reference_price=reference_price,
            offsets=self.params.resolved_ladder_offsets_ngn,
            order_size_cngn=effective_order_size_cngn,
            order_size_usdt=effective_order_size_usdt,
            format_limit_order=self._format_limit_order,
        )
        existing_orders = await self.get_open_orders()
        requote_reason: str | None = None
        previous_anchor: Decimal | None = None
        if existing_orders:
            requote_reason = get_requote_reason(
                existing_orders=existing_orders,
                desired_orders=desired_orders,
                threshold_bps=Decimal(str(self.params.anchor_requote_threshold_bps)),
            )
        if existing_orders and requote_reason is None:
            logger.info(
                "quidax_ladder_requote_skipped",
                reference_price_ngn=float(reference_price),
                existing_orders=len(existing_orders),
                threshold_bps=self.params.anchor_requote_threshold_bps,
            )
            return

        if existing_orders:
            if requote_reason == "anchor_move":
                previous_anchor = estimate_existing_anchor(existing_orders)
            cooldown_seconds = max(0, int(self.params.anchor_requote_cooldown_seconds))
            if cooldown_seconds > 0 and self._last_ladder_requote_at > 0:
                elapsed = time.time() - self._last_ladder_requote_at
                if elapsed < cooldown_seconds:
                    logger.info(
                        "quidax_ladder_requote_cooldown_skipped",
                        reference_price_ngn=float(reference_price),
                        cooldown_seconds=cooldown_seconds,
                        elapsed_seconds=elapsed,
                    )
                    return
            await self.cancel_all_orders()
            remaining_orders = await self.get_open_orders()
            if remaining_orders:
                remaining_ids = [str(order.get("id", "unknown")) for order in remaining_orders[:10]]
                raise RuntimeError(
                    "Refusing to place a new Quidax ladder while prior open orders remain: "
                    f"{len(remaining_orders)} still open ({', '.join(remaining_ids)})"
                )

        orders_attempted = 0
        orders_placed = 0
        order_errors: list[str] = []
        orders_attempted = len(desired_orders)

        place_semaphore = asyncio.Semaphore(min(_LADDER_PLACE_CONCURRENCY, len(desired_orders)))

        async def _place_target(target: LadderOrderTarget) -> tuple[LadderOrderTarget, Exception | None]:
            async with place_semaphore:
                try:
                    await self.place_order(target.side, target.price, target.volume)
                    return target, None
                except Exception as exc:
                    return target, exc

        placement_results = await asyncio.gather(*[_place_target(target) for target in desired_orders])
        for target, error in placement_results:
            if error is None:
                orders_placed += 1
                continue
            order_errors.append(f"{target.side}@{target.price}:{error}")
            logger.warning("place_ladder_order_failed", side=target.side, price=float(target.price), error=str(error))

        if orders_attempted > 0 and orders_placed == 0:
            raise RuntimeError(
                "Quidax ladder sync attempted "
                f"{orders_attempted} orders but none were accepted: {'; '.join(order_errors)}"
            )

        logger.info(
            "order_ladder_synced",
            reference_price_ngn=float(reference_price),
            offsets=self.params.resolved_ladder_offsets_ngn,
            orders_attempted=orders_attempted,
            orders_placed=orders_placed,
        )
        if orders_placed > 0:
            self._last_ladder_requote_at = time.time()
            if existing_orders and requote_reason == "anchor_move":
                await self._emit_anchor_requote_alert(
                    previous_anchor=previous_anchor,
                    reference_price=reference_price,
                    replaced_orders=len(existing_orders),
                    orders_placed=orders_placed,
                )


    async def handle_webhook(self, event: dict[str, Any]) -> None:
        """Handle Quidax webhook events."""
        event_type = event.get("event")

        if event_type in {"order.filled", "order.cancel", "order.cancelled", "order.canceled"}:
            order = event.get("data", {})
            order_id = str(order.get("id", "")).strip()
            if order_id:
                await self._remove_tracked_open_order(order_id)
            logger.info(
                "quidax_order_webhook",
                event_type=event_type,
                order_id=order.get("id"),
                side=order.get("side"),
                price=order.get("price"),
                volume=order.get("executed_volume"),
            )
