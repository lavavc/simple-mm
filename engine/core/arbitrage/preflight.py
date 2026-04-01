"""Preflight helpers shared by arbitrage execution paths."""

from decimal import Decimal
from typing import Any
import structlog
logger = structlog.get_logger()


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _fmt_decimal(value: Decimal | None, places: int = 2) -> str | None:
    if value is None:
        return None
    return f"{value:,.{places}f}"


def _fmt_usd(value: Decimal | None) -> str | None:
    formatted = _fmt_decimal(value, places=2)
    return f"${formatted}" if formatted is not None else None


def _short_address(address: str | None) -> str | None:
    if not address:
        return None
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-6:]}"


def _infer_wallet_symbol(venue: Any, wallet_asset: str) -> str:
    if wallet_asset == "stable":
        config = getattr(venue, "config", None)
        if config:
            return config.token0_symbol if getattr(config, "invert_price", False) else config.token1_symbol
        return "stable"
    return "cNGN"


def _build_preflight_context(engine, venue_name: str, log_ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {}
    lines: list[str] = []

    direction = log_ctx.get("direction")
    if direction:
        details["direction"] = direction
        lines.append(f"Direction: {direction}")

    size_usd = _coerce_decimal(log_ctx.get("size_usd"))
    if size_usd is not None:
        details["size_usd"] = float(size_usd)
        lines.append(f"Trade size: {_fmt_usd(size_usd)}")

    sell_cngn_est = _coerce_decimal(log_ctx.get("sell_cngn_est"))
    if sell_cngn_est is not None:
        details["sell_cngn_est"] = float(sell_cngn_est)
        lines.append(f"Estimated sell: {_fmt_decimal(sell_cngn_est)} cNGN")

    min_out_usd = _coerce_decimal(log_ctx.get("min_out_usd"))
    if min_out_usd is not None:
        details["min_out_usd"] = float(min_out_usd)
        lines.append(f"Min out: {_fmt_usd(min_out_usd)}")

    wallet_asset = str(log_ctx.get("wallet_asset") or "cngn")
    details["wallet_asset"] = wallet_asset

    venue = getattr(engine, "venues", {}).get(venue_name)
    wallet_symbol = log_ctx.get("wallet_symbol") or _infer_wallet_symbol(venue, wallet_asset)
    details["wallet_symbol"] = wallet_symbol

    wallet_amount = _coerce_decimal(log_ctx.get("wallet_amount"))
    if wallet_amount is None:
        if wallet_asset == "stable":
            wallet_amount = engine.inventory.state.per_account_stable.get(venue_name)
        else:
            wallet_amount = engine.inventory.state.per_account_cngn.get(venue_name)
    if wallet_amount is not None:
        details["wallet_amount"] = float(wallet_amount)

    sell_price_usd = _coerce_decimal(log_ctx.get("sell_price_usd"))
    if sell_price_usd is None or sell_price_usd <= 0:
        snapshot_price = engine.inventory.state.cngn_price_usd
        if snapshot_price > 0:
            sell_price_usd = snapshot_price
    if sell_price_usd is not None and sell_price_usd > 0:
        details["sell_price_usd"] = float(sell_price_usd)

    wallet_usd = _coerce_decimal(log_ctx.get("wallet_amount_usd"))
    if wallet_usd is None and wallet_amount is not None:
        if wallet_asset == "stable":
            wallet_usd = wallet_amount
        elif sell_price_usd is not None and sell_price_usd > 0:
            wallet_usd = wallet_amount * sell_price_usd
    if wallet_usd is not None:
        details["wallet_usd"] = float(wallet_usd)

    wallet_address = getattr(getattr(venue, "trade_account", None), "address", None)
    if wallet_address:
        details["wallet_address"] = wallet_address

    if wallet_amount is not None or wallet_address:
        wallet_bits = []
        short_wallet = _short_address(wallet_address)
        if short_wallet:
            wallet_bits.append(short_wallet)
        if wallet_amount is not None:
            wallet_bits.append(f"{_fmt_decimal(wallet_amount)} {wallet_symbol}")
        if wallet_usd is not None:
            wallet_bits.append(f"~{_fmt_usd(wallet_usd)}")
        lines.append(f"Wallet: {' | '.join(wallet_bits)}")

    required_amount = _coerce_decimal(log_ctx.get("required_amount"))
    required_symbol = str(log_ctx.get("required_symbol") or wallet_symbol)
    if required_amount is None and sell_cngn_est is not None and wallet_asset == "cngn":
        required_amount = sell_cngn_est
        required_symbol = wallet_symbol
    if required_amount is not None:
        details["required_amount"] = float(required_amount)
        details["required_symbol"] = required_symbol

    if required_amount is not None and wallet_amount is not None and required_amount > wallet_amount:
        shortfall = required_amount - wallet_amount
        details["wallet_shortfall_amount"] = float(shortfall)
        details["wallet_shortfall_symbol"] = required_symbol
        lines.append(f"Shortfall: {_fmt_decimal(shortfall)} {required_symbol}")

    if not lines:
        return "", details
    return "\n" + "\n".join(lines), details


def _handle_preflight_error(engine, venue_name: str, err: str | None, log_key: str, **log_ctx) -> None:
    """Classify a simulate_swap failure and take the appropriate action.

    Only a confirmed balance revert zeros the venue's cNGN inventory.
    All other categories leave inventory intact and either broadcast a warning
    (rpc, unknown) or trip the circuit breaker (pool_paused, permit2).
    """
    from engine.core.arbitrage.executor import _classify_preflight_error
    category = _classify_preflight_error(err)
    context_text, context_fields = _build_preflight_context(engine, venue_name, log_ctx)
    event_base = {
        "type": "alert",
        "cooldown_s": 60,
    }
    log_data = {**log_ctx, **context_fields}

    if category == "balance":
        wallet_asset = str(context_fields.get("wallet_asset") or "cngn")
        wallet_symbol = str(context_fields.get("wallet_symbol") or ("stable" if wallet_asset == "stable" else "cNGN"))
        if wallet_asset == "stable":
            engine.inventory.reconcile_stables({venue_name: Decimal("0")})
            balance_message = (
                f"{wallet_symbol} balance on {venue_name} is zero or below required amount — "
                "stable inventory zeroed, venue excluded from sizing. "
            )
        else:
            engine.inventory.reconcile_cngn({venue_name: Decimal("0")})
            balance_message = (
                f"{wallet_symbol} balance on {venue_name} is zero or below required amount — "
                "inventory zeroed, venue excluded from sizing. "
            )
        logger.warning(log_key, venue=venue_name, category=category, error=err, **log_data)
        engine.broadcast({**event_base, "severity": "warning",
                          "message": (
                              balance_message
                              + f"Error: {err}"
                              + f"{context_text}"
                          )})

    elif category == "rpc":
        logger.warning(log_key, venue=venue_name, category=category, error=err, **log_data)
        engine.broadcast({**event_base, "severity": "warning",
                          "message": (
                              f"RPC error on {venue_name} during preflight — trading skipped this cycle. "
                              f"Check node connectivity. Error: {err}"
                              f"{context_text}"
                          )})

    elif category == "permit2":
        logger.error(log_key, venue=venue_name, category=category, error=err, **log_data)
        engine.broadcast({**event_base, "severity": "critical",
                          "message": (
                              f"Permit2 approval missing or expired on {venue_name}. "
                              "Approvals run automatically before each swap — reset the circuit breaker to retry. "
                              f"Error: {err}"
                              f"{context_text}"
                          )})

    elif category == "pool_paused":
        engine.inventory.trip_circuit_breaker(f"Pool paused/locked on {venue_name}")
        logger.error(log_key, venue=venue_name, category=category, error=err, **log_data)
        engine.broadcast({**event_base, "severity": "critical",
                          "message": (
                              f"Pool paused or locked on {venue_name} — circuit breaker tripped. "
                              "Investigate pool state before resetting. "
                              f"Error: {err}"
                              f"{context_text}"
                          )})

    else:  # unknown
        logger.error(log_key, venue=venue_name, category=category, error=err, **log_data)
        engine.broadcast({**event_base, "severity": "warning",
                          "message": (
                              f"Unrecognised preflight revert on {venue_name} — trading skipped. "
                              f"Error: {err}"
                              f"{context_text}"
                          )})
