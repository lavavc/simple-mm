"""
Route registry: single source of truth for all registered arbitrage routes.

Adding a new venue means adding entries to ROUTES here. The execution engine,
router, and signal handlers all read leg types and venue names from this registry
— no other files need changing when a new venue or direction is introduced.
"""
from dataclasses import dataclass
from typing import Literal


Pipeline = Literal["cex_dex", "dex_dex"]
TradeLegType = Literal["onchain", "api"]
TradeAction = Literal["buy", "sell"]
CngnEffect = Literal["buys_cngn_from_cex", "sells_cngn_to_cex", "neutral"]


@dataclass(frozen=True)
class TradeLeg:
    venue: str      # "uni-bsc", "uni-base", "quidax", etc.
    leg_type: TradeLegType
    action: TradeAction


@dataclass(frozen=True)
class TradeRoute:
    direction: str    # e.g. "QUIDAX_TO_UNI_BSC"
    pipeline: Pipeline
    buy_leg: TradeLeg
    sell_leg: TradeLeg
    cngn_effect: CngnEffect


ROUTES: list[TradeRoute] = [
    # CEX → DEX: buy cNGN on Quidax, sell cNGN on DEX
    TradeRoute(
        "QUIDAX_TO_UNI_BSC", "cex_dex",
        TradeLeg("quidax",   "api",     "buy"),
        TradeLeg("uni-bsc",  "onchain", "sell"),
        "buys_cngn_from_cex",
    ),
    TradeRoute(
        "QUIDAX_TO_UNI_BASE", "cex_dex",
        TradeLeg("quidax",   "api",     "buy"),
        TradeLeg("uni-base", "onchain", "sell"),
        "buys_cngn_from_cex",
    ),
    # DEX → CEX: buy cNGN on DEX, sell cNGN on Quidax
    TradeRoute(
        "UNI_BSC_TO_QUIDAX", "cex_dex",
        TradeLeg("uni-bsc",  "onchain", "buy"),
        TradeLeg("quidax",   "api",     "sell"),
        "sells_cngn_to_cex",
    ),
    TradeRoute(
        "UNI_BASE_TO_QUIDAX", "cex_dex",
        TradeLeg("uni-base", "onchain", "buy"),
        TradeLeg("quidax",   "api",     "sell"),
        "sells_cngn_to_cex",
    ),
    # DEX → DEX
    TradeRoute(
        "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE", "dex_dex",
        TradeLeg("uni-bsc",  "onchain", "buy"),
        TradeLeg("uni-base", "onchain", "sell"),
        "neutral",
    ),
    TradeRoute(
        "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE", "dex_dex",
        TradeLeg("uni-base", "onchain", "buy"),
        TradeLeg("uni-bsc",  "onchain", "sell"),
        "neutral",
    ),
]

# O(1) lookup by direction string
ROUTES_BY_DIRECTION: dict[str, TradeRoute] = {r.direction: r for r in ROUTES}
