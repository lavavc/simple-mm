"""Core simulation loop for concentrated LP backtesting."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from engine.math.v3 import tick_to_sqrt_price, align_tick
from backtester.data import SwapEvent, MintEvent, BurnEvent, Event
from backtester.params import BacktestParams
from backtester.pool_state import PoolState
from backtester.strategy import EWMACalculator, calculate_tick_range


# -- Pool config (minimal, for backtest only) --

@dataclass
class PoolConfig:
    pool_address: str
    token0_decimals: int
    token1_decimals: int
    tick_spacing: int
    fee_rate: float  # e.g. 0.0005 for 5 bps
    blockchain: str
    invert_price: bool = False  # True when cNGN is token1 (need to invert for tick math)


AERODROME_POOL = PoolConfig(
    pool_address="0x0206B696a410277eF692024C2B64CcF4EaC78589",
    token0_decimals=6,
    token1_decimals=6,
    tick_spacing=10,
    fee_rate=0.0005,
    blockchain="base",
    invert_price=False,  # token0=cNGN, token1=USDC → native price ≈ USDC/cNGN ≈ 0.0007
)

PANCAKESWAP_POOL = PoolConfig(
    pool_address="0xb84e7c912a1034ad674bba8859fca84f1f614a29",
    token0_decimals=18,
    token1_decimals=6,
    tick_spacing=1,
    fee_rate=0.0001,
    blockchain="bnb",
    invert_price=True,  # token0=USDT, token1=cNGN → native price ≈ cNGN/USDT ≈ 1537
)

# Precomputed log constant
_LOG_1_0001 = math.log(1.0001)


def _fast_price_to_tick(price: float, dec0: int, dec1: int) -> int:
    """Float-only price → tick (no Decimal overhead)."""
    adjusted = price / (10 ** (dec0 - dec1))
    if adjusted <= 0:
        return -887272  # MIN_TICK
    return int(math.log(adjusted) / _LOG_1_0001)


def _fast_tick_to_price(tick: int, dec0: int, dec1: int) -> float:
    """Float-only tick → price."""
    return math.pow(1.0001, tick) * (10 ** (dec0 - dec1))


def _cngn_to_native_price(cngn_usd: float, pc: PoolConfig) -> float:
    """Convert cNGN/USD price to pool's native V3 price direction."""
    if pc.invert_price:
        return 1.0 / cngn_usd if cngn_usd > 0 else 0.0
    return cngn_usd


# -- Virtual position math --

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

    def value_at_usd(
        self, current_tick: int, cngn_usd_price: float, dec0: int, dec1: int
    ) -> float:
        """USD value of position using raw L and raw sqrt prices."""
        sp = tick_to_sqrt_price(current_tick)
        spa = tick_to_sqrt_price(self.tick_lower)
        spb = tick_to_sqrt_price(self.tick_upper)
        sp = max(spa, min(sp, spb))

        x_raw = self.liquidity_L * (spb - sp) / (sp * spb) if sp * spb > 0 else 0.0
        y_raw = self.liquidity_L * (sp - spa)
        x_raw = max(x_raw, 0.0)
        y_raw = max(y_raw, 0.0)

        # Convert raw amounts to USD
        if dec0 == dec1:
            # Aerodrome: token0=cNGN, token1=USDC
            x_usd = (x_raw / 10**dec0) * cngn_usd_price
            y_usd = y_raw / 10**dec1
        else:
            # PancakeSwap: token0=USDT, token1=cNGN
            x_usd = x_raw / 10**dec0
            y_usd = (y_raw / 10**dec1) * cngn_usd_price

        return x_usd + y_usd + self.accrued_fees


