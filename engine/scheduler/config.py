"""Scheduler configuration."""

from __future__ import annotations

from dataclasses import dataclass

from engine.config import settings


@dataclass
class SchedulerConfig:
    """Configuration for scheduler intervals and thresholds."""

    dex_arb_curve_interval: int = 10
    price_update_interval: int = settings.price_update_interval
    position_sync_interval: int = settings.position_sync_interval
    dex_check_interval: int = settings.dex_check_interval
    cex_sync_interval: int = settings.cex_sync_interval
    arbitrage_scan_interval: int = settings.arbitrage_scan_interval
    balance_check_interval: int = settings.balance_check_interval
    portfolio_refresh_interval: int = settings.portfolio_refresh_interval
