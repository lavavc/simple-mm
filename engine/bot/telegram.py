"""Telegram bot for operational control of the trading engine."""

import asyncio
import os
import signal
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from engine.db import get_db

logger = structlog.get_logger()

_app: Optional[Application] = None
_settings = None
_scheduler = None
_venues = None
_arbitrage_engine = None
_account_manager = None
_token_contracts: dict = {}


def _auth(update: Update) -> bool:
    if not _settings or not _settings.telegram_chat_id:
        return False
    return str(update.effective_chat.id) == str(_settings.telegram_chat_id)


def _confirm_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"confirm:{action}"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])


# --- Read-only commands ---

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    db = await get_db()
    trading_state = await db.get_system_state("trading_enabled")
    trading = trading_state != "false"
    cb = False
    arb_line = "❌ Not configured"
    if _arbitrage_engine:
        s = await _arbitrage_engine.get_status()
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
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    if not _venues:
        await update.message.reply_text("No venues configured.")
        return
    lines = ["*LP Positions*"]
    for name, venue in _venues.items():
        try:
            pos = await venue.get_position()
            lines.append(f"\n*{name}*")
            for token, amt in pos.balances.items():
                lines.append(f"  {token}: {amt:.4f}")
        except Exception as e:
            lines.append(f"\n*{name}*: error ({e})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    if not _account_manager:
        await update.message.reply_text("Account manager not configured.")
        return
    try:
        balances = await _account_manager.check_all_balances(_token_contracts)
        lines = ["*Account Balances*"]
        for b in balances:
            lines.append(f"\n*{b.role}*")
            for token, amt in (b.token_balances or {}).items():
                lines.append(f"  {token}: {amt}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_arb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    if not _arbitrage_engine:
        await update.message.reply_text("Arbitrage engine not configured.")
        return
    try:
        s = await _arbitrage_engine.get_status()
        text = (
            f"*Arbitrage Status*\n"
            f"Enabled: {'✅' if s.enabled else '❌'}\n"
            f"Consecutive failures: {s.consecutive_failures}\n"
            f"Circuit breaker: {'🚨 Active' if s.circuit_breaker_active else '✅ Clear'}\n"
            f"Opportunities (24h): {s.opportunities_detected_24h} detected / {s.opportunities_executed_24h} executed\n"
            f"Profit (24h): ${s.total_profit_24h_usd:.2f}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    db = await get_db()
    alerts = await db.get_alerts(5)
    if not alerts:
        await update.message.reply_text("No recent alerts.")
        return
    lines = ["*Last 5 Alerts*"]
    for a in alerts:
        lines.append(f"\n[{a.severity}] {a.message}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Destructive commands (require inline keyboard confirm) ---

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    await update.message.reply_text(
        "⚠️ Pause all trading globally. Confirm?",
        reply_markup=_confirm_kb("pause"),
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    await update.message.reply_text(
        "⚠️ Resume all trading. Confirm?",
        reply_markup=_confirm_kb("resume"),
    )


async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    args = context.args
    venue = args[0] if args else "all"
    if venue not in ("uni-base", "uni-bsc", "all"):
        await update.message.reply_text("Usage: /withdraw <uni-base|uni-bsc|all>")
        return
    await update.message.reply_text(
        f"⚠️ Withdraw LP positions: *{venue}*. Confirm?",
        reply_markup=_confirm_kb(f"withdraw:{venue}"),
        parse_mode="Markdown",
    )


async def cmd_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔥 Unwind + Stop", callback_data="confirm:shutdown:unwind"),
        InlineKeyboardButton("🛑 Stop Only", callback_data="confirm:shutdown:stop"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])
    await update.message.reply_text("⚠️ Shutdown engine. Choose action:", reply_markup=keyboard)


async def cmd_reset_breaker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    await update.message.reply_text(
        "⚠️ Reset circuit breaker and re-enable arb. Confirm?",
        reply_markup=_confirm_kb("reset_breaker"),
    )


async def cmd_recover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /recover <opp_id>")
        return
    opp_id = context.args[0]
    await update.message.reply_text(
        f"⚠️ Recover half-open arb `{opp_id}`.\n"
        f"Will retry sell if sell-side has cNGN, otherwise reverse the buy to recover capital.",
        reply_markup=_confirm_kb(f"recover:{opp_id}"),
        parse_mode="Markdown",
    )


# --- Callback handler ---

async def _do_withdraw(venue: str) -> str:
    from engine.venues.dex.lp_v4 import V4LPAdapter
    if venue == "all":
        targets = {k: v for k, v in _venues.items() if isinstance(v, V4LPAdapter)}
    elif venue in _venues and isinstance(_venues[venue], V4LPAdapter):
        targets = {venue: _venues[venue]}
    else:
        return f"❌ Venue {venue} not found or not a DEX."
    results = []
    for name, adapter in targets.items():
        for token_id in adapter.get_owned_positions():
            result = await adapter.remove_position(token_id)
            results.append(f"{name}#{token_id}: {result.status}")
    return ("✅ Withdrawn:\n" + "\n".join(results)) if results else f"ℹ️ No positions on {venue}."


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _settings or str(query.message.chat.id) != str(_settings.telegram_chat_id):
        await query.answer()
        return
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
    elif data == "confirm:pause":
        await _scheduler.pause()
        await query.edit_message_text("⏸ Trading paused.")
    elif data == "confirm:resume":
        await _scheduler.resume()
        await query.edit_message_text("▶️ Trading resumed.")
    elif data.startswith("confirm:withdraw:"):
        venue = data.split(":", 2)[2]
        await query.edit_message_text(f"⏳ Withdrawing {venue}...")
        msg = await _do_withdraw(venue)
        await query.message.reply_text(msg)
    elif data == "confirm:shutdown:unwind":
        await query.edit_message_text("⏳ Unwinding positions and stopping...")
        msg = await _do_withdraw("all")
        await query.message.reply_text(f"{msg}\n🛑 Shutting down.")
        asyncio.get_event_loop().call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    elif data == "confirm:shutdown:stop":
        await query.edit_message_text("🛑 Engine shutting down.")
        asyncio.get_event_loop().call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    elif data == "confirm:reset_breaker":
        if _arbitrage_engine:
            _arbitrage_engine.reset_circuit_breaker()
            await query.edit_message_text("✅ Circuit breaker reset.")
        else:
            await query.edit_message_text("❌ Arbitrage engine not configured.")
    elif data.startswith("confirm:recover:"):
        opp_id = data.split(":", 2)[2]
        if not _arbitrage_engine:
            await query.edit_message_text("❌ Arbitrage engine not configured.")
            return
        await query.edit_message_text(f"⏳ Retrying sell leg for {opp_id}...")
        try:
            result = await _arbitrage_engine.recover_dex_half_open(opp_id)
            method = "sell retried" if result["method"] == "retry_sell" else "buy reversed"
            profit = result["profit_usd"]
            sign = "+" if profit >= 0 else ""
            await query.message.reply_text(
                f"✅ Recovered ({method}): tx {result['sell_tx_hash']}, P&L {sign}${profit:.2f}"
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Recovery failed: {e}")


# --- Alert forwarding ---

async def forward_alert(event: dict) -> None:
    if not _app or not _settings or not _settings.telegram_chat_id:
        return
    severity = event.get("severity", "")
    if severity not in ("critical", "warning"):
        return
    icon = "🚨" if severity == "critical" else "⚠️"
    try:
        await _app.bot.send_message(_settings.telegram_chat_id, f"{icon} {event.get('message', '')}")
    except Exception as e:
        logger.warning("telegram_alert_failed", error=str(e))


# --- Lifecycle ---

async def start(s, sched, ven, arb_engine, acct_manager, token_contracts) -> None:
    global _app, _settings, _scheduler, _venues, _arbitrage_engine, _account_manager, _token_contracts
    _settings = s
    _scheduler = sched
    _venues = ven
    _arbitrage_engine = arb_engine
    _account_manager = acct_manager
    _token_contracts = token_contracts

    _app = Application.builder().token(s.telegram_bot_token).build()
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
    await _app.updater.start_polling(drop_pending_updates=True)
    logger.warning("telegram_bot_started")


async def stop() -> None:
    global _app
    if _app:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        _app = None
        logger.info("telegram_bot_stopped")