def compute_liquidity_raw(
    capital_usd: float,
    cngn_usd_price: float,
    tick_lower: int,
    tick_upper: int,
    tick_current: int,
    token0_decimals: int,
    token1_decimals: int,
) -> float:
    """Compute L in raw on-chain units from USD capital.

    Uses raw √P (from ticks, no decimal adjustment) so L matches pool's L.
    """
    # Raw sqrt prices (no decimal adjustment — matches on-chain sqrtPriceX96 units)
    sp = tick_to_sqrt_price(tick_current)
    spa = tick_to_sqrt_price(tick_lower)
    spb = tick_to_sqrt_price(tick_upper)
    sp = max(spa, min(sp, spb))

    # Convert USD capital to raw token amounts
    # token0 and token1 raw amounts per unit L
    x_per_l = (spb - sp) / (sp * spb) if sp * spb > 0 else 0.0  # token0 raw per L
    y_per_l = sp - spa  # token1 raw per L

    # Cost per unit L in USD:
    # For Aerodrome: token0=cNGN(6dec), token1=USDC(6dec)
    #   x tokens in USD = (x_per_l / 10^dec0) * cngn_usd_price
    #   y tokens in USD = (y_per_l / 10^dec1) * usdc_price ≈ y_per_l / 10^dec1
    # For PancakeSwap: token0=USDT(18dec), token1=cNGN(6dec)
    #   x tokens in USD = (x_per_l / 10^dec0) * usdt_price ≈ x_per_l / 10^dec0
    #   y tokens in USD = (y_per_l / 10^dec1) * cngn_usd_price

    # Determine which token is cNGN vs stablecoin
    # Convention: cNGN price is in USD (≈0.0007), stablecoin ≈ $1
    # Aerodrome: token0=cNGN, token1=USDC → x cost = cngn_price, y cost = 1.0
    # PancakeSwap: token0=USDT, token1=cNGN → x cost = 1.0, y cost = cngn_price
    if token0_decimals == token1_decimals:
        # Aerodrome: both 6 dec, token0=cNGN, token1=USDC
        x_usd = x_per_l / (10 ** token0_decimals) * cngn_usd_price
        y_usd = y_per_l / (10 ** token1_decimals)  # USDC ≈ $1
    else:
        # PancakeSwap: token0=USDT(18), token1=cNGN(6)
        x_usd = x_per_l / (10 ** token0_decimals)  # USDT ≈ $1
        y_usd = y_per_l / (10 ** token1_decimals) * cngn_usd_price

    cost_per_l = x_usd + y_usd
    if cost_per_l <= 0:
        return 0.0
    return capital_usd / cost_per_l


# -- Simulation result --

@dataclass
class SimResult:
    daily_returns: list[float] = field(default_factory=list)
    total_fees: float = 0.0
    total_rebalance_cost: float = 0.0
    total_swaps: int = 0
    in_range_swaps: int = 0
    rebalance_count: int = 0
    final_value: float = 0.0


# -- Main loop --

