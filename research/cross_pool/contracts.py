"""Shared immutable contracts for cross-pool research."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

PoolName: TypeAlias = Literal["uni-base", "uni-bsc"]
StoredPriceModel: TypeAlias = Literal["sqrt_mid"]


class CrossPoolContractError(ValueError):
    """Raised when an input violates a frozen causal research contract."""


@dataclass(frozen=True)
class PoolEvent:
    pool: PoolName
    timestamp_ms: int
    block_number: int
    tx_hash: str
    log_index: int
    raw_mid: Decimal
    fee_adjusted_bid: Decimal
    fee_adjusted_ask: Decimal
    stored_cngn_usd_price: Decimal
    stored_price_model: StoredPriceModel


@dataclass(frozen=True)
class GapQuantiles:
    p50: int
    p95: int
    p99: int


@dataclass(frozen=True)
class StreamQuality:
    pool: PoolName
    rows: int
    first_timestamp_ms: int
    last_timestamp_ms: int
    update_gap_quantiles_ms: GapQuantiles | None
    pre_transition_rows: int
    post_transition_rows: int
