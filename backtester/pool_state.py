"""Tick-level liquidity tracker and V3 swap simulator."""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.math.v3 import compute_swap_step, tick_to_sqrt_price


@dataclass
class SwapResult:
    amount_out: float
    new_sqrt_price: float
    fee_paid: float
    price_impact_bps: float


class PoolState:
    """Sparse tick map built from mint/burn events."""

    def __init__(self) -> None:
        self.tick_map: dict[int, int] = {}  # tick → liquidityNet (signed)

    def copy(self) -> PoolState:
        ps = PoolState()
        ps.tick_map = self.tick_map.copy()
        return ps

    def apply_mint(self, tick_lower: int, tick_upper: int, liquidity_delta: int) -> None:
        self.tick_map[tick_lower] = self.tick_map.get(tick_lower, 0) + liquidity_delta
        self.tick_map[tick_upper] = self.tick_map.get(tick_upper, 0) - liquidity_delta

    def apply_burn(self, tick_lower: int, tick_upper: int, liquidity_delta: int) -> None:
        self.tick_map[tick_lower] = self.tick_map.get(tick_lower, 0) - liquidity_delta
        self.tick_map[tick_upper] = self.tick_map.get(tick_upper, 0) + liquidity_delta

    def get_active_liquidity(self, current_tick: int) -> int:
        """Walk initialised ticks ≤ current_tick, summing liquidityNet."""
        liquidity = 0
        for tick in sorted(self.tick_map):
            if tick > current_tick:
                break
            liquidity += self.tick_map[tick]
        return max(liquidity, 0)

    def simulate_swap(
        self,
        current_sqrt_price: float,
        amount_in: float,
        zero_for_one: bool,
        fee_rate: float,
    ) -> SwapResult:
        """Cross-tick V3 swap simulation (whitepaper §6.3.1)."""
        if amount_in <= 0:
            return SwapResult(0.0, current_sqrt_price, 0.0, 0.0)

        spot = current_sqrt_price ** 2
        remaining = amount_in
        total_out = 0.0
        total_fee = 0.0
        sqrt_p = current_sqrt_price

        # Current tick from sqrt_price
        import math as _math
        current_tick = int(_math.log(sqrt_p**2) / _math.log(1.0001))

        # Get sorted initialised ticks
        init_ticks = sorted(self.tick_map.keys())
        liquidity = self._liquidity_at(current_tick, init_ticks)

        while remaining > 1e-12:
            # Find next initialised tick in swap direction
            next_tick = self._next_init_tick(current_tick, init_ticks, zero_for_one)
            if next_tick is None:
                # No more ticks — consume rest in current range
                sp_next, consumed, out, fee = compute_swap_step(
                    sqrt_p, liquidity, remaining, fee_rate, zero_for_one
                )
                total_out += out
                total_fee += fee
                sqrt_p = sp_next
                break

            sqrt_at_next = tick_to_sqrt_price(next_tick)

            # Max input to reach next tick boundary
            if liquidity > 0:
                if zero_for_one:
                    max_in = liquidity * (1.0 / sqrt_at_next - 1.0 / sqrt_p) / (1.0 - fee_rate)
                else:
                    max_in = liquidity * (sqrt_at_next - sqrt_p) / (1.0 - fee_rate)
                max_in = max(max_in, 0.0)
            else:
                # No liquidity — skip to next tick
                sqrt_p = sqrt_at_next
                current_tick = next_tick
                delta_l = self.tick_map.get(next_tick, 0)
                liquidity += delta_l if not zero_for_one else -delta_l
                liquidity = max(liquidity, 0)
                continue

            if remaining <= max_in:
                # Swap finishes in this range
                sp_next, consumed, out, fee = compute_swap_step(
                    sqrt_p, liquidity, remaining, fee_rate, zero_for_one
                )
                total_out += out
                total_fee += fee
                sqrt_p = sp_next
                break
            else:
                # Fill the range, cross the tick
                sp_next, consumed, out, fee = compute_swap_step(
                    sqrt_p, liquidity, max_in, fee_rate, zero_for_one
                )
                total_out += out
                total_fee += fee
                remaining -= max_in
                sqrt_p = sqrt_at_next
                current_tick = next_tick

                delta_l = self.tick_map.get(next_tick, 0)
                if zero_for_one:
                    liquidity -= delta_l
                else:
                    liquidity += delta_l
                liquidity = max(liquidity, 0)

        # Price impact
        exec_price = sqrt_p ** 2
        impact_bps = abs(exec_price / spot - 1.0) * 10_000 if spot > 0 else 0.0

        return SwapResult(
            amount_out=total_out,
            new_sqrt_price=sqrt_p,
            fee_paid=total_fee,
            price_impact_bps=impact_bps,
        )

    # -- helpers --

    def _liquidity_at(self, current_tick: int, init_ticks: list[int]) -> int:
        liq = 0
        for t in init_ticks:
            if t > current_tick:
                break
            liq += self.tick_map[t]
        return max(liq, 0)

    @staticmethod
    def _next_init_tick(
        current_tick: int, init_ticks: list[int], zero_for_one: bool
    ) -> int | None:
        if zero_for_one:
            # Going left — find highest init tick strictly < current_tick
            cand = None
            for t in init_ticks:
                if t < current_tick:
                    cand = t
                else:
                    break
            return cand
        else:
            # Going right — find lowest init tick > current_tick
            for t in init_ticks:
                if t > current_tick:
                    return t
            return None
