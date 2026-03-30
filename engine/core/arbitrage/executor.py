"""Arbitrage execution across DEX and CEX venues."""

import re
import time
from decimal import Decimal
from typing import Any, Optional

import structlog

from engine.api.schemas import ArbitrageTrade
from engine.venues.base import VenueAdapter
from engine.venues.cex.quidax import QuidaxAdapter

logger = structlog.get_logger()


def _clean_revert(err: Any) -> str | None:
    """Decode or strip Solidity revert data from web3 error strings.

    web3 often formats reverts as: "execution reverted: MESSAGE: 0xDATA"
    Strips the trailing hex — the text is already decoded by web3.
    Falls back to ABI-decoding Error(string) if the error is raw hex only.
    """
    if not err:
        return err
    if isinstance(err, (tuple, list)):
        parts: list[str] = []
        for item in err:
            cleaned_item = _clean_revert(item)
            if cleaned_item and cleaned_item not in parts:
                parts.append(cleaned_item)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return " | ".join(parts)
    if not isinstance(err, str):
        err = str(err)
    cleaned = re.sub(r":\s*0x[0-9a-fA-F]{8,}$", "", err).strip()
    if re.fullmatch(r"0x08c379a0[0-9a-fA-F]*", cleaned):
        try:
            from eth_abi import decode
            msg = decode(["string"], bytes.fromhex(cleaned[10:]))[0]
            return f"execution reverted: {msg}"
        except Exception:
            pass
    return cleaned

_RPC_MARKERS = (
    "connectionerror", "timeouterror", "timeout", "connection refused",
    "httperror", "read timed out", "max retries", "connectionrefused",
    "remotedisconnected", "broken pipe", "connection reset",
)
_BALANCE_MARKERS = (
    "transfer amount exceeds balance", "insufficient balance",
    "erc20: transfer",
)
_PERMIT2_MARKERS = ("allowanceexpired", "insufficientallowance")
_POOL_PAUSED_MARKERS = ("lok", "poolnotinitialized", "paused")


def _classify_preflight_error(err: str | None) -> str:
    """Classify a simulate_swap error string into one of five categories.

    Returns one of: "balance", "permit2", "rpc", "pool_paused", "unknown".
    Only "balance" should zero the venue's cNGN inventory.
    """
    if not err:
        return "unknown"
    low = err.lower()
    for m in _RPC_MARKERS:
        if m in low:
            return "rpc"
    for m in _BALANCE_MARKERS:
        if m in low:
            return "balance"
    for m in _PERMIT2_MARKERS:
        if m in low:
            return "permit2"
    for m in _POOL_PAUSED_MARKERS:
        if m in low:
            return "pool_paused"
    return "unknown"


# Slippage tolerance applied to arb swaps (separate from LP slippage)
_ARB_SLIPPAGE_BPS = 10  # 0.1% — matches optimizer assumption in cex_dex.py / dex_dex.py


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dex_execution_slippage(venue: VenueAdapter) -> Decimal:
    params = getattr(venue, "params", None)
    max_slippage_percent = getattr(params, "max_slippage_percent", None)
    if max_slippage_percent is not None:
        return Decimal(max_slippage_percent) / Decimal(100)
    return Decimal(_ARB_SLIPPAGE_BPS) / Decimal(10000)


def _is_dex_execution_venue(venue: VenueAdapter) -> bool:
    return all(
        hasattr(venue, attr)
        for attr in ("swap", "stable_address", "cngn_address", "stable_decimals", "cngn_decimals")
    )


class ArbitrageExecutor:
    """Executes individual DEX and CEX trade legs for the arbitrage engine."""

    def __init__(self, venues: dict[str, VenueAdapter]):
        self.venues = venues

    async def execute_dex_buy(
        self,
        venue_name: str,
        amount_usd: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Swap stablecoin → cNGN on a DEX."""
        venue = self.venues[venue_name]

        price_quote = await venue.get_current_price()
        if price_quote is None or price_quote.mid == 0:
            return ArbitrageTrade(
                id=0, opportunity_id=opportunity_id, venue=venue_name, side="buy",
                amount=Decimal("0"), status="failed", timestamp=_now_ms(),
                error="Could not fetch DEX price",
            )

        current_price = price_quote.mid  # stablecoin per cNGN
        amount_in_raw = int(amount_usd * Decimal(10 ** venue.stable_decimals))

        slippage = _dex_execution_slippage(venue)
        expected_cngn = amount_usd / current_price
        min_out_raw = int(expected_cngn * (1 - slippage) * Decimal(10 ** venue.cngn_decimals))

        result = await venue.swap(venue.stable_address, amount_in_raw, min_out_raw)

        actual_cngn = (
            Decimal(result.output_raw) / Decimal(10 ** venue.cngn_decimals)
            if result.output_raw
            else expected_cngn
        )

        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="buy",
            amount=actual_cngn,
            price=current_price,
            tx_hash=result.hash or None,
            status="confirmed" if result.status == "confirmed" else "failed",
            timestamp=_now_ms(),
            error=_clean_revert(result.error),
        )

    async def execute_dex_sell(
        self,
        venue_name: str,
        amount_cngn: Decimal,
        min_amount_out_usd: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Swap cNGN → stablecoin on a DEX."""
        venue = self.venues[venue_name]

        price_quote = await venue.get_current_price()
        current_price = price_quote.mid if price_quote else Decimal("0")

        amount_in_raw = int(amount_cngn * Decimal(10 ** venue.cngn_decimals))
        min_out_raw = int(min_amount_out_usd * Decimal(10 ** venue.stable_decimals))

        result = await venue.swap(venue.cngn_address, amount_in_raw, min_out_raw)

        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="sell",
            amount=amount_cngn,
            price=current_price,
            tx_hash=result.hash or None,
            status="confirmed" if result.status == "confirmed" else "failed",
            timestamp=_now_ms(),
            error=_clean_revert(result.error),
        )

    async def execute_cex_buy(
        self,
        venue_name: str,
        amount_usd: Decimal,
        limit_price: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Place a market buy order on a CEX."""
        venue: QuidaxAdapter = self.venues[venue_name]  # type: ignore

        amount_cngn = amount_usd / limit_price
        success, executed_cngn, avg_price, error = await venue.place_market_order("buy", amount_cngn)
        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="buy",
            amount=executed_cngn if success else amount_cngn,
            price=avg_price if success else limit_price,
            status="submitted" if success else "failed",
            timestamp=_now_ms(),
            error=error,
        )

    async def execute_cex_sell(
        self,
        venue_name: str,
        amount_cngn: Decimal,
        limit_price: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Place a market sell order on a CEX."""
        venue: QuidaxAdapter = self.venues[venue_name]  # type: ignore

        success, executed_cngn, avg_price, error = await venue.place_market_order("sell", amount_cngn)
        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="sell",
            amount=executed_cngn if success else amount_cngn,
            price=avg_price if success else limit_price,
            status="submitted" if success else "failed",
            timestamp=_now_ms(),
            error=error,
        )
