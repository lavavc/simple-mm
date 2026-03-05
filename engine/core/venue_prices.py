"""Per-venue price fetching for arbitrage and LP management.

Each venue is a separate price source. All venues are fetched in parallel
to get a complete picture of the market at a point in time.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

import httpx
import structlog

from engine.api.schemas import PriceQuote

if TYPE_CHECKING:
    from engine.venues.base import VenueAdapter
    from engine.venues.dex.base import PoolPriceReader

logger = structlog.get_logger()


# =============================================================================
# Base class
# =============================================================================


class VenuePriceSource(ABC):
    """Base class for venue-specific price sources."""

    name: str
    pair: str  # e.g. "USDT/NGN", "cNGN/USDC"

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
    min_completion_rate: float = 0.95
    max_avg_release_time: int = 900  # 15 minutes
    skip_first_n: int = 5
    sample_size: int = 10
    max_deviation_from_median: float = 0.02  # 2%
    cache_seconds: int = 60


class BybitP2PPriceSource(VenuePriceSource):
    """Fetches USDT/NGN price from Bybit P2P with fraud filtering.

    Strategy:
    1. Skip first N ads (likely fraud/bait prices)
    2. Filter by trader reputation
    3. Reject outliers using median-based filtering
    4. Calculate volume-weighted average
    """

    name = "bybit"
    pair = "USDT/NGN"

    def __init__(self, config: BybitConfig | None = None):
        self.config = config or BybitConfig()
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Optional[tuple[PriceQuote, float]] = None

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
        """Apply fraud filtering and calculate VWAP."""
        if len(ads) < self.config.skip_first_n + 3:
            return None

        after_skip = ads[self.config.skip_first_n:]

        reputable = [
            ad
            for ad in after_skip
            if ad.completed_orders >= self.config.min_completed_orders
            and ad.completion_rate >= self.config.min_completion_rate
            and ad.avg_release_time <= self.config.max_avg_release_time
            and ad.is_online
        ]

        if len(reputable) < 3:
            logger.warning("bybit_insufficient_reputable_ads", count=len(reputable))
            return None

        sample = reputable[: self.config.sample_size]

        prices = sorted([ad.price for ad in sample])
        median = prices[len(prices) // 2]

        max_dev = Decimal(str(self.config.max_deviation_from_median))
        filtered = [ad for ad in sample if abs(ad.price - median) / median <= max_dev]

        if len(filtered) < 2:
            filtered = sample

        total_volume = sum(ad.quantity for ad in filtered)
        if total_volume == 0:
            return sum(ad.price for ad in filtered) / len(filtered)

        return sum(ad.price * ad.quantity for ad in filtered) / total_volume


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
    ):
        self.name = venue_name
        self.pair = pair
        self.pool_address = pool_address

    async def fetch_price(self) -> Optional[PriceQuote]:
        from engine.core.arbitrage.simulator import get_cached_pool_state, Q96

        sqrt_p, _, _, _, timestamp, _ = get_cached_pool_state(self.pool_address)

        if sqrt_p is None:
            logger.warning("dex_price_cache_miss", venue=self.name)
            return None

        # token0=cNGN(6), token1=USDC(6): price = sqrtP^2, no inversion
        if self.name in ("aerodrome", "uni-base"):
            price = ((sqrt_p / Q96) ** 2) * Decimal(10 ** (6 - 6))
            human_price = price
        else:
            # token0=USDT(18), token1=cNGN(6): invert to get USD per cNGN
            price = ((sqrt_p / Q96) ** 2) * Decimal(10 ** (18 - 6))
            human_price = Decimal(1) / price if price > 0 else Decimal(0)
            
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
