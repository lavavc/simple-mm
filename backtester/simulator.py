"""Core simulation loop for concentrated LP backtesting."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from backtester.data import BurnEvent, Event, MintEvent, SwapEvent, V4Event
from backtester.params import BacktestParams
from backtester.pool_state import PoolState
from backtester.strategy import EWMACalculator, calculate_tick_range
from engine.math.v3 import tick_to_sqrt_price
from engine.venues.dex.uniswap_base import UNISWAP_BASE_EXECUTION_CONFIG
from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_EXECUTION_CONFIG


@dataclass(frozen=True)
class PoolConfig:
    name: str
    pool_address: str
    token0_decimals: int
    token1_decimals: int
    tick_spacing: int
    fee_rate: float
    blockchain: str
    invert_price: bool = False
    cngn_is_token0: bool = True


AERODROME_POOL = PoolConfig(
    name="aerodrome",
    pool_address="0x0206B696a410277eF692024C2B64CcF4EaC78589",
    token0_decimals=6,
    token1_decimals=6,
    tick_spacing=10,
    fee_rate=0.0005,
    blockchain="base",
    invert_price=False,
    cngn_is_token0=True,
)

PANCAKESWAP_POOL = PoolConfig(
    name="pancakeswap",
    pool_address="0xb84e7c912a1034ad674bba8859fca84f1f614a29",
    token0_decimals=18,
    token1_decimals=6,
    tick_spacing=1,
    fee_rate=0.0001,
    blockchain="bnb",
    invert_price=True,
    cngn_is_token0=False,
)

UNISWAP_BASE_POOL = PoolConfig(
    name="uni-base",
    pool_address=UNISWAP_BASE_EXECUTION_CONFIG.pool_id,
    token0_decimals=UNISWAP_BASE_EXECUTION_CONFIG.token0_decimals,
    token1_decimals=UNISWAP_BASE_EXECUTION_CONFIG.token1_decimals,
    tick_spacing=UNISWAP_BASE_EXECUTION_CONFIG.tick_spacing,
    fee_rate=UNISWAP_BASE_EXECUTION_CONFIG.fee / 1_000_000,
    blockchain=UNISWAP_BASE_EXECUTION_CONFIG.chain_name,
    invert_price=UNISWAP_BASE_EXECUTION_CONFIG.invert_price,
    cngn_is_token0=UNISWAP_BASE_EXECUTION_CONFIG.cngn_is_token0,
)

UNISWAP_BSC_POOL = PoolConfig(
    name="uni-bsc",
    pool_address=UNISWAP_BSC_EXECUTION_CONFIG.pool_id,
    token0_decimals=UNISWAP_BSC_EXECUTION_CONFIG.token0_decimals,
    token1_decimals=UNISWAP_BSC_EXECUTION_CONFIG.token1_decimals,
    tick_spacing=UNISWAP_BSC_EXECUTION_CONFIG.tick_spacing,
    fee_rate=UNISWAP_BSC_EXECUTION_CONFIG.fee / 1_000_000,
    blockchain=UNISWAP_BSC_EXECUTION_CONFIG.chain_name,
    invert_price=UNISWAP_BSC_EXECUTION_CONFIG.invert_price,
    cngn_is_token0=False,
)

_LOG_1_0001 = math.log(1.0001)


def _fast_price_to_tick(price: float, dec0: int, dec1: int) -> int:
    adjusted = price / (10 ** (dec0 - dec1))
    if adjusted <= 0:
        return -887272
    return int(math.log(adjusted) / _LOG_1_0001)


def _cngn_to_native_price(cngn_usd: float, pool_config: PoolConfig) -> float:
    if pool_config.invert_price:
        return 1.0 / cngn_usd if cngn_usd > 0 else 0.0
    return cngn_usd


@dataclass
class VirtualPosition:
    tick_lower: int
    tick_upper: int
    liquidity_L: float
    entry_price: float
    entry_value: float
    accrued_fees: float = 0.0

    def is_in_range(self, current_tick: int) -> bool:
        return self.tick_lower <= current_tick < self.tick_upper

    def amounts_at_tick(self, current_tick: int) -> tuple[float, float]:
        sp = tick_to_sqrt_price(current_tick)
        spa = tick_to_sqrt_price(self.tick_lower)
        spb = tick_to_sqrt_price(self.tick_upper)
        sp = max(spa, min(sp, spb))
        amount0 = self.liquidity_L * (spb - sp) / (sp * spb) if sp * spb > 0 else 0.0
        amount1 = self.liquidity_L * (sp - spa)
        return max(amount0, 0.0), max(amount1, 0.0)

    def value_at_usd(self, current_tick: int, cngn_usd_price: float, pool_config: PoolConfig) -> float:
        amount0, amount1 = self.amounts_at_tick(current_tick)
        usd0, usd1 = _raw_amounts_to_usd(amount0, amount1, cngn_usd_price, pool_config)
        return usd0 + usd1 + self.accrued_fees


def _raw_amounts_to_usd(amount0: float, amount1: float, cngn_usd_price: float, pool_config: PoolConfig) -> tuple[float, float]:
    if pool_config.cngn_is_token0:
        usd0 = (amount0 / 10 ** pool_config.token0_decimals) * cngn_usd_price
        usd1 = amount1 / 10 ** pool_config.token1_decimals
    else:
        usd0 = amount0 / 10 ** pool_config.token0_decimals
        usd1 = (amount1 / 10 ** pool_config.token1_decimals) * cngn_usd_price
    return usd0, usd1


def compute_liquidity_raw(
    capital_usd: float,
    cngn_usd_price: float,
    tick_lower: int,
    tick_upper: int,
    tick_current: int,
    pool_config: PoolConfig,
) -> float:
    sp = tick_to_sqrt_price(tick_current)
    spa = tick_to_sqrt_price(tick_lower)
    spb = tick_to_sqrt_price(tick_upper)
    sp = max(spa, min(sp, spb))
    amount0_per_l = (spb - sp) / (sp * spb) if sp * spb > 0 else 0.0
    amount1_per_l = sp - spa
    usd0, usd1 = _raw_amounts_to_usd(amount0_per_l, amount1_per_l, cngn_usd_price, pool_config)
    cost_per_l = usd0 + usd1
    if cost_per_l <= 0:
        return 0.0
    return capital_usd / cost_per_l


@dataclass
class PortfolioComposition:
    stable_usd: float
    cngn_amount: float


@dataclass
class SimResult:
    daily_returns: list[float] = field(default_factory=list)
    total_fees: float = 0.0
    total_rebalance_cost: float = 0.0
    total_swaps: int = 0
    in_range_swaps: int = 0
    rebalance_count: int = 0
    final_value: float = 0.0
    divergent_loss: float = 0.0


def _snapshot_composition(
    position: VirtualPosition | None,
    cash_usd: float,
    current_tick: int,
    cngn_usd_price: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    if position is None:
        return PortfolioComposition(stable_usd=max(cash_usd, 0.0), cngn_amount=0.0)

    amount0, amount1 = position.amounts_at_tick(current_tick)
    fee_stable = position.accrued_fees
    if pool_config.cngn_is_token0:
        cngn_amount = amount0 / 10 ** pool_config.token0_decimals
        stable_amount = amount1 / 10 ** pool_config.token1_decimals
    else:
        stable_amount = amount0 / 10 ** pool_config.token0_decimals
        cngn_amount = amount1 / 10 ** pool_config.token1_decimals
    return PortfolioComposition(stable_usd=stable_amount + cash_usd + fee_stable, cngn_amount=cngn_amount)


def _portfolio_value(
    position: VirtualPosition | None,
    cash: float,
    cngn_price: float,
    current_tick: int,
    pool_config: PoolConfig,
) -> float:
    if position is None:
        return cash
    return position.value_at_usd(current_tick, cngn_price, pool_config) + cash


def _event_pool_fee_rate(event: Event, pool_config: PoolConfig) -> float:
    if isinstance(event, V4Event) and event.fee_rate > 0:
        return event.fee_rate
    return pool_config.fee_rate


def _event_liquidity(event: Event, pool_state: PoolState, current_tick: int) -> int:
    if isinstance(event, V4Event) and event.active_liquidity > 0:
        return event.active_liquidity
    return pool_state.get_active_liquidity(current_tick)


def simulate_pool(
    events: list[Event],
    params: BacktestParams,
    pool_config: PoolConfig,
    initial_capital_usd: float = 5000.0,
    initial_pool_state: PoolState | None = None,
) -> SimResult:
    ewma = EWMACalculator(params.ewma_lambda)
    pool_state = initial_pool_state.copy() if initial_pool_state is not None else PoolState()
    position: VirtualPosition | None = None
    result = SimResult()
    capital = initial_capital_usd
    current_price = 0.0
    current_tick = 0
    day_start_value = initial_capital_usd
    current_day: date | None = None
    validation_start: PortfolioComposition | None = None

    for event in events:
        if isinstance(event, MintEvent):
            pool_state.apply_mint(event.tick_lower, event.tick_upper, event.liquidity_delta)
            continue
        if isinstance(event, BurnEvent):
            pool_state.apply_burn(event.tick_lower, event.tick_upper, event.liquidity_delta)
            continue
        if isinstance(event, V4Event) and event.event_type == "mint":
            # Approximate mint/burn updates from event-local active liquidity only is lossy.
            # Preserve deterministic stream replay when explicit liquidity deltas are unavailable.
            continue
        if isinstance(event, V4Event) and event.event_type == "burn":
            continue

        assert isinstance(event, (SwapEvent, V4Event))
        result.total_swaps += 1
        current_price = event.cngn_usd_price
        if current_price <= 0:
            continue

        native_price = _cngn_to_native_price(current_price, pool_config)
        ewma.update(native_price)
        current_tick = event.tick if isinstance(event, V4Event) else _fast_price_to_tick(
            native_price, pool_config.token0_decimals, pool_config.token1_decimals
        )

        if validation_start is None:
            validation_start = _snapshot_composition(position, capital, current_tick, current_price, pool_config)

        event_day = event.block_time.date()
        if current_day is not None and event_day != current_day:
            current_val = _portfolio_value(position, capital, current_price, current_tick, pool_config)
            if day_start_value > 0:
                result.daily_returns.append(current_val / day_start_value - 1.0)
            day_start_value = current_val
        current_day = event_day

        if position is not None:
            if position.is_in_range(current_tick):
                result.in_range_swaps += 1
                active_liquidity = _event_liquidity(event, pool_state, current_tick)
                if active_liquidity > 0:
                    fee = event.amount_usd * _event_pool_fee_rate(event, pool_config) * (position.liquidity_L / active_liquidity)
                    position.accrued_fees += fee
                    result.total_fees += fee

            should_rebalance = False
            tick_range = position.tick_upper - position.tick_lower
            if not position.is_in_range(current_tick):
                distance = position.tick_lower - current_tick if current_tick < position.tick_lower else current_tick - position.tick_upper
                if tick_range > 0 and (distance / tick_range * 100) >= params.rebalance_threshold_pct:
                    should_rebalance = True
            elif params.preemptive_rebalance:
                margin = tick_range * params.rebalance_threshold_pct / 100
                if current_tick - position.tick_lower < margin or position.tick_upper - current_tick < margin:
                    should_rebalance = True

            if should_rebalance and ewma.ready:
                position_value = position.value_at_usd(current_tick, current_price, pool_config)
                rebalance_cost = params.gas_cost_usd + _swap_cost_approx(
                    position_value,
                    _event_liquidity(event, pool_state, current_tick),
                    _event_pool_fee_rate(event, pool_config),
                    current_tick,
                )
                capital = position_value - rebalance_cost
                result.total_rebalance_cost += rebalance_cost
                result.rebalance_count += 1
                position = None

        if position is None and ewma.ready and capital > params.gas_cost_usd:
            tick_lower, tick_upper = calculate_tick_range(
                ewma,
                params.sd_multiplier,
                params.downside_skew,
                pool_config.token0_decimals,
                pool_config.token1_decimals,
                pool_config.tick_spacing,
                params.min_tick_width,
                params.max_tick_width,
            )
            liquidity = compute_liquidity_raw(
                capital, current_price, tick_lower, tick_upper, current_tick, pool_config
            )
            if liquidity > 0:
                position = VirtualPosition(
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    liquidity_L=liquidity,
                    entry_price=current_price,
                    entry_value=capital,
                )
                capital = 0.0

    result.final_value = _portfolio_value(position, capital, current_price, current_tick, pool_config)
    if day_start_value > 0 and result.final_value > 0:
        result.daily_returns.append(result.final_value / day_start_value - 1.0)

    if validation_start is None:
        validation_start = PortfolioComposition(stable_usd=initial_capital_usd, cngn_amount=0.0)
    hodl_end_value = validation_start.stable_usd + validation_start.cngn_amount * current_price
    result.divergent_loss = (result.final_value / hodl_end_value - 1.0) if hodl_end_value > 0 else 0.0
    return result


def _swap_cost_approx(
    position_value_usd: float,
    active_liquidity: int,
    fee_rate: float,
    current_tick: int,
) -> float:
    if position_value_usd <= 0:
        return 0.0
    swap_usd = position_value_usd * 0.3
    fee_cost = swap_usd * fee_rate
    if active_liquidity <= 0:
        return fee_cost + swap_usd * 0.02
    sqrt_p = tick_to_sqrt_price(current_tick)
    spot_p = sqrt_p ** 2
    if spot_p <= 0:
        return fee_cost + swap_usd * 0.02
    impact = (swap_usd ** 2) / (2 * active_liquidity * spot_p)
    return fee_cost + impact