def simulate_pool(
    events: list[Event],
    params: BacktestParams,
    pool_config: PoolConfig,
    initial_capital_usd: float = 5000.0,
) -> SimResult:
    """Run one backtest for a single parameter set on one pool."""
    ewma = EWMACalculator(params.ewma_lambda)
    pool_state = PoolState()
    position: VirtualPosition | None = None
    result = SimResult()

    capital = initial_capital_usd
    current_price = 0.0
    dec0, dec1 = pool_config.token0_decimals, pool_config.token1_decimals

    # Daily tracking
    day_start_value = initial_capital_usd
    current_day: date | None = None

    for event in events:
        if isinstance(event, MintEvent):
            pool_state.apply_mint(event.tick_lower, event.tick_upper, event.liquidity_delta)
            continue
        if isinstance(event, BurnEvent):
            pool_state.apply_burn(event.tick_lower, event.tick_upper, event.liquidity_delta)
            continue

        assert isinstance(event, SwapEvent)
        result.total_swaps += 1
        current_price = event.cngn_usd_price
        if current_price <= 0:
            continue

        native_price = _cngn_to_native_price(current_price, pool_config)
        ewma.update(native_price)  # EWMA tracks pool-native price for tick range calc
        current_tick = _fast_price_to_tick(native_price, dec0, dec1)

        # Day boundary snapshot
        event_day = event.block_time.date()
        if current_day is not None and event_day != current_day:
            current_val = _portfolio_value(position, capital, current_price, current_tick, dec0, dec1)
            if day_start_value > 0:
                result.daily_returns.append(current_val / day_start_value - 1.0)
            day_start_value = current_val
        current_day = event_day

        # Fee accrual + rebalance
        if position is not None:
            if position.is_in_range(current_tick):
                result.in_range_swaps += 1
                L_active = pool_state.get_active_liquidity(current_tick)
                if L_active > 0:
                    fee = event.amount_usd * pool_config.fee_rate * (position.liquidity_L / L_active)
                    position.accrued_fees += fee
                    result.total_fees += fee

            should_rebalance = False
            if not position.is_in_range(current_tick):
                tick_range = position.tick_upper - position.tick_lower
                if current_tick < position.tick_lower:
                    distance = position.tick_lower - current_tick
                else:
                    distance = current_tick - position.tick_upper
                if tick_range > 0 and (distance / tick_range * 100) >= params.rebalance_threshold_pct:
                    should_rebalance = True
            elif params.preemptive_rebalance:
                tick_range = position.tick_upper - position.tick_lower
                margin = tick_range * params.rebalance_threshold_pct / 100
                if (current_tick - position.tick_lower < margin or
                        position.tick_upper - current_tick < margin):
                    should_rebalance = True

            if should_rebalance and ewma.ready:
                pos_value = position.value_at_usd(current_tick, current_price, dec0, dec1)
                rebalance_cost = params.gas_cost_usd + _swap_cost_approx(
                    pos_value, pool_state, pool_config, current_tick
                )
                capital = pos_value - rebalance_cost
                result.total_rebalance_cost += rebalance_cost
                result.rebalance_count += 1
                position = None

        # Open position
        if position is None and ewma.ready and capital > params.gas_cost_usd:
            tl, tu = calculate_tick_range(
                ewma,
                params.sd_multiplier,
                params.downside_skew,
                dec0, dec1,
                pool_config.tick_spacing,
                params.min_tick_width,
                params.max_tick_width,
            )
            L = compute_liquidity_raw(
                capital, current_price, tl, tu, current_tick, dec0, dec1
            )
            if L > 0:
                position = VirtualPosition(
                    tick_lower=tl,
                    tick_upper=tu,
                    liquidity_L=L,
                    entry_price=current_price,
                    entry_value=capital,
                )
                capital = 0.0

    # Final valuation
    result.final_value = _portfolio_value(position, capital, current_price, current_tick, dec0, dec1)
    if day_start_value > 0 and result.final_value > 0:
        result.daily_returns.append(result.final_value / day_start_value - 1.0)

    return result


# -- helpers --

def _portfolio_value(
    pos: VirtualPosition | None, cash: float, cngn_price: float,
    current_tick: int, dec0: int, dec1: int,
) -> float:
    if pos is None:
        return cash
    return pos.value_at_usd(current_tick, cngn_price, dec0, dec1) + cash


def _swap_cost_approx(
    pos_value_usd: float,
    pool_state: PoolState,
    pool_config: PoolConfig,
    current_tick: int,
) -> float:
    """Approximate rebalance swap cost (fee + price impact)."""
    if pos_value_usd <= 0:
        return 0.0

    swap_usd = pos_value_usd * 0.3  # ~30% needs swapping for ratio adjustment
    fee_cost = swap_usd * pool_config.fee_rate

    L_active = pool_state.get_active_liquidity(current_tick)
    if L_active <= 0:
        return fee_cost + swap_usd * 0.02  # 2% fallback

    # Quadratic price impact: Δ²/(2LP)
    sqrt_p = tick_to_sqrt_price(current_tick)
    spot_p = sqrt_p ** 2
    if spot_p <= 0:
        return fee_cost + swap_usd * 0.02

    impact = (swap_usd ** 2) / (2 * L_active * spot_p)
    return fee_cost + impact
