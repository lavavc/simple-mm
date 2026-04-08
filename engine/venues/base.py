"""Base classes for venue adapters."""

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, TypeGuard

from engine.types import Position, PriceQuote, TxResult


class VenueAdapter(ABC):
    """Abstract base class for all venue adapters."""

    name: str
    enabled: bool = True
    paused: bool = False

    @abstractmethod
    async def get_position(self) -> Position:
        """Get current position at this venue."""
        pass

    @abstractmethod
    async def get_current_price(self) -> Optional[PriceQuote]:
        """Get current price from this venue (if applicable)."""
        pass

    def pause(self) -> None:
        """Pause this venue."""
        self.paused = True

    def resume(self) -> None:
        """Resume this venue."""
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

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Get the current price from the venue."""

    async def swap(self, token_in: str, amount_in: int, min_amount_out: int) -> TxResult:
        """Execute a swap."""

    def simulate_swap(self, token_in: str, amount_in: int, min_amount_out: int) -> str | None:
        """Run the swap as a preflight simulation."""

    async def ensure_trade_approvals(self) -> None:
        """Ensure the venue is approved for trading."""


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
