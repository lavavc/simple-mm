"""Core simulation loop for concentrated LP backtesting."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backtester.data import BurnEvent, Event, MintEvent, SwapEvent, V4Event
from backtester.clmm_math import (
    cngn_price_from_sqrt_price_x96,
    liquidity_amounts_raw,
    sqrt_price_x96_to_float,
    tick_to_sqrt_price_x96,
)
from backtester.params import BacktestParams, TransactionCostModel
from backtester.pool_state import PoolState
from backtester.strategy import (
    EWMACalculator,
    calculate_fixed_pct_tick_range,
    calculate_fixed_tick_range,
    calculate_tick_range,
)
from engine.math.v3 import compute_swap_step, tick_to_sqrt_price
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


def _native_to_cngn_price(native_price: float, pool_config: PoolConfig) -> float:
    if pool_config.invert_price:
        return 1.0 / native_price if native_price > 0 else 0.0
    return native_price


@dataclass(frozen=True)
class TransactionCostBreakdown:
    action: str
    gas_cost: float = 0.0
    swap_fee_cost: float = 0.0
    price_impact_cost: float = 0.0
    slippage_cost: float = 0.0
    latency_slippage_cost: float = 0.0
    failed_tx_expected_cost: float = 0.0
    swap_notional_usd: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.gas_cost
            + self.swap_fee_cost
            + self.price_impact_cost
            + self.slippage_cost
            + self.latency_slippage_cost
            + self.failed_tx_expected_cost
        )


@dataclass
class VirtualPosition:
    tick_lower: int
    tick_upper: int
    liquidity_L: float
    entry_price: float
    entry_value: float
    entry_time: datetime
    entry_tick: int
    entry_active_liquidity: int
    deployed_capital: float
    entry_transaction_cost: TransactionCostBreakdown = field(default_factory=lambda: TransactionCostBreakdown("enter"))
    accrued_fee_stable: float = 0.0
    accrued_fee_cngn: float = 0.0
    observed_swaps: int = 0
    in_range_swaps: int = 0

    def is_in_range(self, current_tick: int) -> bool:
        return self.tick_lower <= current_tick < self.tick_upper

    def amounts_at_tick(self, current_tick: int) -> tuple[float, float]:
        return self.amounts_at_sqrt_price_x96(tick_to_sqrt_price_x96(current_tick))

    def amounts_at_sqrt_price_x96(self, current_sqrt_price_x96: int) -> tuple[float, float]:
        return liquidity_amounts_raw(
            self.liquidity_L,
            self.tick_lower,
            self.tick_upper,
            current_sqrt_price_x96,
        )

    def value_at_usd(self, current_tick: int, cngn_usd_price: float, pool_config: PoolConfig) -> float:
        amount0, amount1 = self.amounts_at_tick(current_tick)
        usd0, usd1 = _raw_amounts_to_usd(amount0, amount1, cngn_usd_price, pool_config)
        return usd0 + usd1 + self.fee_value_usd(cngn_usd_price)

    def value_at_sqrt_price_x96(
        self,
        current_sqrt_price_x96: int,
        cngn_usd_price: float,
        pool_config: PoolConfig,
    ) -> float:
        amount0, amount1 = self.amounts_at_sqrt_price_x96(current_sqrt_price_x96)
        usd0, usd1 = _raw_amounts_to_usd(amount0, amount1, cngn_usd_price, pool_config)
        return usd0 + usd1 + self.fee_value_usd(cngn_usd_price)

    def fee_value_usd(self, cngn_usd_price: float) -> float:
        return self.accrued_fee_stable + self.accrued_fee_cngn * cngn_usd_price


def _raw_amounts_to_usd(amount0: float, amount1: float, cngn_usd_price: float, pool_config: PoolConfig) -> tuple[float, float]:
    if pool_config.cngn_is_token0:
        usd0 = (amount0 / 10 ** pool_config.token0_decimals) * cngn_usd_price
        usd1 = amount1 / 10 ** pool_config.token1_decimals
    else:
        usd0 = amount0 / 10 ** pool_config.token0_decimals
        usd1 = (amount1 / 10 ** pool_config.token1_decimals) * cngn_usd_price
    return usd0, usd1


def _cngn_notional_usd(amount0: float, amount1: float, cngn_usd_price: float, pool_config: PoolConfig) -> float:
    if pool_config.cngn_is_token0:
        return (amount0 / 10 ** pool_config.token0_decimals) * cngn_usd_price
    return (amount1 / 10 ** pool_config.token1_decimals) * cngn_usd_price


def _token0_usd_price(cngn_usd_price: float, pool_config: PoolConfig) -> float:
    return cngn_usd_price if pool_config.cngn_is_token0 else 1.0


def _token1_usd_price(cngn_usd_price: float, pool_config: PoolConfig) -> float:
    return 1.0 if pool_config.cngn_is_token0 else cngn_usd_price


def _stable_token_decimals(pool_config: PoolConfig) -> int:
    return pool_config.token1_decimals if pool_config.cngn_is_token0 else pool_config.token0_decimals


def _cngn_token_decimals(pool_config: PoolConfig) -> int:
    return pool_config.token0_decimals if pool_config.cngn_is_token0 else pool_config.token1_decimals


def compute_liquidity_raw(
    capital_usd: float,
    cngn_usd_price: float,
    tick_lower: int,
    tick_upper: int,
    tick_current: int,
    pool_config: PoolConfig,
) -> float:
    current_sqrt_price_x96 = tick_to_sqrt_price_x96(tick_current)
    amount0_per_l, amount1_per_l = liquidity_amounts_raw(
        1.0,
        tick_lower,
        tick_upper,
        current_sqrt_price_x96,
    )
    usd0, usd1 = _raw_amounts_to_usd(amount0_per_l, amount1_per_l, cngn_usd_price, pool_config)
    cost_per_l = usd0 + usd1
    if cost_per_l <= 0:
        return 0.0
    return capital_usd / cost_per_l


@dataclass
class PortfolioComposition:
    stable_usd: float
    cngn_amount: float

    def value_usd(self, cngn_usd_price: float) -> float:
        return self.stable_usd + self.cngn_amount * cngn_usd_price


@dataclass
class EpisodeRecord:
    entry_time: datetime
    entry_price: float
    entry_tick: int
    tick_lower: int
    tick_upper: int
    entry_value: float
    exit_reason: str
    exit_time: datetime
    exit_price: float
    exit_tick: int
    exit_value: float
    fees_earned: float
    fee_stable: float
    fee_cngn: float
    rebalance_cost: float
    entry_transaction_cost: float
    exit_transaction_cost: float
    total_transaction_cost: float
    gas_cost: float
    swap_fee_cost: float
    price_impact_cost: float
    slippage_cost: float
    latency_slippage_cost: float
    failed_tx_expected_cost: float
    swap_notional_usd: float
    inventory_pnl: float
    net_pnl: float
    net_return: float
    range_traversal_fraction: float
    time_in_range: float
    duration_seconds: float
    active_liquidity_share: float


@dataclass
class SimResult:
    daily_returns: list[float] = field(default_factory=list)
    episodes: list[EpisodeRecord] = field(default_factory=list)
    total_fees: float = 0.0
    total_fee_stable: float = 0.0
    total_fee_cngn: float = 0.0
    total_rebalance_cost: float = 0.0
    total_transaction_cost: float = 0.0
    total_entry_cost: float = 0.0
    total_exit_cost: float = 0.0
    total_gas_cost: float = 0.0
    total_swap_fee_cost: float = 0.0
    total_price_impact_cost: float = 0.0
    total_slippage_cost: float = 0.0
    total_latency_slippage_cost: float = 0.0
    total_failed_tx_expected_cost: float = 0.0
    total_swaps: int = 0
    in_range_swaps: int = 0
    rebalance_count: int = 0
    final_value: float = 0.0
    divergent_loss: float = 0.0
    start_time: datetime | None = None
    end_time: datetime | None = None


def _snapshot_composition(
    position: VirtualPosition | None,
    wallet: PortfolioComposition,
    current_tick: int,
    current_sqrt_price_x96: int,
    cngn_usd_price: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    if position is None:
        return PortfolioComposition(stable_usd=max(wallet.stable_usd, 0.0), cngn_amount=max(wallet.cngn_amount, 0.0))

    amount0, amount1 = position.amounts_at_sqrt_price_x96(current_sqrt_price_x96)
    position_wallet = _raw_amounts_to_wallet(amount0, amount1, cngn_usd_price, pool_config)
    return PortfolioComposition(
        stable_usd=wallet.stable_usd + position_wallet.stable_usd + position.accrued_fee_stable,
        cngn_amount=wallet.cngn_amount + position_wallet.cngn_amount + position.accrued_fee_cngn,
    )


def _raw_amounts_to_wallet(
    amount0: float,
    amount1: float,
    cngn_usd_price: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    if pool_config.cngn_is_token0:
        return PortfolioComposition(
            stable_usd=amount1 / 10 ** pool_config.token1_decimals,
            cngn_amount=amount0 / 10 ** pool_config.token0_decimals,
        )
    return PortfolioComposition(
        stable_usd=amount0 / 10 ** pool_config.token0_decimals,
        cngn_amount=amount1 / 10 ** pool_config.token1_decimals,
    )


def _wallet_with_position_removed(
    wallet: PortfolioComposition,
    position: VirtualPosition,
    current_tick: int,
    current_sqrt_price_x96: int,
    cngn_usd_price: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    amount0, amount1 = position.amounts_at_sqrt_price_x96(current_sqrt_price_x96)
    removed = _raw_amounts_to_wallet(amount0, amount1, cngn_usd_price, pool_config)
    return PortfolioComposition(
        stable_usd=wallet.stable_usd + removed.stable_usd + position.accrued_fee_stable,
        cngn_amount=wallet.cngn_amount + removed.cngn_amount + position.accrued_fee_cngn,
    )


def _pay_wallet_cost(wallet: PortfolioComposition, cost_usd: float, cngn_usd_price: float) -> PortfolioComposition:
    if cost_usd <= 0:
        return wallet
    stable = wallet.stable_usd
    cngn = wallet.cngn_amount
    if stable >= cost_usd:
        return PortfolioComposition(stable_usd=stable - cost_usd, cngn_amount=cngn)

    shortfall = cost_usd - stable
    cngn_to_sell = shortfall / cngn_usd_price if cngn_usd_price > 0 else 0.0
    return PortfolioComposition(stable_usd=0.0, cngn_amount=max(cngn - cngn_to_sell, 0.0))


def _position_token_values_per_liquidity(
    tick_lower: int,
    tick_upper: int,
    current_tick: int,
    current_sqrt_price_x96: int,
    cngn_usd_price: float,
    pool_config: PoolConfig,
) -> tuple[float, float]:
    amount0_per_l, amount1_per_l = liquidity_amounts_raw(
        1.0,
        tick_lower,
        tick_upper,
        current_sqrt_price_x96,
    )
    usd0, usd1 = _raw_amounts_to_usd(amount0_per_l, amount1_per_l, cngn_usd_price, pool_config)
    if pool_config.cngn_is_token0:
        return usd0, usd1
    return usd1, usd0


def _required_wallet_for_liquidity(
    liquidity: float,
    tick_lower: int,
    tick_upper: int,
    current_tick: int,
    current_sqrt_price_x96: int,
    cngn_usd_price: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    position = VirtualPosition(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_L=liquidity,
        entry_price=cngn_usd_price,
        entry_value=0.0,
        entry_time=datetime.min,
        entry_tick=current_tick,
        entry_active_liquidity=0,
        deployed_capital=0.0,
    )
    amount0, amount1 = position.amounts_at_sqrt_price_x96(current_sqrt_price_x96)
    return _raw_amounts_to_wallet(amount0, amount1, cngn_usd_price, pool_config)


def _subtract_wallet(wallet: PortfolioComposition, required: PortfolioComposition) -> PortfolioComposition:
    return PortfolioComposition(
        stable_usd=max(wallet.stable_usd - required.stable_usd, 0.0),
        cngn_amount=max(wallet.cngn_amount - required.cngn_amount, 0.0),
    )


def _apply_inventory_swap(
    wallet: PortfolioComposition,
    direction: str | None,
    notional_usd: float,
    variable_cost_usd: float,
    cngn_usd_price: float,
    *,
    exact_output: bool = True,
) -> PortfolioComposition:
    if direction is None or notional_usd <= 0 or cngn_usd_price <= 0:
        return wallet
    if not exact_output:
        if direction == "stable_to_cngn":
            return PortfolioComposition(
                stable_usd=max(wallet.stable_usd - notional_usd, 0.0),
                cngn_amount=wallet.cngn_amount + max(notional_usd - variable_cost_usd, 0.0) / cngn_usd_price,
            )
        return PortfolioComposition(
            stable_usd=wallet.stable_usd + max(notional_usd - variable_cost_usd, 0.0),
            cngn_amount=max(wallet.cngn_amount - notional_usd / cngn_usd_price, 0.0),
        )

    if direction == "stable_to_cngn":
        return PortfolioComposition(
            stable_usd=max(wallet.stable_usd - notional_usd - variable_cost_usd, 0.0),
            cngn_amount=wallet.cngn_amount + notional_usd / cngn_usd_price,
        )
    return PortfolioComposition(
        stable_usd=wallet.stable_usd + notional_usd,
        cngn_amount=max(wallet.cngn_amount - (notional_usd + variable_cost_usd) / cngn_usd_price, 0.0),
    )


def _inventory_delta_swap(
    wallet: PortfolioComposition,
    required: PortfolioComposition,
    cngn_usd_price: float,
) -> tuple[str | None, float]:
    wallet_cngn_usd = wallet.cngn_amount * cngn_usd_price
    required_cngn_usd = required.cngn_amount * cngn_usd_price
    if wallet_cngn_usd + 1e-12 < required_cngn_usd:
        return "stable_to_cngn", required_cngn_usd - wallet_cngn_usd
    if wallet.stable_usd + 1e-12 < required.stable_usd:
        return "cngn_to_stable", required.stable_usd - wallet.stable_usd
    return None, 0.0


def _route_wallet_to_position(
    wallet: PortfolioComposition,
    tick_lower: int,
    tick_upper: int,
    current_tick: int,
    current_sqrt_price_x96: int,
    cngn_usd_price: float,
    active_liquidity: int,
    fee_rate: float,
    pool_config: PoolConfig,
    params: BacktestParams,
) -> tuple[float, float, TransactionCostBreakdown, PortfolioComposition]:
    cngn_per_l, stable_per_l = _position_token_values_per_liquidity(
        tick_lower,
        tick_upper,
        current_tick,
        current_sqrt_price_x96,
        cngn_usd_price,
        pool_config,
    )
    value_per_l = cngn_per_l + stable_per_l
    wallet_value = wallet.value_usd(cngn_usd_price)
    if value_per_l <= 0 or wallet_value <= 0:
        return 0.0, 0.0, TransactionCostBreakdown("enter"), wallet

    max_liquidity = None
    if params.max_active_liquidity_share is not None and params.max_active_liquidity_share > 0 and active_liquidity > 0:
        max_liquidity = active_liquidity * params.max_active_liquidity_share

    liquidity = wallet_value / value_per_l
    if max_liquidity is not None:
        liquidity = min(liquidity, max_liquidity)
    entry_cost = TransactionCostBreakdown("enter")
    required = PortfolioComposition(0.0, 0.0)
    direction: str | None = None
    swap_notional_usd = 0.0

    for _ in range(5):
        required = _required_wallet_for_liquidity(
            liquidity,
            tick_lower,
            tick_upper,
            current_tick,
            current_sqrt_price_x96,
            cngn_usd_price,
            pool_config,
        )
        direction, swap_notional_usd = _inventory_delta_swap(wallet, required, cngn_usd_price)
        entry_cost = _swap_cost_breakdown(
            "enter",
            swap_notional_usd,
            active_liquidity,
            fee_rate,
            current_tick,
            params,
            direction=direction,
            current_price=cngn_usd_price,
            current_sqrt_price_x96=current_sqrt_price_x96,
            pool_config=pool_config,
            exact_output=True,
        )
        affordable_value = max(wallet_value - entry_cost.total, 0.0)
        next_liquidity = affordable_value / value_per_l
        if max_liquidity is not None:
            next_liquidity = min(next_liquidity, max_liquidity)
        if abs(next_liquidity - liquidity) <= max(abs(liquidity) * 1e-9, 1e-9):
            liquidity = next_liquidity
            break
        liquidity = next_liquidity

    if liquidity <= 0 or entry_cost.total >= wallet_value:
        return 0.0, 0.0, entry_cost, wallet

    required = _required_wallet_for_liquidity(
        liquidity,
        tick_lower,
        tick_upper,
        current_tick,
        current_sqrt_price_x96,
        cngn_usd_price,
        pool_config,
    )
    direction, swap_notional_usd = _inventory_delta_swap(wallet, required, cngn_usd_price)
    entry_cost = _swap_cost_breakdown(
        "enter",
        swap_notional_usd,
        active_liquidity,
        fee_rate,
        current_tick,
        params,
        direction=direction,
        current_price=cngn_usd_price,
        current_sqrt_price_x96=current_sqrt_price_x96,
        pool_config=pool_config,
        exact_output=True,
    )
    variable_cost = (
        entry_cost.swap_fee_cost
        + entry_cost.price_impact_cost
        + entry_cost.slippage_cost
        + entry_cost.latency_slippage_cost
    )
    routed_wallet = _pay_wallet_cost(wallet, entry_cost.gas_cost + entry_cost.failed_tx_expected_cost, cngn_usd_price)
    routed_wallet = _apply_inventory_swap(
        routed_wallet,
        direction,
        swap_notional_usd,
        variable_cost,
        cngn_usd_price,
        exact_output=True,
    )
    remaining_wallet = _subtract_wallet(routed_wallet, required)
    deployed_capital = required.value_usd(cngn_usd_price)
    return liquidity, deployed_capital, entry_cost, remaining_wallet


def _portfolio_value(
    position: VirtualPosition | None,
    wallet: PortfolioComposition,
    cngn_price: float,
    current_tick: int,
    current_sqrt_price_x96: int,
    pool_config: PoolConfig,
) -> float:
    value = wallet.value_usd(cngn_price)
    if position is None:
        return value
    return position.value_at_sqrt_price_x96(current_sqrt_price_x96, cngn_price, pool_config) + value


def _pay_or_unwind_exit_cost(
    wallet: PortfolioComposition,
    exit_cost: TransactionCostBreakdown,
    cngn_usd_price: float,
) -> PortfolioComposition:
    variable_cost = (
        exit_cost.swap_fee_cost
        + exit_cost.price_impact_cost
        + exit_cost.slippage_cost
        + exit_cost.latency_slippage_cost
    )
    wallet = _pay_wallet_cost(wallet, exit_cost.gas_cost + exit_cost.failed_tx_expected_cost, cngn_usd_price)
    wallet = _apply_inventory_swap(
        wallet,
        "cngn_to_stable" if exit_cost.swap_notional_usd > 0 else None,
        exit_cost.swap_notional_usd,
        variable_cost,
        cngn_usd_price,
        exact_output=False,
    )
    return wallet


def _event_pool_fee_rate(event: Event, pool_config: PoolConfig) -> float:
    if isinstance(event, V4Event) and event.fee_rate > 0:
        return event.fee_rate
    return pool_config.fee_rate


def _event_liquidity(event: Event, pool_state: PoolState, current_tick: int) -> int:
    if isinstance(event, V4Event) and event.active_liquidity > 0:
        return event.active_liquidity
    return pool_state.get_active_liquidity(current_tick)


def _event_block_number(event: Event) -> int | None:
    return event.block_number if isinstance(event, V4Event) else None


def _event_swap_volume_usd(event: Event) -> float:
    return max(float(getattr(event, "amount_usd", 0.0) or 0.0), 0.0)


def _event_fair_price(event: Event) -> float | None:
    fair_price = getattr(event, "fair_price_usd", None)
    return fair_price if fair_price and fair_price > 0 else None


def _event_cngn_price(event: Event, pool_config: PoolConfig) -> float:
    if isinstance(event, V4Event):
        return cngn_price_from_sqrt_price_x96(
            event.sqrt_price_x96,
            pool_config.token0_decimals,
            pool_config.token1_decimals,
            pool_config.invert_price,
        )
    return event.cngn_usd_price


def _event_sqrt_price_x96(event: Event, current_tick: int) -> int:
    if isinstance(event, V4Event):
        return event.sqrt_price_x96
    return tick_to_sqrt_price_x96(current_tick)


def _tick_from_cngn_price(price: float, pool_config: PoolConfig) -> int:
    native_price = _cngn_to_native_price(price, pool_config)
    return _fast_price_to_tick(native_price, pool_config.token0_decimals, pool_config.token1_decimals)


def _qualified_pool_twap(
    observations: list[tuple[datetime, float]],
    now: datetime,
    lookback_minutes: float,
) -> float | None:
    if not observations:
        return None
    if lookback_minutes <= 0:
        return observations[-1][1]
    window_start = now - timedelta(minutes=lookback_minutes)
    relevant = [(ts, price) for ts, price in observations if ts >= window_start]
    if not relevant:
        return observations[-1][1]
    if len(relevant) == 1:
        return relevant[0][1]

    weighted_sum = 0.0
    total_seconds = 0.0
    for idx, (ts, price) in enumerate(relevant):
        next_ts = relevant[idx + 1][0] if idx + 1 < len(relevant) else now
        interval_start = max(ts, window_start)
        seconds = max((next_ts - interval_start).total_seconds(), 0.0)
        weighted_sum += price * seconds
        total_seconds += seconds
    return weighted_sum / total_seconds if total_seconds > 0 else relevant[-1][1]


def _defensive_exit_mark(
    event: Event,
    current_price: float,
    current_tick: int,
    pool_config: PoolConfig,
    params: BacktestParams,
    qualified_price_observations: list[tuple[datetime, float]],
) -> tuple[float, int]:
    if params.exit_price_mode == "fair_price":
        mark_price = _event_fair_price(event) or current_price
    elif params.exit_price_mode == "qualified_pool_twap":
        mark_price = (
            _qualified_pool_twap(
                qualified_price_observations,
                event.block_time,
                params.exit_price_twap_lookback_minutes,
            )
            or current_price
        )
    else:
        mark_price = current_price
    if mark_price <= 0:
        return current_price, current_tick
    return mark_price, _tick_from_cngn_price(mark_price, pool_config)


def _token_fee_wallet(
    token_index: int,
    token_amount: float,
    fee_rate: float,
    liquidity_share: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    fee_amount = max(token_amount, 0.0) * fee_rate * liquidity_share
    if fee_amount <= 0:
        return PortfolioComposition(0.0, 0.0)
    if token_index == 0:
        if pool_config.cngn_is_token0:
            return PortfolioComposition(stable_usd=0.0, cngn_amount=fee_amount)
        return PortfolioComposition(stable_usd=fee_amount, cngn_amount=0.0)
    if pool_config.cngn_is_token0:
        return PortfolioComposition(stable_usd=fee_amount, cngn_amount=0.0)
    return PortfolioComposition(stable_usd=0.0, cngn_amount=fee_amount)


def _event_fee_wallet(
    event: Event,
    fee_rate: float,
    liquidity_share: float,
    current_price: float,
    pool_config: PoolConfig,
) -> PortfolioComposition:
    if liquidity_share <= 0 or fee_rate <= 0:
        return PortfolioComposition(0.0, 0.0)
    if isinstance(event, V4Event):
        candidates: list[tuple[float, PortfolioComposition]] = []
        if event.amount0 > 0:
            wallet = _token_fee_wallet(0, event.amount0, fee_rate, liquidity_share, pool_config)
            candidates.append((wallet.value_usd(current_price), wallet))
        if event.amount1 > 0:
            wallet = _token_fee_wallet(1, event.amount1, fee_rate, liquidity_share, pool_config)
            candidates.append((wallet.value_usd(current_price), wallet))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
        return PortfolioComposition(stable_usd=event.amount_usd * fee_rate * liquidity_share, cngn_amount=0.0)

    sold_amount = event.token_sold_amount * fee_rate * liquidity_share
    if event.token_sold_symbol == "cNGN":
        return PortfolioComposition(stable_usd=0.0, cngn_amount=sold_amount)
    return PortfolioComposition(stable_usd=sold_amount, cngn_amount=0.0)


def _center_price_for_event(
    event: Event,
    params: BacktestParams,
    ewma: EWMACalculator,
    current_price: float,
    pool_config: PoolConfig,
) -> tuple[float, float]:
    if params.center_mode == "spot":
        quote_center = current_price
    elif params.center_mode == "fair_price":
        quote_center = _event_fair_price(event) or current_price
    else:
        quote_center = _native_to_cngn_price(ewma.mean, pool_config)

    if params.center_offset_pct:
        quote_center *= 1 + params.center_offset_pct
    native_center = _cngn_to_native_price(quote_center, pool_config)
    return quote_center, native_center


def _quote_pct_tick_range(
    quote_center: float,
    params: BacktestParams,
    pool_config: PoolConfig,
) -> tuple[int, int]:
    width_pct = params.fixed_width_pct
    if width_pct is None:
        raise ValueError("fixed_width_pct is required for fixed_pct_width range mode")
    lower_pct = params.lower_width_pct if params.lower_width_pct is not None else width_pct / 2
    upper_pct = params.upper_width_pct if params.upper_width_pct is not None else width_pct / 2
    quote_lower = max(quote_center * (1 - lower_pct), 1e-18)
    quote_upper = quote_center * (1 + upper_pct)

    if pool_config.invert_price:
        native_lower = _cngn_to_native_price(quote_upper, pool_config)
        native_upper = _cngn_to_native_price(quote_lower, pool_config)
        native_center = _cngn_to_native_price(quote_center, pool_config)
        native_lower_pct = abs(native_center - native_lower) / native_center if native_center > 0 else 0.0
        native_upper_pct = abs(native_upper - native_center) / native_center if native_center > 0 else 0.0
        return calculate_fixed_pct_tick_range(
            native_center,
            width_pct,
            pool_config.token0_decimals,
            pool_config.token1_decimals,
            pool_config.tick_spacing,
            params.min_tick_width,
            params.max_tick_width,
            lower_width_pct=native_lower_pct,
            upper_width_pct=native_upper_pct,
        )

    return calculate_fixed_pct_tick_range(
        quote_center,
        width_pct,
        pool_config.token0_decimals,
        pool_config.token1_decimals,
        pool_config.tick_spacing,
        params.min_tick_width,
        params.max_tick_width,
        lower_width_pct=lower_pct,
        upper_width_pct=upper_pct,
    )


def _calculate_entry_range(
    event: Event,
    params: BacktestParams,
    ewma: EWMACalculator,
    current_price: float,
    current_tick: int,
    pool_config: PoolConfig,
) -> tuple[int, int]:
    quote_center, native_center = _center_price_for_event(event, params, ewma, current_price, pool_config)
    if params.range_mode == "fixed_pct_width":
        return _quote_pct_tick_range(quote_center, params, pool_config)
    if params.range_mode == "fixed_tick_width":
        if params.fixed_tick_width is None:
            raise ValueError("fixed_tick_width is required for fixed_tick_width range mode")
        return calculate_fixed_tick_range(
            current_tick if params.center_mode == "spot" else _fast_price_to_tick(
                native_center, pool_config.token0_decimals, pool_config.token1_decimals
            ),
            params.fixed_tick_width,
            pool_config.tick_spacing,
            params.min_tick_width,
            params.max_tick_width,
            params.downside_skew,
        )
    return calculate_tick_range(
        ewma,
        params.sd_multiplier,
        params.downside_skew,
        pool_config.token0_decimals,
        pool_config.token1_decimals,
        pool_config.tick_spacing,
        params.min_tick_width,
        params.max_tick_width,
        center_price=native_center if params.center_mode != "ewma" else None,
    )


def _quote_upward_tick_delta(position: VirtualPosition, current_tick: int, pool_config: PoolConfig) -> int:
    if pool_config.cngn_is_token0:
        return current_tick - position.entry_tick
    return position.entry_tick - current_tick


def _range_traversal_fraction(position: VirtualPosition, current_tick: int, pool_config: PoolConfig) -> float:
    tick_width = position.tick_upper - position.tick_lower
    if tick_width <= 0:
        return 0.0
    return _quote_upward_tick_delta(position, current_tick, pool_config) / tick_width


def _out_of_range_overshoot_fraction(position: VirtualPosition, current_tick: int) -> float:
    tick_width = position.tick_upper - position.tick_lower
    if tick_width <= 0:
        return 0.0
    if current_tick < position.tick_lower:
        return (position.tick_lower - current_tick) / tick_width
    if current_tick >= position.tick_upper:
        return (current_tick - position.tick_upper) / tick_width
    return 0.0


def _entry_filters_pass(
    event: Event,
    params: BacktestParams,
    ewma: EWMACalculator,
    current_price: float,
    active_liquidity: int,
    pool_config: PoolConfig,
) -> bool:
    filters = params.entry_filters
    if filters.min_swap_volume_usd is not None and event.amount_usd < filters.min_swap_volume_usd:
        return False
    if filters.min_active_liquidity is not None and active_liquidity < filters.min_active_liquidity:
        return False
    if filters.max_realized_volatility is not None and ewma.std > filters.max_realized_volatility:
        return False
    fair_price = _event_fair_price(event)
    if (
        filters.max_fair_price_deviation is not None
        and fair_price is not None
        and fair_price > 0
        and abs(current_price / fair_price - 1.0) > filters.max_fair_price_deviation
    ):
        return False
    if filters.trend_filter == "flat_or_up":
        ewma_quote = _native_to_cngn_price(ewma.mean, pool_config)
        if current_price < ewma_quote:
            return False
    return True


def _expected_fee_apr_passes(
    event: Event,
    params: BacktestParams,
    liquidity: float,
    deployed_capital: float,
    active_liquidity: int,
    previous_swap_time: datetime | None,
    pool_config: PoolConfig,
) -> bool:
    min_apr = params.entry_filters.min_expected_fee_apr
    if min_apr is None:
        return True
    if previous_swap_time is None or deployed_capital <= 0 or active_liquidity <= 0:
        return False
    elapsed_seconds = (event.block_time - previous_swap_time).total_seconds()
    if elapsed_seconds <= 0:
        return False
    expected_fee = event.amount_usd * _event_pool_fee_rate(event, pool_config) * (liquidity / active_liquidity)
    elapsed_years = elapsed_seconds / (365 * 86400)
    expected_apr = (expected_fee / deployed_capital) / elapsed_years if elapsed_years > 0 else 0.0
    return expected_apr >= min_apr


def _gas_cost_for_action(cost_model: TransactionCostModel, action: str, fallback_gas_cost_usd: float) -> float:
    if action == "enter":
        return fallback_gas_cost_usd if cost_model.mint_gas_usd is None else cost_model.mint_gas_usd
    return fallback_gas_cost_usd if cost_model.remove_gas_usd is None else cost_model.remove_gas_usd


def _failed_tx_expected_cost(cost_model: TransactionCostModel, fallback_gas_cost_usd: float) -> float:
    failed_gas = fallback_gas_cost_usd if cost_model.failed_tx_gas_usd is None else cost_model.failed_tx_gas_usd
    return max(cost_model.failed_tx_probability, 0.0) * max(failed_gas, 0.0)


def _swap_direction_to_zero_for_one(direction: str, pool_config: PoolConfig) -> bool:
    if direction == "cngn_to_stable":
        return pool_config.cngn_is_token0
    if direction == "stable_to_cngn":
        return not pool_config.cngn_is_token0
    raise ValueError(f"Unsupported swap direction: {direction}")


def _input_raw_amount(
    direction: str,
    input_notional_usd: float,
    current_price: float,
    pool_config: PoolConfig,
) -> float:
    if direction == "stable_to_cngn":
        return input_notional_usd * 10 ** _stable_token_decimals(pool_config)
    cngn_amount = input_notional_usd / current_price if current_price > 0 else 0.0
    return cngn_amount * 10 ** _cngn_token_decimals(pool_config)


def _output_raw_amount(
    direction: str,
    output_notional_usd: float,
    current_price: float,
    pool_config: PoolConfig,
) -> float:
    if direction == "stable_to_cngn":
        cngn_amount = output_notional_usd / current_price if current_price > 0 else 0.0
        return cngn_amount * 10 ** _cngn_token_decimals(pool_config)
    return output_notional_usd * 10 ** _stable_token_decimals(pool_config)


def _raw_input_usd(
    direction: str,
    amount_raw: float,
    current_price: float,
    pool_config: PoolConfig,
) -> float:
    if direction == "stable_to_cngn":
        return amount_raw / 10 ** _stable_token_decimals(pool_config)
    return amount_raw / 10 ** _cngn_token_decimals(pool_config) * current_price


def _raw_output_usd(
    zero_for_one: bool,
    amount_out_raw: float,
    current_price: float,
    pool_config: PoolConfig,
) -> float:
    output_is_token0 = not zero_for_one
    if output_is_token0:
        return (amount_out_raw / 10 ** pool_config.token0_decimals) * _token0_usd_price(current_price, pool_config)
    return (amount_out_raw / 10 ** pool_config.token1_decimals) * _token1_usd_price(current_price, pool_config)


def _current_spacing_tick_range(current_tick: int, tick_spacing: int) -> tuple[int, int]:
    tick_lower = (current_tick // tick_spacing) * tick_spacing
    return tick_lower, tick_lower + tick_spacing


def _same_spacing_range(
    sqrt_price_next: float,
    current_tick: int,
    tick_spacing: int,
    zero_for_one: bool,
) -> bool:
    tick_lower, tick_upper = _current_spacing_tick_range(current_tick, tick_spacing)
    if zero_for_one:
        return sqrt_price_next >= tick_to_sqrt_price(tick_lower)
    return sqrt_price_next < tick_to_sqrt_price(tick_upper)


def _exact_clmm_input_costs(
    input_notional_usd: float,
    active_liquidity: int,
    fee_rate: float,
    current_tick: int,
    current_sqrt_price_x96: int,
    current_price: float,
    direction: str,
    pool_config: PoolConfig,
) -> tuple[float, float] | None:
    if input_notional_usd <= 0 or active_liquidity <= 0 or current_price <= 0:
        return None
    zero_for_one = _swap_direction_to_zero_for_one(direction, pool_config)
    input_raw = _input_raw_amount(direction, input_notional_usd, current_price, pool_config)
    sqrt_price = sqrt_price_x96_to_float(current_sqrt_price_x96) or tick_to_sqrt_price(current_tick)
    sqrt_next, _, amount_out_raw, fee_raw = compute_swap_step(
        sqrt_price,
        active_liquidity,
        input_raw,
        fee_rate,
        zero_for_one,
    )
    if not _same_spacing_range(sqrt_next, current_tick, pool_config.tick_spacing, zero_for_one):
        return None

    fee_usd = _raw_input_usd(direction, fee_raw, current_price, pool_config)
    effective_input_usd = max(input_notional_usd - fee_usd, 0.0)
    output_usd = _raw_output_usd(zero_for_one, amount_out_raw, current_price, pool_config)
    return fee_usd, max(effective_input_usd - output_usd, 0.0)


def _exact_clmm_output_costs(
    output_notional_usd: float,
    active_liquidity: int,
    fee_rate: float,
    current_tick: int,
    current_sqrt_price_x96: int,
    current_price: float,
    direction: str,
    pool_config: PoolConfig,
) -> tuple[float, float] | None:
    if output_notional_usd <= 0 or active_liquidity <= 0 or current_price <= 0 or fee_rate >= 1:
        return None
    zero_for_one = _swap_direction_to_zero_for_one(direction, pool_config)
    output_raw = _output_raw_amount(direction, output_notional_usd, current_price, pool_config)
    sqrt_price = sqrt_price_x96_to_float(current_sqrt_price_x96) or tick_to_sqrt_price(current_tick)

    if zero_for_one:
        sqrt_next = sqrt_price - output_raw / active_liquidity
        if sqrt_next <= 0:
            return None
        effective_input_raw = active_liquidity * (1 / sqrt_next - 1 / sqrt_price)
    else:
        denominator = 1 / sqrt_price - output_raw / active_liquidity
        if denominator <= 0:
            return None
        sqrt_next = 1 / denominator
        effective_input_raw = active_liquidity * (sqrt_next - sqrt_price)

    if not _same_spacing_range(sqrt_next, current_tick, pool_config.tick_spacing, zero_for_one):
        return None

    input_raw = effective_input_raw / (1 - fee_rate)
    fee_raw = input_raw - effective_input_raw
    fee_usd = _raw_input_usd(direction, fee_raw, current_price, pool_config)
    effective_input_usd = _raw_input_usd(direction, effective_input_raw, current_price, pool_config)
    return fee_usd, max(effective_input_usd - output_notional_usd, 0.0)


def _swap_cost_breakdown(
    action: str,
    swap_notional_usd: float,
    active_liquidity: int,
    fee_rate: float,
    current_tick: int,
    params: BacktestParams,
    direction: str | None = None,
    current_price: float | None = None,
    current_sqrt_price_x96: int | None = None,
    pool_config: PoolConfig | None = None,
    exact_output: bool = True,
) -> TransactionCostBreakdown:
    model = params.transaction_costs
    swap_notional_usd = max(swap_notional_usd, 0.0)
    gas_cost = _gas_cost_for_action(model, action, params.gas_cost_usd)
    failed_cost = _failed_tx_expected_cost(model, params.gas_cost_usd)
    if swap_notional_usd <= 0:
        return TransactionCostBreakdown(action=action, gas_cost=gas_cost, failed_tx_expected_cost=failed_cost)

    exact_costs = None
    if direction is not None and current_price is not None and pool_config is not None:
        sqrt_price_x96 = current_sqrt_price_x96 or tick_to_sqrt_price_x96(current_tick)
        if exact_output:
            exact_costs = _exact_clmm_output_costs(
                swap_notional_usd,
                active_liquidity,
                fee_rate,
                current_tick,
                sqrt_price_x96,
                current_price,
                direction,
                pool_config,
            )
        else:
            exact_costs = _exact_clmm_input_costs(
                swap_notional_usd,
                active_liquidity,
                fee_rate,
                current_tick,
                sqrt_price_x96,
                current_price,
                direction,
                pool_config,
            )

    if exact_costs is not None:
        swap_fee, price_impact = exact_costs
    else:
        swap_fee = swap_notional_usd * fee_rate
        if active_liquidity <= 0:
            price_impact = swap_notional_usd * model.fallback_price_impact_bps / 10_000
        else:
            sqrt_p = tick_to_sqrt_price(current_tick)
            spot_p = sqrt_p ** 2
            if spot_p <= 0:
                price_impact = swap_notional_usd * model.fallback_price_impact_bps / 10_000
            else:
                price_impact = (swap_notional_usd ** 2) / (2 * active_liquidity * spot_p)

    slippage = swap_notional_usd * max(model.swap_slippage_bps, 0.0) / 10_000
    latency = swap_notional_usd * max(model.latency_slippage_bps, 0.0) / 10_000
    return TransactionCostBreakdown(
        action=action,
        gas_cost=gas_cost,
        swap_fee_cost=swap_fee,
        price_impact_cost=price_impact,
        slippage_cost=slippage,
        latency_slippage_cost=latency,
        failed_tx_expected_cost=failed_cost,
        swap_notional_usd=swap_notional_usd,
    )


def _exit_cost_breakdown(
    position: VirtualPosition,
    current_tick: int,
    current_sqrt_price_x96: int,
    current_price: float,
    active_liquidity: int,
    fee_rate: float,
    pool_config: PoolConfig,
    params: BacktestParams,
) -> TransactionCostBreakdown:
    amount0, amount1 = position.amounts_at_sqrt_price_x96(current_sqrt_price_x96)
    cngn_notional = _cngn_notional_usd(amount0, amount1, current_price, pool_config)
    if not params.transaction_costs.unwind_to_cash_on_exit:
        cngn_notional = 0.0
    return _swap_cost_breakdown(
        "exit",
        cngn_notional,
        active_liquidity,
        fee_rate,
        current_tick,
        params,
        direction="cngn_to_stable" if cngn_notional > 0 else None,
        current_price=current_price,
        current_sqrt_price_x96=current_sqrt_price_x96,
        pool_config=pool_config,
        exact_output=False,
    )


def _profit_basis_return(
    position: VirtualPosition,
    position_value: float,
    exit_cost: float,
    current_price: float,
    params: BacktestParams,
) -> float:
    episode_capital = position.entry_value + position.entry_transaction_cost.total
    if episode_capital <= 0:
        return 0.0
    cost = exit_cost if params.require_profit_after_cost else 0.0
    if params.profit_take_pnl_mode == "fees":
        return (position.fee_value_usd(current_price) - cost - position.entry_transaction_cost.total) / episode_capital
    return (position_value - cost) / episode_capital - 1.0


def _paper_exit_reason(
    position: VirtualPosition,
    event: Event,
    current_tick: int,
    current_sqrt_price_x96: int,
    current_price: float,
    defensive_tick: int,
    defensive_price: float,
    active_liquidity: int,
    pool_config: PoolConfig,
    params: BacktestParams,
) -> tuple[str | None, TransactionCostBreakdown]:
    position_value = position.value_at_sqrt_price_x96(current_sqrt_price_x96, current_price, pool_config)
    defensive_position_value = position.value_at_sqrt_price_x96(
        tick_to_sqrt_price_x96(defensive_tick),
        defensive_price,
        pool_config,
    )
    exit_cost = _exit_cost_breakdown(
        position,
        current_tick,
        current_sqrt_price_x96,
        current_price,
        active_liquidity,
        _event_pool_fee_rate(event, pool_config),
        pool_config,
        params,
    )
    episode_capital = position.entry_value + position.entry_transaction_cost.total
    net_return_after_cost = (
        (defensive_position_value - exit_cost.total) / episode_capital - 1.0
        if episode_capital > 0
        else 0.0
    )

    if params.stop_loss_return is not None and net_return_after_cost <= params.stop_loss_return:
        return "stop_loss", exit_cost

    tick_width = position.tick_upper - position.tick_lower
    defensive_traversal = _range_traversal_fraction(position, defensive_tick, pool_config)
    if params.downward_range_fraction is not None and tick_width > 0 and defensive_traversal <= -params.downward_range_fraction:
        return "downward_move", exit_cost

    if not position.is_in_range(defensive_tick):
        overshoot_threshold = 0.0 if params.out_of_range_overshoot_fraction is None else params.out_of_range_overshoot_fraction
        if _out_of_range_overshoot_fraction(position, defensive_tick) >= overshoot_threshold:
            return "out_of_range", exit_cost
        return None, TransactionCostBreakdown("exit")

    if params.harvest_upward_range_fraction is None:
        return None, TransactionCostBreakdown("exit")
    traversal = _range_traversal_fraction(position, current_tick, pool_config)
    if traversal < params.harvest_upward_range_fraction:
        return None, TransactionCostBreakdown("exit")

    profit_take_return = params.profit_take_return if params.profit_take_return is not None else 0.0
    if _profit_basis_return(position, position_value, exit_cost.total, current_price, params) < profit_take_return:
        return None, TransactionCostBreakdown("exit")
    if params.require_profit_after_cost and net_return_after_cost <= 0:
        return None, TransactionCostBreakdown("exit")
    return "harvest_upward", exit_cost


def _is_defensive_exit(reason: str | None) -> bool:
    return reason in {"stop_loss", "downward_move", "out_of_range"}


def _defensive_exit_quality_passes(event: Event, params: BacktestParams) -> bool:
    min_volume = params.min_exit_swap_volume_usd
    if min_volume is not None and _event_swap_volume_usd(event) < min_volume:
        return False
    return True


def _record_episode(
    result: SimResult,
    position: VirtualPosition,
    exit_reason: str,
    exit_time: datetime,
    exit_price: float,
    exit_tick: int,
    exit_value: float,
    exit_cost: TransactionCostBreakdown,
    pool_config: PoolConfig,
) -> None:
    fee_value = position.fee_value_usd(exit_price)
    inventory_pnl = exit_value - fee_value - position.entry_value
    entry_cost = position.entry_transaction_cost
    total_transaction_cost = entry_cost.total + exit_cost.total
    gas_cost = entry_cost.gas_cost + exit_cost.gas_cost
    swap_fee_cost = entry_cost.swap_fee_cost + exit_cost.swap_fee_cost
    price_impact_cost = entry_cost.price_impact_cost + exit_cost.price_impact_cost
    slippage_cost = entry_cost.slippage_cost + exit_cost.slippage_cost
    latency_slippage_cost = entry_cost.latency_slippage_cost + exit_cost.latency_slippage_cost
    failed_tx_expected_cost = entry_cost.failed_tx_expected_cost + exit_cost.failed_tx_expected_cost
    swap_notional_usd = entry_cost.swap_notional_usd + exit_cost.swap_notional_usd
    episode_capital = position.entry_value + entry_cost.total
    net_pnl = exit_value - exit_cost.total - episode_capital
    result.episodes.append(
        EpisodeRecord(
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            entry_tick=position.entry_tick,
            tick_lower=position.tick_lower,
            tick_upper=position.tick_upper,
            entry_value=position.entry_value,
            exit_reason=exit_reason,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_tick=exit_tick,
            exit_value=exit_value,
            fees_earned=fee_value,
            fee_stable=position.accrued_fee_stable,
            fee_cngn=position.accrued_fee_cngn,
            rebalance_cost=exit_cost.total,
            entry_transaction_cost=entry_cost.total,
            exit_transaction_cost=exit_cost.total,
            total_transaction_cost=total_transaction_cost,
            gas_cost=gas_cost,
            swap_fee_cost=swap_fee_cost,
            price_impact_cost=price_impact_cost,
            slippage_cost=slippage_cost,
            latency_slippage_cost=latency_slippage_cost,
            failed_tx_expected_cost=failed_tx_expected_cost,
            swap_notional_usd=swap_notional_usd,
            inventory_pnl=inventory_pnl,
            net_pnl=net_pnl,
            net_return=net_pnl / episode_capital if episode_capital > 0 else 0.0,
            range_traversal_fraction=_range_traversal_fraction(position, exit_tick, pool_config),
            time_in_range=position.in_range_swaps / position.observed_swaps if position.observed_swaps else 0.0,
            duration_seconds=max((exit_time - position.entry_time).total_seconds(), 0.0),
            active_liquidity_share=(
                position.liquidity_L / position.entry_active_liquidity
                if position.entry_active_liquidity > 0
                else 0.0
            ),
        )
    )
    result.total_transaction_cost += total_transaction_cost
    result.total_entry_cost += entry_cost.total
    result.total_exit_cost += exit_cost.total
    result.total_gas_cost += gas_cost
    result.total_swap_fee_cost += swap_fee_cost
    result.total_price_impact_cost += price_impact_cost
    result.total_slippage_cost += slippage_cost
    result.total_latency_slippage_cost += latency_slippage_cost
    result.total_failed_tx_expected_cost += failed_tx_expected_cost


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
    wallet = PortfolioComposition(stable_usd=initial_capital_usd, cngn_amount=0.0)
    current_price = 0.0
    current_tick = 0
    current_sqrt_price_x96 = tick_to_sqrt_price_x96(0)
    day_start_value = initial_capital_usd
    current_day: date | None = None
    validation_start: PortfolioComposition | None = None
    cooldown_until_time: datetime | None = None
    cooldown_until_block: int | None = None
    previous_swap_time: datetime | None = None
    pending_defensive_exit_reason: str | None = None
    pending_defensive_exit_first_time: datetime | None = None
    pending_defensive_exit_count = 0
    qualified_price_observations: list[tuple[datetime, float]] = []

    for event in events:
        if isinstance(event, MintEvent):
            pool_state.apply_mint(event.tick_lower, event.tick_upper, event.liquidity_delta)
            continue
        if isinstance(event, BurnEvent):
            pool_state.apply_burn(event.tick_lower, event.tick_upper, event.liquidity_delta)
            continue
        if isinstance(event, V4Event) and event.event_type == "mint":
            if event.tick_lower is not None and event.tick_upper is not None and event.liquidity_delta is not None:
                pool_state.apply_mint(event.tick_lower, event.tick_upper, abs(event.liquidity_delta))
            continue
        if isinstance(event, V4Event) and event.event_type == "burn":
            if event.tick_lower is not None and event.tick_upper is not None and event.liquidity_delta is not None:
                pool_state.apply_burn(event.tick_lower, event.tick_upper, abs(event.liquidity_delta))
            continue

        assert isinstance(event, (SwapEvent, V4Event))
        result.total_swaps += 1
        result.start_time = result.start_time or event.block_time
        result.end_time = event.block_time
        current_price = _event_cngn_price(event, pool_config)
        if current_price <= 0:
            continue

        native_price = _cngn_to_native_price(current_price, pool_config)
        ewma.update(native_price)
        current_tick = event.tick if isinstance(event, V4Event) else _fast_price_to_tick(
            native_price, pool_config.token0_decimals, pool_config.token1_decimals
        )
        current_sqrt_price_x96 = _event_sqrt_price_x96(event, current_tick)
        active_liquidity = _event_liquidity(event, pool_state, current_tick)
        if _event_swap_volume_usd(event) >= params.exit_price_min_swap_volume_usd:
            qualified_price_observations.append((event.block_time, current_price))

        if validation_start is None:
            validation_start = _snapshot_composition(
                position,
                wallet,
                current_tick,
                current_sqrt_price_x96,
                current_price,
                pool_config,
            )

        event_day = event.block_time.date()
        if current_day is not None and event_day != current_day:
            current_val = _portfolio_value(
                position,
                wallet,
                current_price,
                current_tick,
                current_sqrt_price_x96,
                pool_config,
            )
            if day_start_value > 0:
                result.daily_returns.append(current_val / day_start_value - 1.0)
            day_start_value = current_val
        current_day = event_day

        if position is not None:
            position.observed_swaps += 1
            if position.is_in_range(current_tick):
                result.in_range_swaps += 1
                position.in_range_swaps += 1
                if active_liquidity > 0:
                    liquidity_share = position.liquidity_L / active_liquidity
                    fee_wallet = _event_fee_wallet(
                        event,
                        _event_pool_fee_rate(event, pool_config),
                        liquidity_share,
                        current_price,
                        pool_config,
                    )
                    position.accrued_fee_stable += fee_wallet.stable_usd
                    position.accrued_fee_cngn += fee_wallet.cngn_amount
                    result.total_fees += fee_wallet.value_usd(current_price)
                    result.total_fee_stable += fee_wallet.stable_usd
                    result.total_fee_cngn += fee_wallet.cngn_amount

            should_rebalance = False
            exit_reason: str | None = None
            exit_cost = TransactionCostBreakdown("exit")
            if params.strategy_mode == "paper":
                defensive_price, defensive_tick = _defensive_exit_mark(
                    event,
                    current_price,
                    current_tick,
                    pool_config,
                    params,
                    qualified_price_observations,
                )
                exit_reason, exit_cost = _paper_exit_reason(
                    position,
                    event,
                    current_tick,
                    current_sqrt_price_x96,
                    current_price,
                    defensive_tick,
                    defensive_price,
                    active_liquidity,
                    pool_config,
                    params,
                )
                if _is_defensive_exit(exit_reason):
                    if not _defensive_exit_quality_passes(event, params):
                        exit_reason = None
                        exit_cost = TransactionCostBreakdown("exit")
                    else:
                        if pending_defensive_exit_reason == exit_reason:
                            pending_defensive_exit_count += 1
                        else:
                            pending_defensive_exit_reason = exit_reason
                            pending_defensive_exit_first_time = event.block_time
                            pending_defensive_exit_count = 1
                        required_swaps = max(params.exit_confirmation_swaps, 1)
                        required_minutes = max(params.exit_confirmation_minutes, 0.0)
                        elapsed_minutes = (
                            (event.block_time - pending_defensive_exit_first_time).total_seconds() / 60
                            if pending_defensive_exit_first_time is not None
                            else 0.0
                        )
                        if pending_defensive_exit_count < required_swaps or elapsed_minutes < required_minutes:
                            exit_reason = None
                            exit_cost = TransactionCostBreakdown("exit")
                else:
                    pending_defensive_exit_reason = None
                    pending_defensive_exit_first_time = None
                    pending_defensive_exit_count = 0
                should_rebalance = exit_reason is not None
            else:
                tick_range = position.tick_upper - position.tick_lower
                if not position.is_in_range(current_tick):
                    distance = position.tick_lower - current_tick if current_tick < position.tick_lower else current_tick - position.tick_upper
                    if tick_range > 0 and (distance / tick_range * 100) >= params.rebalance_threshold_pct:
                        should_rebalance = True
                        exit_reason = "ewma_out_of_range"
                elif params.preemptive_rebalance:
                    margin = tick_range * params.rebalance_threshold_pct / 100
                    if current_tick - position.tick_lower < margin or position.tick_upper - current_tick < margin:
                        should_rebalance = True
                        exit_reason = "ewma_preemptive"

            if should_rebalance and ewma.ready:
                position_value = position.value_at_sqrt_price_x96(
                    current_sqrt_price_x96,
                    current_price,
                    pool_config,
                )
                if exit_cost.total == 0.0:
                    exit_cost = _exit_cost_breakdown(
                        position,
                        current_tick,
                        current_sqrt_price_x96,
                        current_price,
                        active_liquidity,
                        _event_pool_fee_rate(event, pool_config),
                        pool_config,
                        params,
                    )
                _record_episode(
                    result,
                    position,
                    exit_reason or "rebalance",
                    event.block_time,
                    current_price,
                    current_tick,
                    position_value,
                    exit_cost,
                    pool_config,
                )
                wallet = _wallet_with_position_removed(
                    wallet,
                    position,
                    current_tick,
                    current_sqrt_price_x96,
                    current_price,
                    pool_config,
                )
                wallet = _pay_or_unwind_exit_cost(wallet, exit_cost, current_price)
                result.total_rebalance_cost += exit_cost.total
                result.rebalance_count += 1
                if params.cooldown_minutes > 0:
                    cooldown_until_time = event.block_time + timedelta(minutes=params.cooldown_minutes)
                if params.cooldown_blocks > 0 and _event_block_number(event) is not None:
                    cooldown_until_block = (_event_block_number(event) or 0) + params.cooldown_blocks
                position = None
                pending_defensive_exit_reason = None
                pending_defensive_exit_first_time = None
                pending_defensive_exit_count = 0

        in_time_cooldown = cooldown_until_time is not None and event.block_time < cooldown_until_time
        event_block = _event_block_number(event)
        in_block_cooldown = (
            cooldown_until_block is not None
            and event_block is not None
            and event_block < cooldown_until_block
        )
        if (
            position is None
            and ewma.ready
            and wallet.value_usd(current_price) > params.gas_cost_usd
            and not in_time_cooldown
            and not in_block_cooldown
            and _entry_filters_pass(event, params, ewma, current_price, active_liquidity, pool_config)
        ):
            tick_lower, tick_upper = _calculate_entry_range(
                event,
                params,
                ewma,
                current_price,
                current_tick,
                pool_config,
            )
            liquidity, deployed_capital, entry_cost, next_wallet = _route_wallet_to_position(
                wallet,
                tick_lower,
                tick_upper,
                current_tick,
                current_sqrt_price_x96,
                current_price,
                active_liquidity,
                _event_pool_fee_rate(event, pool_config),
                pool_config,
                params,
            )
            if (
                liquidity > 0
                and _expected_fee_apr_passes(
                    event,
                    params,
                    liquidity,
                    deployed_capital,
                    active_liquidity,
                    previous_swap_time,
                    pool_config,
                )
            ):
                position = VirtualPosition(
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    liquidity_L=liquidity,
                    entry_price=current_price,
                    entry_value=deployed_capital,
                    entry_time=event.block_time,
                    entry_tick=current_tick,
                    entry_active_liquidity=active_liquidity,
                    deployed_capital=deployed_capital,
                    entry_transaction_cost=entry_cost,
                )
                wallet = next_wallet

        previous_swap_time = event.block_time

    result.final_value = _portfolio_value(
        position,
        wallet,
        current_price,
        current_tick,
        current_sqrt_price_x96,
        pool_config,
    )
    if position is not None and result.end_time is not None:
        _record_episode(
            result,
            position,
            "end_of_data",
            result.end_time,
            current_price,
            current_tick,
            position.value_at_sqrt_price_x96(current_sqrt_price_x96, current_price, pool_config),
            TransactionCostBreakdown("end_of_data"),
            pool_config,
        )
    if day_start_value > 0 and result.final_value > 0:
        result.daily_returns.append(result.final_value / day_start_value - 1.0)

    if validation_start is None:
        validation_start = PortfolioComposition(stable_usd=initial_capital_usd, cngn_amount=0.0)
    hodl_end_value = validation_start.value_usd(current_price)
    result.divergent_loss = (result.final_value / hodl_end_value - 1.0) if hodl_end_value > 0 else 0.0
    return result
