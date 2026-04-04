"""Shared helpers for portfolio exposure endpoints and jobs."""

from __future__ import annotations

from engine.api.schemas import GlobalPosition, PortfolioExposure, PortfolioExposureSource
from engine.market.portfolio_exposure import (
    PortfolioExposureCalculator,
    PortfolioExposureSnapshot,
    PortfolioExposureSourceSnapshot,
)
from engine.runtime import EngineRuntime


def get_portfolio_exposure_calculator(runtime: EngineRuntime) -> PortfolioExposureCalculator:
    """Return the shared portfolio exposure calculator, creating it lazily when needed."""
    if runtime.portfolio_exposure_calculator is None:
        runtime.portfolio_exposure_calculator = PortfolioExposureCalculator(
            venues=runtime.venues,
            account_manager=runtime.account_manager,
            token_contracts=runtime.token_contracts,
            blended_calculator=runtime.blended_calculator,
            portfolio_source_registry=runtime.portfolio_source_registry,
        )
    return runtime.portfolio_exposure_calculator


def to_portfolio_exposure_response(snapshot: PortfolioExposureSnapshot) -> PortfolioExposure:
    """Map the internal snapshot into the public API response shape."""
    return PortfolioExposure(
        total_cngn=snapshot.total_cngn,
        total_usdt=snapshot.total_usdt,
        total_usdc=snapshot.total_usdc,
        total_usd_value=snapshot.total_usd_value,
        delta_ratio=snapshot.delta_ratio,
        target_delta=snapshot.target_delta,
        sources=[to_portfolio_exposure_source_response(source) for source in snapshot.sources],
    )


def to_global_position_response(snapshot: PortfolioExposureSnapshot) -> GlobalPosition:
    """Map the internal snapshot into the legacy global-position response shape."""
    return GlobalPosition(
        total_cngn=snapshot.total_cngn,
        total_usdt=snapshot.total_usdt,
        total_usdc=snapshot.total_usdc,
        total_usd_value=snapshot.total_usd_value,
        delta_ratio=snapshot.delta_ratio,
        target_delta=snapshot.target_delta,
    )


def to_portfolio_exposure_source_response(
    source: PortfolioExposureSourceSnapshot,
) -> PortfolioExposureSource:
    """Map one internal source snapshot into the public API shape."""
    return PortfolioExposureSource(
        source=source.source,
        kind=source.kind,
        balances=source.balances,
        usd_value=source.usd_value,
    )
