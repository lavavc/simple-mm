"""Telegram bot for operational control of the trading engine."""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import time
from typing import Any, cast

import structlog
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from engine.config import Settings
from engine.runtime import EngineRuntime

logger = structlog.get_logger()

_app: Any | None = None
_settings: Settings | None = None
_runtime: EngineRuntime | None = None
_recent_alerts: dict[str, float] = {}
_pending_withdrawals: dict[str, tuple[str, str | None]] = {}


def _require_runtime() -> EngineRuntime:
    if _runtime is None:
        raise RuntimeError("Telegram bot runtime is not configured")
    return _runtime


def _auth(update: Update) -> bool:
    if not _settings or not _settings.telegram_chat_id or update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == str(_settings.telegram_chat_id)


def _get_message(update: Update) -> Message | None:
    return update.effective_message


def _get_callback_query(update: Update) -> CallbackQuery | None:
    return update.callback_query


def _require_reply_message(message: object) -> Message | None:
    return message if isinstance(message, Message) else None


def _confirm_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"confirm:{action}"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])


# --- Read-only commands ---

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    runtime = _require_runtime()
    trading_state = await runtime.db.system_state.get_system_state("trading_enabled")
    trading = trading_state != "false"
    cb = False
    arb_line = "❌ Not configured"
    if runtime.arbitrage_engine:
        s = await runtime.arbitrage_engine.get_status()
        cb = s.circuit_breaker_active
        if not s.enabled:
            arb_line = "⏸ Detection paused"
        elif not s.execute_cex_dex and not s.execute_dex_dex:
            arb_line = "👁 Detection only (execution off)"
        else:
            parts = []
            if s.execute_cex_dex:
                parts.append("cex-dex")
            if s.execute_dex_dex:
                parts.append("dex-dex")
            arb_line = f"✅ Executing ({', '.join(parts)})"
    text = (
        f"*Engine Status*\n"
        f"Trading: {'✅ Running' if trading else '⏸ Paused'}\n"
        f"Arb: {arb_line}\n"
        f"Circuit breaker: {'🚨 Active' if cb else '✅ Clear'}"
    )
    await message.reply_text(text, parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    runtime = _require_runtime()
    if not runtime.venues:
        await message.reply_text("No venues configured.")
        return
    lines = ["*Positions*"]
    for name, venue in runtime.venues.items():
        try:
            lp_manager = runtime.lp_managers.get(name)
            if lp_manager is not None:
                pos = await lp_manager.get_position_as_schema()
            else:
                pos = await venue.get_position()
            lines.append(f"\n*{name}*")
            for token, amt in pos.balances.items():
                lines.append(f"  {token}: {amt:.4f}")
            if pos.lp_position:
                label = (
                    f"token_id: {pos.lp_position.token_id}"
                    if pos.lp_position.token_id is not None
                    else "token_id: unavailable"
                )
                lines.append(f"  {label}")
                lines.append(f"  snapshot_status: {pos.lp_position.snapshot_status}")
                if pos.lp_position.snapshot_message:
                    lines.append(f"  snapshot_message: {pos.lp_position.snapshot_message}")
                if pos.lp_position.range_min is not None and pos.lp_position.range_max is not None:
                    lines.append(
                        f"  range: {pos.lp_position.range_min:.6f} -> {pos.lp_position.range_max:.6f}"
                    )
                else:
                    lines.append("  range: unavailable")
                if pos.lp_position.in_range is None:
                    lines.append("  in_range: unknown")
                else:
                    lines.append(f"  in_range: {'yes' if pos.lp_position.in_range else 'no'}")
                if pos.position_value_usd is not None:
                    lines.append(f"  value_usd: {pos.position_value_usd:.4f}")
                if pos.lp_position.our_share_pct is not None:
                    lines.append(f"  our_share_pct: {pos.lp_position.our_share_pct:.4f}")
        except Exception as e:
            lines.append(f"\n*{name}*: error ({e})")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    runtime = _require_runtime()
    if not runtime.account_manager:
        await message.reply_text("Account manager not configured.")
        return
    try:
        balances = await runtime.account_manager.check_all_balances(runtime.token_contracts)
        lines = ["*Account Balances*"]
        for b in balances:
            lines.append(f"\n*{b.role}*")
            for token, amt in (b.token_balances or {}).items():
                lines.append(f"  {token}: {amt}")
        await message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await message.reply_text(f"Error: {e}")


async def cmd_arb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    runtime = _require_runtime()
    if not runtime.arbitrage_engine:
        await message.reply_text("Arbitrage engine not configured.")
        return
    try:
        s = await runtime.arbitrage_engine.get_status()
        text = (
            f"*Arbitrage Status*\n"
            f"Enabled: {'✅' if s.enabled else '❌'}\n"
            f"Consecutive failures: {s.consecutive_failures}\n"
            f"Circuit breaker: {'🚨 Active' if s.circuit_breaker_active else '✅ Clear'}\n"
            f"Opportunities (24h): {s.opportunities_detected_24h} detected / {s.opportunities_executed_24h} executed\n"
            f"Profit (24h): ${s.total_profit_24h_usd:.2f}"
        )
        await message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await message.reply_text(f"Error: {e}")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    alerts = await _require_runtime().db.alerts.get_alerts(5)
    if not alerts:
        await message.reply_text("No recent alerts.")
        return
    lines = ["*Last 5 Alerts*"]
    for a in alerts:
        lines.append(f"\n[{a.severity}] {a.message}")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Destructive commands (require inline keyboard confirm) ---

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    await message.reply_text(
        "⚠️ Pause all trading globally. Confirm?",
        reply_markup=_confirm_kb("pause"),
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    await message.reply_text(
        "⚠️ Resume all trading. Confirm?",
        reply_markup=_confirm_kb("resume"),
    )


async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = _get_message(update)
    if message is None:
        return
    args = context.args or []
    if len(args) < 2:
        await message.reply_text("Usage: /withdraw <uni-base|uni-bsc> <to_address>")
        return
    venue, to_address = args[0], args[1]
    if venue not in ("uni-base", "uni-bsc"):
        await message.reply_text("Usage: /withdraw <uni-base|uni-bsc> <to_address>")
        return
    token = secrets.token_hex(4)
    _pending_withdrawals[token] = (venue, to_address)
    await message.reply_text(
        f"⚠️ Withdraw LP positions: *{venue}* → `{to_address}`. Confirm?",
        reply_markup=_confirm_kb(f"wd:{token}"),
        parse_mode="Markdown",
    )


async def cmd_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔥 Unwind + Stop", callback_data="confirm:shutdown:unwind"),
        InlineKeyboardButton("🛑 Stop Only", callback_data="confirm:shutdown:stop"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])
    await message.reply_text("⚠️ Shutdown engine. Choose action:", reply_markup=keyboard)


async def cmd_reset_breaker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    del context
    message = _get_message(update)
    if message is None:
        return
    await message.reply_text(
        "⚠️ Reset circuit breaker and re-enable arb. Confirm?",
        reply_markup=_confirm_kb("reset_breaker"),
    )


async def cmd_recover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    message = _get_message(update)
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /recover <opp_id>")
        return
    opp_id = context.args[0]
    await message.reply_text(
        f"⚠️ Recover half-open arb `{opp_id}`.\n"
        f"Will retry sell if sell-side has cNGN, otherwise reverse the buy to recover capital.",
        reply_markup=_confirm_kb(f"recover:{opp_id}"),
        parse_mode="Markdown",
    )


# --- Callback handler ---

async def _do_withdraw(
    venue: str,
    to_address: str | None = None,
    *,
    action_type: str = "manual_withdraw",
    triggered_by: str = "telegram:withdraw",
) -> str:
    runtime = _require_runtime()
    if venue == "all":
        targets = dict(runtime.lp_managers)
    elif venue in runtime.lp_managers:
        targets = {venue: runtime.lp_managers[venue]}
    else:
        return f"❌ Venue {venue} not found or not a DEX."
    results = []
    for name, lp_manager in targets.items():
        venue_results = await runtime.scheduler.lp_rebalancer.withdraw_positions(
            lp_manager,
            recipient=to_address,
            action_type=action_type,
            triggered_by=triggered_by,
        )
        for item in venue_results:
            results.append(f"{name}#{item['token_id']}: {item['status']}")
    return ("✅ Withdrawn:\n" + "\n".join(results)) if results else f"ℹ️ No positions on {venue}."


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = _get_callback_query(update)
    if query is None:
        return
    if not _settings or query.message is None or str(query.message.chat.id) != str(_settings.telegram_chat_id):
        await query.answer()
        return
    runtime = _require_runtime()
    await query.answer()
    data = query.data or ""
    reply_message = _require_reply_message(query.message)

    if data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
    elif data == "confirm:pause":
        await runtime.scheduler.pause()
        await query.edit_message_text("⏸ Trading paused.")
    elif data == "confirm:resume":
        await runtime.scheduler.resume()
        await query.edit_message_text("▶️ Trading resumed.")
    elif data.startswith("confirm:wd:"):
        token = data.split(":", 2)[2]
        pending = _pending_withdrawals.pop(token, None)
        if pending is None:
            await query.edit_message_text("❌ Withdraw request expired or not found.")
            return
        venue, to_address = pending
        await query.edit_message_text(f"⏳ Withdrawing {venue}...")
        msg = await _do_withdraw(venue, to_address)
        if reply_message is not None:
            await reply_message.reply_text(msg)
    elif data == "confirm:shutdown:unwind":
        await query.edit_message_text("⏳ Unwinding positions and stopping...")
        await runtime.scheduler.pause()
        msg = await _do_withdraw(
            "all",
            action_type="shutdown_unwind",
            triggered_by="telegram:shutdown_unwind",
        )
        if reply_message is not None:
            await reply_message.reply_text(f"{msg}\n🛑 Shutting down.")
        asyncio.get_event_loop().call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    elif data == "confirm:shutdown:stop":
        await query.edit_message_text("🛑 Engine shutting down.")
        asyncio.get_event_loop().call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    elif data == "confirm:reset_breaker":
        if runtime.arbitrage_engine:
            cast(Any, runtime.arbitrage_engine).reset_circuit_breaker()
            await query.edit_message_text("✅ Circuit breaker reset.")
        else:
            await query.edit_message_text("❌ Arbitrage engine not configured.")
    elif data.startswith("confirm:recover:"):
        opp_id = data.split(":", 2)[2]
        if not runtime.arbitrage_engine:
            await query.edit_message_text("❌ Arbitrage engine not configured.")
            return
        await query.edit_message_text(f"⏳ Recovering {opp_id}...")
        try:
            try:
                result = await runtime.arbitrage_engine.recover_dex_half_open(opp_id)
                method = "sell retried" if result["method"] == "retry_sell" else "buy reversed"
                profit = result["profit_usd"]
                sign = "+" if profit >= 0 else ""
                if reply_message is not None:
                    await reply_message.reply_text(
                        f"✅ Recovered DEX-DEX ({method}): tx {result['sell_tx_hash']}, P&L {sign}${profit:.2f}"
                    )
            except ValueError as e:
                if "Unknown DEX arbitrage opportunity" not in str(e):
                    raise
                result = await runtime.arbitrage_engine.recover_cex_half_open(opp_id)
                profit = result["profit_usd"]
                sign = "+" if profit >= 0 else ""
                method = result["method"].replace("_", " ")
                if reply_message is not None:
                    await reply_message.reply_text(
                        f"✅ Recovered CEX-DEX ({method}): P&L {sign}${profit:.2f}"
                    )
        except Exception as e:
            if reply_message is not None:
                await reply_message.reply_text(f"❌ Recovery failed: {e}")


# --- Alert forwarding ---

async def forward_alert(event: dict[str, Any]) -> None:
    if not _app or not _settings or not _settings.telegram_chat_id:
        return
    severity = event.get("severity", "")
    if severity not in ("critical", "warning"):
        return
    now = time.monotonic()
    for key, expires_at in list(_recent_alerts.items()):
        if expires_at <= now:
            _recent_alerts.pop(key, None)
    cooldown_s = float(event.get("cooldown_s", 60))
    dedupe_key = str(event.get("dedupe_key") or f"{severity}:{event.get('message', '')}")
    if cooldown_s > 0:
        last_expires_at = _recent_alerts.get(dedupe_key)
        if last_expires_at and last_expires_at > now:
            logger.info("telegram_alert_suppressed_duplicate", dedupe_key=dedupe_key, severity=severity)
            return
        _recent_alerts[dedupe_key] = now + cooldown_s
    icon = "🚨" if severity == "critical" else "⚠️"
    try:
        await _app.bot.send_message(_settings.telegram_chat_id, f"{icon} {event.get('message', '')}")
    except Exception as e:
        _recent_alerts.pop(dedupe_key, None)
        logger.warning("telegram_alert_failed", error=str(e))


# --- Lifecycle ---

async def start(s: Settings, runtime: EngineRuntime) -> None:
    global _app, _settings, _runtime
    _settings = s
    _runtime = runtime

    _app = Application.builder().token(cast(str, s.telegram_bot_token)).build()
    _app.add_handler(CommandHandler("status", cmd_status))
    _app.add_handler(CommandHandler("positions", cmd_positions))
    _app.add_handler(CommandHandler("balances", cmd_balances))
    _app.add_handler(CommandHandler("arb", cmd_arb))
    _app.add_handler(CommandHandler("alerts", cmd_alerts))
    _app.add_handler(CommandHandler("pause", cmd_pause))
    _app.add_handler(CommandHandler("resume", cmd_resume))
    _app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    _app.add_handler(CommandHandler("shutdown", cmd_shutdown))
    _app.add_handler(CommandHandler("reset_breaker", cmd_reset_breaker))
    _app.add_handler(CommandHandler("recover", cmd_recover))
    _app.add_handler(CallbackQueryHandler(handle_callback))

    await _app.initialize()
    await _app.start()
    if _app.updater is not None:
        await _app.updater.start_polling(drop_pending_updates=True)
    logger.warning("telegram_bot_started")


async def stop() -> None:
    global _app, _runtime
    if _app:
        if _app.updater is not None:
            await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        _app = None
        logger.info("telegram_bot_stopped")
    _runtime = None
