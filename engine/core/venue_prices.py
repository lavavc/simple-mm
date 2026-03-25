"""Per-venue price fetching for arbitrage and LP management.

Each venue is a separate price source. All venues are fetched in parallel
to get a complete picture of the market at a point in time.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

import httpx
import structlog

from engine.api.schemas import PriceQuote

if TYPE_CHECKING:
    from engine.venues.base import VenueAdapter
    from engine.venues.dex.pool_reader_v3 import PoolPriceReader

logger = structlog.get_logger()


# =============================================================================
# Base class
# =============================================================================


class VenuePriceSource(ABC):
    """Base class for venue-specific price sources."""

    name: str
    pair: str  # e.g. "USDT/NGN", "cNGN/USDC"
    volume_24h_usd: Optional[Decimal] = None  # Set during fetch_price(); used for VWAP weighting

    @abstractmethod
    async def fetch_price(self) -> Optional[PriceQuote]:
        """Fetch current price from this venue."""
        ...

    async def close(self):
        """Clean up resources."""
        pass


# =============================================================================
# Bybit P2P (with fraud filtering)
# =============================================================================


@dataclass
class P2PAd:
    """Bybit P2P advertisement."""

    price: Decimal
    quantity: Decimal
    completed_orders: int
    completion_rate: float
    avg_release_time: int
    is_online: bool


@dataclass
class BybitConfig:
    """Configuration for Bybit P2P fraud filtering."""

    min_completed_orders: int = 100
    min_completion_rate: float = 0.90
    max_avg_release_time: int = 900  # 15 minutes
    min_amount_ngn: Decimal = Decimal("5000000")   # ₦5M minimum ad size
    max_amount_ngn: Decimal = Decimal("20000000")  # ₦20M maximum ad size
    max_deviation_from_median: float = 0.02  # 2%
    cache_seconds: int = 60
    depth_utilization: float = 0.05   # Fraction of total listed depth treated as effective trading activity
    depth_cache_seconds: int = 300    # Depth is stable; refresh every 5 minutes


class BybitP2PPriceSource(VenuePriceSource):
    """Fetches USDT/NGN price from Bybit P2P with fraud filtering.

    Strategy:
    1. Filter by trader reputation (completion rate, order count, release time)
    2. Filter by ad size (₦5M–₦20M) to exclude retail noise and whale outliers
    3. Reject outliers beyond 2% of the median
    4. Return the mode (most frequently occurring rate)
    """

    name = "bybit"
    pair = "USDT/NGN"

    def __init__(self, config: BybitConfig | None = None):
        self.config = config or BybitConfig()
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Optional[tuple[PriceQuote, float]] = None
        self._depth_cache_time: float = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch_price(self) -> Optional[PriceQuote]:
        if self._cache:
            quote, fetched_at = self._cache
            if time.time() - fetched_at < self.config.cache_seconds:
                return quote

        try:
            buy_ads, sell_ads = await asyncio.gather(
                self._fetch_p2p_ads("buy"),
                self._fetch_p2p_ads("sell"),
            )

            if not buy_ads or not sell_ads:
                logger.warning("bybit_insufficient_ads")
                return None

            ask = self._filter_and_aggregate(buy_ads)
            bid = self._filter_and_aggregate(sell_ads)

            if ask is None or bid is None:
                return None

            mid = (bid + ask) / 2
            quote = PriceQuote(
                source="bybit_p2p",
                timestamp=int(time.time() * 1000),
                bid=bid,
                ask=ask,
                mid=mid,
            )
            self._cache = (quote, time.time())

            # Refresh market depth every 5 minutes; used as VWAP weight
            if time.time() - self._depth_cache_time > self.config.depth_cache_seconds:
                depth = await self._fetch_total_depth_usdt()
                if depth is not None:
                    self.volume_24h_usd = depth
                self._depth_cache_time = time.time()

            logger.info(
                "bybit_price_fetched",
                bid=float(bid),
                ask=float(ask),
                spread_bps=int((ask - bid) / mid * 10000),
            )
            return quote

        except Exception as e:
            logger.error("bybit_fetch_failed", error=str(e))
            return None

    async def _fetch_total_depth_usdt(self) -> Optional[Decimal]:
        """Estimate total market depth from BUY-side ads.

        Fetches page 1 of 200 BUY ads, sums lastQuantity (remaining available USDT),
        extrapolates to total ad count, then applies a utilization factor since not all
        listed depth actually trades. Returns a depth proxy in USDT.
        """
        client = await self._get_client()
        payload = {
            "tokenId": "USDT",
            "currencyId": "NGN",
            "side": "1",  # BUY side
            "size": "200",
            "page": "1",
            "payment": [],
            "amount": "",
        }
        try:
            response = await client.post(
                "https://api2.bybit.com/fiat/otc/item/online", json=payload
            )
            response.raise_for_status()
            result = response.json().get("result", {})
            total_count = int(result.get("count", 0))
            items = result.get("items", [])
            if not items or total_count == 0:
                return None

            page_depth = sum(
                Decimal(str(item.get("lastQuantity", "0"))) for item in items
            )
            # Extrapolate page-1 sample to full market, then apply utilization factor
            total_depth = page_depth * Decimal(str(total_count)) / Decimal(str(len(items)))
            depth_proxy = total_depth * Decimal(str(self.config.depth_utilization))
            logger.debug(
                "bybit_depth_fetched",
                total_ads=total_count,
                page1_depth_usdt=float(page_depth),
                depth_proxy_usdt=float(depth_proxy),
            )
            return depth_proxy
        except Exception as e:
            logger.warning("bybit_depth_fetch_failed", error=str(e))
            return None

    async def _fetch_p2p_ads(self, side: str) -> list[P2PAd]:
        client = await self._get_client()
        url = "https://api2.bybit.com/fiat/otc/item/online"

        payload = {
            "tokenId": "USDT",
            "currencyId": "NGN",
            "side": "1" if side == "buy" else "0",
            "size": "50",
            "page": "1",
            "payment": [],
            "amount": "",
        }

        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            ads = []
            for item in data.get("result", {}).get("items", []):
                ads.append(
                    P2PAd(
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item.get("quantity", "0"))),
                        completed_orders=int(item.get("recentOrderNum", 0)),
                        completion_rate=float(item.get("recentExecuteRate", 0)),
                        avg_release_time=int(item.get("avgReleaseTime", 0)),
                        is_online=item.get("isOnline", False),
                    )
                )
            return ads

        except Exception as e:
            logger.error("bybit_p2p_fetch_failed", side=side, error=str(e))
            return []

    def _filter_and_aggregate(self, ads: list[P2PAd]) -> Optional[Decimal]:
        """Apply fraud filtering and return the modal price."""
        reputable = [
            ad
            for ad in ads
            if ad.completed_orders >= self.config.min_completed_orders
            and ad.completion_rate >= self.config.min_completion_rate
            and ad.avg_release_time <= self.config.max_avg_release_time
            and ad.is_online
        ]

        # Keep ads whose total NGN value falls within ₦5M–₦20M
        in_range = [
            ad for ad in reputable
            if self.config.min_amount_ngn <= ad.price * ad.quantity <= self.config.max_amount_ngn
        ]

        if len(in_range) < 3:
            logger.warning("bybit_insufficient_filtered_ads", count=len(in_range))
            return None

        prices = sorted(ad.price for ad in in_range)
        median = prices[len(prices) // 2]

        max_dev = Decimal(str(self.config.max_deviation_from_median))
        filtered = [ad for ad in in_range if abs(ad.price - median) / median <= max_dev]

        if len(filtered) < 2:
            filtered = in_range

        # Mode: round to nearest integer NGN to cluster equivalent prices
        counts = Counter(int(ad.price.to_integral_value()) for ad in filtered)
        mode_ngn = counts.most_common(1)[0][0]
        return Decimal(str(mode_ngn))


# =============================================================================
# Quidax (public API)
# =============================================================================


@dataclass
class QuidaxConfig:
    """Configuration for Quidax price source."""

    base_url: str = "https://openapi.quidax.io/exchange-open-api/api/v1"
    pair: str = "usdtcngn"  # Active Quidax market: USDT=base, cNGN=quote
    cache_seconds: int = 30


class QuidaxPriceSource(VenuePriceSource):
    """Fetches cNGN/USDT price from Quidax public v3 tickers API.

    Uses the `usdtcngn` pair which gives us the NGN per USDT rate.
    We invert this to get USDT per NGN (i.e., USD per cNGN).
    No authentication required.
    """

    name = "quidax"
    pair = "cNGN/USDT"  # Represents USD per cNGN

    def __init__(self, config: QuidaxConfig | None = None):
        self.config = config or QuidaxConfig()
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Optional[tuple[PriceQuote, float]] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"accept": "application/json"},
                timeout=30,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch_price(self) -> Optional[PriceQuote]:
        if self._cache:
            quote, fetched_at = self._cache
            if time.time() - fetched_at < self.config.cache_seconds:
                return quote

        try:
            client = await self._get_client()
            # v3 tickers endpoint — has real volume and live bid/ask
            response = await client.get(f"{self.config.base_url}/markets/tickers")
            response.raise_for_status()

            data = response.json()

            if data.get("status") != "success":
                logger.warning("quidax_api_error", message=data.get("message"))
                return None

            # usdtcngn: USDT=base, cNGN=quote
            # 'buy'  = best bid on USDT = cNGN per USDT (e.g. 1380.1)
            # 'sell' = best ask on USDT = cNGN per USDT (e.g. 1383.5)
            market_data = data.get("data", {}).get(self.config.pair)
            if not market_data:
                logger.warning("quidax_pair_not_found", pair=self.config.pair)
                return None

            ticker = market_data.get("ticker", {})

            buy_cngn_per_usdt  = Decimal(str(ticker.get("buy",  "0")))  # bid on USDT side
            sell_cngn_per_usdt = Decimal(str(ticker.get("sell", "0")))  # ask on USDT side
            last_cngn_per_usdt = Decimal(str(ticker.get("last", "0")))

            if buy_cngn_per_usdt <= 0 and sell_cngn_per_usdt <= 0:
                if last_cngn_per_usdt <= 0:
                    return None
                buy_cngn_per_usdt = last_cngn_per_usdt
                sell_cngn_per_usdt = last_cngn_per_usdt

            # Invert to USD per cNGN:
            # Selling cNGN → hitting USDT ask (sell_cngn_per_usdt) → cNGN bid = 1/sell
            # Buying  cNGN → hitting USDT bid (buy_cngn_per_usdt)  → cNGN ask = 1/buy
            bid = Decimal("1") / sell_cngn_per_usdt if sell_cngn_per_usdt > 0 else Decimal("0")
            ask = Decimal("1") / buy_cngn_per_usdt  if buy_cngn_per_usdt  > 0 else Decimal("0")
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (
                Decimal("1") / last_cngn_per_usdt if last_cngn_per_usdt > 0 else Decimal("0")
            )

            if mid <= 0:
                return None

            quote = PriceQuote(
                source="quidax",
                timestamp=int(time.time() * 1000),
                bid=bid,
                ask=ask,
                mid=mid,
            )
            self._cache = (quote, time.time())

            vol = ticker.get("vol")
            self.volume_24h_usd = Decimal(str(vol)) if vol else None

            logger.info(
                "quidax_price_fetched",
                pair=self.config.pair,
                bid=float(bid),
                ask=float(ask),
                mid=float(mid),
                spread_bps=int((ask - bid) / mid * 10000) if mid > 0 else 0,
            )
            return quote

        except Exception as e:
            logger.error("quidax_fetch_failed", error=str(e))
            return None


# =============================================================================
# Blockradar Price Source
# =============================================================================


class BlockradarPriceSource(VenuePriceSource):
    """Fetches the cNGN/USDC rate from the Blockradar public rates API.

    To add another Blockradar pair (e.g. cNGN/USDT, USDC/cNGN):
    - Create a second subclass with a different `name` and `pair`
    - Pass the matching `currency`/`assets` params to the adapter
    - Register it in `create_venue_aggregator`
    - Add the pair string to the right constant in price_aggregation.py
    """

    name = "blockradar"
    pair = "cNGN/USDC"

    def __init__(self, adapter: "VenueAdapter"):
        self._adapter = adapter

    async def fetch_price(self) -> Optional[PriceQuote]:
        try:
            return await self._adapter.get_current_price()
        except Exception as e:
            logger.error("blockradar_fetch_failed", error=str(e))
            return None

    async def close(self):
        if hasattr(self._adapter, "close"):
            await self._adapter.close()


# =============================================================================
# DEX Adapter Price Source (wraps a live VenueAdapter)
# =============================================================================


class DexAdapterPriceSource(VenuePriceSource):
    """Wraps the global simulator cache as a zero-latency price source.

    Instead of making redundant RPC calls, this source instantly pulls the 
    most recent mathematical `sqrtPriceX96` from the Arbitrage WebSocket Listener.
    """

    def __init__(
        self,
        venue_name: str,
        pair: str,
        pool_address: str,
        dexscreener_chain: str = "",
    ):
        self.name = venue_name
        self.pair = pair
        self.pool_address = pool_address
        self._dexscreener_chain = dexscreener_chain
        self._vol_cache_time: float = 0

    async def fetch_price(self) -> Optional[PriceQuote]:
        from engine.core.arbitrage.pool_state import get_cached_pool_state, Q96

        sqrt_p, _, _, _, timestamp, _ = get_cached_pool_state(self.pool_address)

        if sqrt_p is None:
            logger.warning("dex_price_cache_miss", venue=self.name)
            return None

        # token0=cNGN(6), token1=USDC(6): price = sqrtP^2, no inversion
        if self.name == "uni-base":
            price = ((sqrt_p / Q96) ** 2) * Decimal(10 ** (6 - 6))
            human_price = price
        else:
            # token0=USDT(18), token1=cNGN(6): invert to get USD per cNGN
            price = ((sqrt_p / Q96) ** 2) * Decimal(10 ** (18 - 6))
            human_price = Decimal(1) / price if price > 0 else Decimal(0)

        # Refresh 24h volume from DexScreener every 5 minutes
        if self._dexscreener_chain and time.time() - self._vol_cache_time > 300:
            try:
                url = f"https://api.dexscreener.com/latest/dex/pairs/{self._dexscreener_chain}/{self.pool_address}"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url)
                    pairs = resp.json().get("pairs") or []
                    if pairs:
                        vol = pairs[0].get("volume", {}).get("h24")
                        self.volume_24h_usd = Decimal(str(vol)) if vol is not None else None
                self._vol_cache_time = time.time()
            except Exception as e:
                logger.warning("dexscreener_volume_fetch_failed", venue=self.name, error=str(e))

        return PriceQuote(
            source="simulator_cache",
            timestamp=int((timestamp or time.time()) * 1000),
            bid=human_price,
            ask=human_price,
            mid=human_price,
        )


# =============================================================================
# Venue Price Aggregator
# =============================================================================


@dataclass
class VenuePrice:
    """Price from a specific venue with metadata."""

    venue: str
    pair: str
    quote: Optional[PriceQuote]
    error: Optional[str] = None
    fetched_at: float = field(default_factory=time.time)
    volume_24h_usd: Optional[Decimal] = None

    @property
    def is_valid(self) -> bool:
        return self.quote is not None and self.error is None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


class VenuePriceAggregator:
    """Fetches prices from all venues in parallel."""

    def __init__(self, sources: list[VenuePriceSource]):
        self.sources = {s.name: s for s in sources}
        self._prices: dict[str, VenuePrice] = {}
        self._last_fetch: float = 0

    async def fetch_all(self) -> dict[str, VenuePrice]:
        tasks = []
        venue_names = []

        for name, source in self.sources.items():
            tasks.append(self._fetch_one(source))
            venue_names.append(name)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        prices = {}
        for name, result in zip(venue_names, results):
            source = self.sources[name]
            if isinstance(result, Exception):
                prices[name] = VenuePrice(
                    venue=name,
                    pair=source.pair,
                    quote=None,
                    error=str(result),
                )
            else:
                prices[name] = result

        self._prices = prices
        self._last_fetch = time.time()

        valid = sum(1 for p in prices.values() if p.is_valid)
        logger.info(
            "venue_prices_fetched",
            total=len(prices),
            valid=valid,
            venues=[p.venue for p in prices.values() if p.is_valid],
        )

        return prices

    async def _fetch_one(self, source: VenuePriceSource) -> VenuePrice:
        try:
            quote = await source.fetch_price()
            return VenuePrice(
                venue=source.name,
                pair=source.pair,
                quote=quote,
                error=None if quote else "No price returned",
                volume_24h_usd=source.volume_24h_usd,
            )
        except Exception as e:
            return VenuePrice(
                venue=source.name,
                pair=source.pair,
                quote=None,
                error=str(e),
            )

    def get_price(self, venue: str) -> Optional[VenuePrice]:
        return self._prices.get(venue)

    def get_all_prices(self) -> dict[str, VenuePrice]:
        return self._prices.copy()

    @property
    def last_fetch_time(self) -> float:
        return self._last_fetch

    async def close(self):
        for source in self.sources.values():
            await source.close()


# =============================================================================
# Factory
# =============================================================================


def create_venue_aggregator(
    bybit_enabled: bool = True,
    quidax_enabled: bool = True,
    blockradar_adapter: "Optional[VenueAdapter]" = None,
) -> VenuePriceAggregator:
    """Create a venue price aggregator with configured sources."""
    sources: list[VenuePriceSource] = []

    if bybit_enabled:
        sources.append(BybitP2PPriceSource())

    if quidax_enabled:
        sources.append(QuidaxPriceSource())
        
    from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    sources.append(
        DexAdapterPriceSource(
            venue_name="uni-bsc",
            pair="cNGN/USDT",
            pool_address=UNISWAP_BSC_POOL_READ_CONFIG.pool_address,
        )
    )
    sources.append(
        DexAdapterPriceSource(
            venue_name="uni-base",
            pair="cNGN/USDC",
            pool_address=UNISWAP_BASE_POOL_READ_CONFIG.pool_address,
            dexscreener_chain="base",
        )
    )
    sources.append(
        DexAdapterPriceSource(
            venue_name="assetchain",
            pair="cNGN/USDT",
            pool_address=ASSETCHAIN_POOL_READ_CONFIG.pool_address,
        )
    )

    if blockradar_adapter:
        sources.append(BlockradarPriceSource(blockradar_adapter))

    return VenuePriceAggregator(sources)
