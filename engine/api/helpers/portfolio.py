"""Shared helpers for portfolio exposure endpoints and jobs."""

from __future__ import annotations

from engine.market.portfolio_exposure import PortfolioExposureCalculator
from engine.runtime import EngineRuntime


def get_portfolio_exposure_calculator(runtime: EngineRuntime) -> PortfolioExposureCalculator:
    """Return the shared portfolio exposure calculator, creating it lazily when needed."""
    if runtime.portfolio_exposure_calculator is None:
        runtime.portfolio_exposure_calculator = PortfolioExposureCalculator(
            venues=runtime.venues,
            account_manager=runtime.account_manager,
            token_contracts=runtime.token_contracts,
            blended_calculator=runtime.blended_calculator,
        )
    return runtime.portfolio_exposure_calculator
