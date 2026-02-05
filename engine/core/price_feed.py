"""Price feed aggregation with Bybit P2P fraud filtering."""

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import httpx
import structlog

from engine.api.schemas import PriceQuote

logger = structlog.get_logger()


@dataclass
class P2PAd:
    """Bybit P2P advertisement."""

    price: Decimal
    quantity: Decimal
    completed_orders: int
    completion_rate: float
    avg_release_time: int  # seconds
    is_online: bool


@dataclass
class PriceFeedConfig:
    """Configuration for price feed fraud filtering."""

    # Bybit P2P filtering
    min_completed_orders: int = 100
    min_completion_rate: float = 0.95
    max_avg_release_time: int = 900  # 15 minutes
    skip_first_n: int = 5
    sample_size: int = 10

    # Staleness
    max_age_seconds: int = 60

    # Outlier rejection
    max_deviation_from_median: float = 0.02  # 2%


class PriceFeed:
    """
    Aggregates USDT/NGN price from Bybit P2P with fraud filtering.

    Filtering strategy:
    1. Skip first N ads (likely fraud/bait prices)
    2. Filter by trader reputation (completed orders, completion rate, release time)
    3. Take a sample of reputable ads
    4. Reject outliers using median-based filtering
    5. Calculate volume-weighted average price
    """

    def __init__(self, config: PriceFeedConfig | None = None):
        self.config = config or PriceFeedConfig()
        self._cache: Optional[tuple[PriceQuote, float]] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_price(self) -> PriceQuote:
        """
        Get current price, using cache if fresh.

        Returns:
            PriceQuote with bid, ask, and mid prices
        """
        if self._cache:
            quote, fetched_at = self._cache
            if time.time() - fetched_at < self.config.max_age_seconds:
                return quote

        return await self._fetch_fresh_price()

    async def _fetch_fresh_price(self) -> PriceQuote:
        """Fetch and filter P2P prices."""
        # Fetch buy and sell sides in parallel
        buy_ads, sell_ads = await asyncio.gather(
            self._fetch_bybit_p2p("buy"),  # People buying USDT (selling NGN)
            self._fetch_bybit_p2p("sell"),  # People selling USDT (buying NGN)
        )

        # Filter and aggregate
        # Ask = price at which we can sell USDT (people are buying)
        # Bid = price at which we can buy USDT (people are selling)
        ask = self._filter_and_aggregate(buy_ads)
        bid = self._filter_and_aggregate(sell_ads)
        mid = (bid + ask) / 2

        quote = PriceQuote(
            source="bybit_p2p_filtered",
            timestamp=int(time.time() * 1000),
            bid=bid,
            ask=ask,
            mid=mid,
        )

        self._cache = (quote, time.time())

        logger.info(
            "price_updated",
            bid=float(bid),
            ask=float(ask),
            mid=float(mid),
            spread_bps=int((ask - bid) / mid * 10000),
        )

        return quote

    async def _fetch_bybit_p2p(self, side: str) -> list[P2PAd]:
        """
        Fetch P2P ads from Bybit.

        Args:
            side: "buy" or "sell" (from the user's perspective)

        Returns:
            List of P2P advertisements
        """
        client = await self._get_client()
        url = "https://api2.bybit.com/fiat/otc/item/online"

        # side "1" = buy USDT, "0" = sell USDT
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

            logger.debug(
                "fetched_p2p_ads",
                side=side,
                count=len(ads),
            )
            return ads

        except Exception as e:
            logger.error("p2p_fetch_failed", side=side, error=str(e))
            raise

    def _filter_and_aggregate(self, ads: list[P2PAd]) -> Decimal:
        """
        Apply fraud filtering and calculate VWAP.

        Args:
            ads: List of P2P advertisements

        Returns:
            Volume-weighted average price after filtering
        """
        # 1. Skip first N (likely fraud/bait)
        after_skip = ads[self.config.skip_first_n :]

        # 2. Filter by reputation
        reputable = [
            ad
            for ad in after_skip
            if ad.completed_orders >= self.config.min_completed_orders
            and ad.completion_rate >= self.config.min_completion_rate
            and ad.avg_release_time <= self.config.max_avg_release_time
            and ad.is_online
        ]

        logger.debug(
            "reputation_filter",
            before=len(after_skip),
            after=len(reputable),
        )

        # 3. Take sample
        sample = reputable[: self.config.sample_size]
        if len(sample) < 3:
            raise ValueError(
                f"Insufficient reputable P2P ads for price discovery: {len(sample)}"
            )

        # 4. Outlier rejection (median-based)
        prices = sorted([ad.price for ad in sample])
        median = prices[len(prices) // 2]

        max_dev = Decimal(str(self.config.max_deviation_from_median))
        filtered = [ad for ad in sample if abs(ad.price - median) / median <= max_dev]

        if len(filtered) < 2:
            # If too many outliers, fall back to sample
            logger.warning("outlier_filter_too_aggressive", using_sample=True)
            filtered = sample

        # 5. Volume-weighted average
        total_volume = sum(ad.quantity for ad in filtered)
        if total_volume == 0:
            # Fallback to simple average if no volume info
            return sum(ad.price for ad in filtered) / len(filtered)

        vwap = sum(ad.price * ad.quantity for ad in filtered) / total_volume

        logger.debug(
            "price_aggregated",
            sample_size=len(filtered),
            vwap=float(vwap),
            median=float(median),
        )

        return vwap
