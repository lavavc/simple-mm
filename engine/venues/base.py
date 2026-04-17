"""Base classes and capability protocols for venue adapters."""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Optional, Protocol, TypeGuard

from engine.types import Position, PriceQuote, TxResult


class VenueAdapter(ABC):
    """Abstract base class for all venue adapters."""

    name: str
    enabled: bool = True
    paused: bool = False

    @abstractmethod
    async def get_position(self) -> Position: ...

    @abstractmethod
    async def get_current_price(self) -> Optional[PriceQuote]: ...

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class DexExecutionVenue(Protocol):
    """DEX-only execution surface shared across arb modules."""

    stable_address: str
    cngn_address: str
    stable_decimals: int
    cngn_decimals: int
    stable_token: Any
    cngn_token: Any
    trade_account: Any

    async def get_current_price(self) -> Optional[PriceQuote]: ...
    async def swap(self, token_in: str, amount_in: int, min_amount_out: int) -> TxResult: ...
    def simulate_swap(self, token_in: str, amount_in: int, min_amount_out: int) -> str | None: ...
    async def ensure_trade_approvals(self) -> None: ...


class SyncOrderLadderVenue(Protocol):
    async def sync_order_ladder(self, reference_price_ngn: Decimal) -> None: ...


class DepthVenue(Protocol):
    async def get_order_book_depth(self, limit: int = 50) -> Any: ...
    async def get_position(self) -> Any: ...


class WebhookVenue(Protocol):
    async def handle_webhook(self, event: dict[str, Any]) -> None: ...


class CloseableVenue(Protocol):
    async def close(self) -> None: ...


def is_dex_execution_venue(venue: VenueAdapter) -> TypeGuard[DexExecutionVenue]:
    """Return True when a venue exposes the on-chain execution surface."""
    return all(
        hasattr(venue, attr)
        for attr in (
            "swap",
            "simulate_swap",
            "stable_address",
            "cngn_address",
            "stable_decimals",
            "cngn_decimals",
            "stable_token",
            "cngn_token",
            "trade_account",
            "ensure_trade_approvals",
        )
    )


def is_closeable(venue: VenueAdapter) -> TypeGuard[CloseableVenue]:
    return hasattr(venue, "close")
