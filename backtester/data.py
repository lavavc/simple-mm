"""Historical event loaders for legacy and Uniswap v4 backtest datasets."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Union

from backtester.clmm_math import cngn_price_from_sqrt_price_x96


def _parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


@dataclass(frozen=True)
class SwapEvent:
    block_time: datetime
    blockchain: str
    pool_address: str
    amount_usd: float
    token_bought_symbol: str
    token_bought_amount: float
    token_sold_symbol: str
    token_sold_amount: float
    cngn_usd_price: float


@dataclass(frozen=True)
class MintEvent:
    block_time: datetime
    blockchain: str
    pool_address: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    amount0: float
    amount1: float


@dataclass(frozen=True)
class BurnEvent:
    block_time: datetime
    blockchain: str
    pool_address: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    amount0: float
    amount1: float


LegacyEvent = Union[SwapEvent, MintEvent, BurnEvent]


@dataclass(frozen=True)
class V4Event:
    block_time: datetime
    chain: str
    pool_id: str
    event_type: Literal["swap", "mint", "burn"]
    tx_hash: str
    log_index: int
    block_number: int
    sqrt_price_x96: int
    tick: int
    active_liquidity: int
    fee_rate: float
    amount0: float
    amount1: float
    amount_usd: float
    cngn_usd_price: float
    token0_symbol: str
    token1_symbol: str
    tick_lower: int | None = None
    tick_upper: int | None = None
    liquidity_delta: int | None = None
    fair_price_usd: float | None = None


Event = Union[LegacyEvent, V4Event]


def _as_float(row: dict[str, str], key: str) -> float:
    raw = row.get(key, "")
    return float(raw) if raw not in ("", None) else 0.0


def _infer_legacy_cngn_price(row: dict[str, str]) -> float:
    bought_sym = row["token_bought_symbol"].strip()
    sold_sym = row["token_sold_symbol"].strip()
    bought_amt = float(row["token_bought_amount"])
    sold_amt = float(row["token_sold_amount"])
    if sold_sym == "cNGN":
        return bought_amt / sold_amt if sold_amt else 0.0
    if bought_sym == "cNGN":
        return sold_amt / bought_amt if bought_amt else 0.0
    return 0.0


def _token_decimals(symbol: str, chain: str) -> int:
    normalized = symbol.strip().upper()
    if normalized == "CNGN":
        return 6
    if normalized == "USDC":
        return 6
    if normalized == "USDT":
        return 18 if chain.strip().lower() in {"bsc", "bnb"} else 6
    raise ValueError(f"Cannot infer decimals for token symbol {symbol!r} on chain {chain!r}")


def _infer_v4_cngn_price_from_state(row: dict[str, str], sqrt_price_x96: int) -> float:
    chain = row["chain"].strip()
    token0_symbol = row["token0_symbol"].strip()
    token1_symbol = row["token1_symbol"].strip()
    token0_decimals = _token_decimals(token0_symbol, chain)
    token1_decimals = _token_decimals(token1_symbol, chain)
    invert_price = token1_symbol.upper() == "CNGN"
    return cngn_price_from_sqrt_price_x96(
        sqrt_price_x96,
        token0_decimals,
        token1_decimals,
        invert_price,
    )


def load_events(csv_path: str, pool_address: str | None = None) -> list[LegacyEvent]:
    """Load legacy CSV events sorted chronologically."""
    events: list[LegacyEvent] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row["pool_address"].strip()
            if pool_address and addr.lower() != pool_address.lower():
                continue

            block_time = _parse_time(row["block_time"])
            blockchain = row["blockchain"].strip()
            event_type = row["event_type"].strip()

            if event_type == "swap":
                events.append(
                    SwapEvent(
                        block_time=block_time,
                        blockchain=blockchain,
                        pool_address=addr,
                        amount_usd=_as_float(row, "amount_usd"),
                        token_bought_symbol=row["token_bought_symbol"].strip(),
                        token_bought_amount=_as_float(row, "token_bought_amount"),
                        token_sold_symbol=row["token_sold_symbol"].strip(),
                        token_sold_amount=_as_float(row, "token_sold_amount"),
                        cngn_usd_price=_infer_legacy_cngn_price(row),
                    )
                )
            elif event_type in ("mint", "burn"):
                cls = MintEvent if event_type == "mint" else BurnEvent
                events.append(
                    cls(
                        block_time=block_time,
                        blockchain=blockchain,
                        pool_address=addr,
                        tick_lower=int(row["tick_lower"]),
                        tick_upper=int(row["tick_upper"]),
                        liquidity_delta=int(row["liquidity_delta"]),
                        amount0=_as_float(row, "mint_burn_amount0"),
                        amount1=_as_float(row, "mint_burn_amount1"),
                    )
                )

    events.sort(key=lambda event: event.block_time)
    return events


def load_v4_events(csv_path: str, pool_id: str | None = None) -> list[V4Event]:
    """Load normalized Uniswap v4 events sorted for deterministic replay."""
    events: list[V4Event] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_pool_id = row["pool_id"].strip()
            if pool_id and row_pool_id.lower() != pool_id.lower():
                continue

            event_type = row["event_type"].strip().lower()
            if event_type not in {"swap", "mint", "burn"}:
                continue

            sqrt_price_x96 = int(row["sqrt_price_x96"])
            token0_symbol = row["token0_symbol"].strip()
            token1_symbol = row["token1_symbol"].strip()
            events.append(
                V4Event(
                    block_time=_parse_time(row["block_time"]),
                    chain=row["chain"].strip(),
                    pool_id=row_pool_id,
                    event_type=event_type,
                    tx_hash=row["tx_hash"].strip(),
                    log_index=int(row["log_index"]),
                    block_number=int(row["block_number"]),
                    sqrt_price_x96=sqrt_price_x96,
                    tick=int(row["tick"]),
                    active_liquidity=int(row["active_liquidity"]),
                    fee_rate=_as_float(row, "fee_rate"),
                    amount0=_as_float(row, "amount0"),
                    amount1=_as_float(row, "amount1"),
                    amount_usd=_as_float(row, "amount_usd"),
                    cngn_usd_price=_infer_v4_cngn_price_from_state(row, sqrt_price_x96),
                    token0_symbol=token0_symbol,
                    token1_symbol=token1_symbol,
                    tick_lower=int(row["tick_lower"]) if row.get("tick_lower") else None,
                    tick_upper=int(row["tick_upper"]) if row.get("tick_upper") else None,
                    liquidity_delta=int(row["liquidity_delta"]) if row.get("liquidity_delta") else None,
                    fair_price_usd=_as_float(row, "fair_price_usd") if row.get("fair_price_usd") else None,
                )
            )

    events.sort(key=lambda event: (event.block_time, event.block_number, event.log_index))
    return events
