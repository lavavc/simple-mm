"""CSV loader and event types for historical pool data."""

import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Union


@dataclass
class SwapEvent:
    block_time: datetime
    blockchain: str
    pool_address: str
    amount_usd: float
    token_bought_symbol: str
    token_bought_amount: float
    token_sold_symbol: str
    token_sold_amount: float
    cngn_usd_price: float  # derived: USD per 1 cNGN


@dataclass
class MintEvent:
    block_time: datetime
    blockchain: str
    pool_address: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    amount0: float
    amount1: float


@dataclass
class BurnEvent:
    block_time: datetime
    blockchain: str
    pool_address: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    amount0: float
    amount1: float


Event = Union[SwapEvent, MintEvent, BurnEvent]


def _parse_time(raw: str) -> datetime:
    # "2025-02-16 15:26:20+00:00"
    return datetime.fromisoformat(raw)


def load_events(csv_path: str, pool_address: str | None = None) -> list[Event]:
    """Load and parse CSV into typed events, sorted chronologically."""
    events: list[Event] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row["pool_address"].strip()
            if pool_address and addr.lower() != pool_address.lower():
                continue

            bt = _parse_time(row["block_time"])
            bc = row["blockchain"].strip()
            etype = row["event_type"].strip()

            if etype == "swap":
                bought_sym = row["token_bought_symbol"].strip()
                sold_sym = row["token_sold_symbol"].strip()
                bought_amt = float(row["token_bought_amount"])
                sold_amt = float(row["token_sold_amount"])

                # Derive cNGN/USD price
                if sold_sym == "cNGN":
                    cngn_price = bought_amt / sold_amt if sold_amt else 0.0
                else:
                    cngn_price = sold_amt / bought_amt if bought_amt else 0.0

                events.append(SwapEvent(
                    block_time=bt,
                    blockchain=bc,
                    pool_address=addr,
                    amount_usd=float(row["amount_usd"]) if row["amount_usd"] else 0.0,
                    token_bought_symbol=bought_sym,
                    token_bought_amount=bought_amt,
                    token_sold_symbol=sold_sym,
                    token_sold_amount=sold_amt,
                    cngn_usd_price=cngn_price,
                ))
            elif etype in ("mint", "burn"):
                cls = MintEvent if etype == "mint" else BurnEvent
                events.append(cls(
                    block_time=bt,
                    blockchain=bc,
                    pool_address=addr,
                    tick_lower=int(row["tick_lower"]),
                    tick_upper=int(row["tick_upper"]),
                    liquidity_delta=int(row["liquidity_delta"]),
                    amount0=float(row["mint_burn_amount0"]),
                    amount1=float(row["mint_burn_amount1"]),
                ))

    events.sort(key=lambda e: e.block_time)
    return events
