from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


ZERO = Decimal("0")


@dataclass(frozen=True)
class SwapFlow:
    cngn_flow_direction: int
    signed_cngn_amount: Decimal
    signed_usd_notional: Decimal


def derive_swap_flow(row: Mapping[str, str]) -> SwapFlow:
    if row["event_type"] != "swap":
        return SwapFlow(
            cngn_flow_direction=0,
            signed_cngn_amount=ZERO,
            signed_usd_notional=ZERO,
        )

    token0_symbol = row["token0_symbol"]
    token1_symbol = row["token1_symbol"]

    if token0_symbol == "cNGN":
        pool_cngn_amount = Decimal(str(row["amount0"]))
    elif token1_symbol == "cNGN":
        pool_cngn_amount = Decimal(str(row["amount1"]))
    else:
        raise ValueError("swap row does not contain cNGN")

    signed_cngn_amount = -pool_cngn_amount
    cngn_flow_direction = _direction_for(signed_cngn_amount)
    if cngn_flow_direction == 0:
        return SwapFlow(
            cngn_flow_direction=0,
            signed_cngn_amount=ZERO,
            signed_usd_notional=ZERO,
        )

    usd_notional = Decimal(str(row["amount_usd"])).copy_abs()
    return SwapFlow(
        cngn_flow_direction=cngn_flow_direction,
        signed_cngn_amount=signed_cngn_amount,
        signed_usd_notional=usd_notional * cngn_flow_direction,
    )


def _direction_for(value: Decimal) -> int:
    if value > ZERO:
        return 1
    if value < ZERO:
        return -1
    return 0
