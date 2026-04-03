"""Base classes for venue adapters."""

from abc import ABC, abstractmethod
from typing import Optional
from engine.api.schemas import Position, PriceQuote


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
