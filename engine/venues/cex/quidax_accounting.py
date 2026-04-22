"""Quidax account balance presentation helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from engine.config import settings
from engine.types import AccountBalanceResponse


def build_quidax_account_balance(
    *,
    role: str,
    address: str,
    balances: dict[str, Any],
) -> AccountBalanceResponse:
    token_balances = {
        "cNGN": Decimal(str(balances.get("cngn", "0"))),
        "USDT": Decimal(str(balances.get("usdt", "0"))),
    }
    refill_reasons = _quidax_refill_reasons(token_balances)
    return AccountBalanceResponse(
        role=role,
        address=address,
        chain_id=0,
        native_balance=Decimal("0"),
        native_symbol="",
        token_balances=token_balances,
        needs_refill=bool(refill_reasons),
        refill_reasons=refill_reasons,
    )


def _quidax_refill_reasons(token_balances: dict[str, Decimal]) -> list[str]:
    reasons: list[str] = []
    cngn_balance = token_balances.get("cNGN", Decimal("0"))
    usdt_balance = token_balances.get("USDT", Decimal("0"))
    if cngn_balance < settings.quidax_min_cngn:
        reasons.append(f"Low cNGN: {cngn_balance} < {settings.quidax_min_cngn} min")
    if usdt_balance < settings.quidax_min_usdt:
        reasons.append(f"Low USDT: {usdt_balance} < {settings.quidax_min_usdt} min")
    return reasons
