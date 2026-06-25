"""Derived paper LP episode features with receipt-backed gas fields."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from research.backtester.lp_paper_episodes import (
    CLOSE_ATTRIBUTION_INTERIM_COLLECT,
    CLOSE_ATTRIBUTION_MIXED,
    PaperLPEpisode,
    _pool_name,
    reconstruct_paper_episodes,
)
from research.backtester.v4_lp_ledger import OPENING_ATTRIBUTION_EXACT, LPLedgerRow

_EXACT_OPENING_ATTRIBUTION_STATUSES = {OPENING_ATTRIBUTION_EXACT, "fixture_exact"}
_WEI_PER_NATIVE = Decimal("1000000000000000000")


@dataclass(frozen=True)
class LPEpisodeFeature:
    pool: str
    lp_owner: str
    tick_lower: int
    tick_upper: int
    open_ms: int
    close_ms: int
    duration_ms: int
    opening_capital: Decimal
    closing_capital: Decimal
    gross_pnl: Decimal
    gross_return_on_capital: Decimal | None
    start_price: Decimal
    end_price: Decimal
    lower_price: Decimal
    upper_price: Decimal
    closed_liquidity: Decimal
    position_type: int | None
    delta_traversed: Decimal | None
    close_attribution_status: str
    close_attribution_source: str
    open_tx_hash: str
    close_tx_hash: str
    gas_tx_hashes: str
    gas_native_fee_wei: int | None
    gas_native_fee: Decimal | None
    native_token_usd: Decimal | None
    gas_cost_usd: Decimal | None
    net_pnl_after_gas: Decimal | None
    net_return_on_capital: Decimal | None
    gas_attribution_status: str
    net_pnl_status: str


@dataclass(frozen=True)
class _ReceiptGas:
    tx_hash: str
    native_fee_wei: int


@dataclass(frozen=True)
class _EpisodeTxCandidates:
    open_rows: list[LPLedgerRow]
    close_rows: list[LPLedgerRow]


def build_lp_episode_features(
    ledger_rows: Sequence[LPLedgerRow],
    receipt_rows: Sequence[dict[str, str]],
    *,
    native_token_usd: Decimal | None = None,
) -> list[LPEpisodeFeature]:
    episodes = reconstruct_paper_episodes(ledger_rows)
    receipt_by_tx = _receipt_gas_by_tx(receipt_rows)
    matches = _episode_tx_matches(ledger_rows)
    return [
        _episode_feature(
            episode,
            matches.get(_episode_match_key(episode)),
            receipt_by_tx,
            native_token_usd,
        )
        for episode in episodes
    ]


def _episode_feature(
    episode: PaperLPEpisode,
    tx_candidates: _EpisodeTxCandidates | None,
    receipt_by_tx: dict[str, _ReceiptGas],
    native_token_usd: Decimal | None,
) -> LPEpisodeFeature:
    gross_return = _return_on_capital(episode.opening_capital, episode.pnl)
    tx_match = _resolve_tx_match(episode, tx_candidates)
    if tx_match is None:
        return _feature_with_gas(
            episode,
            open_tx_hash="",
            close_tx_hash="",
            gas_tx_hashes=[],
            gas_native_fee_wei=None,
            native_token_usd=native_token_usd,
            gas_attribution_status="ambiguous_ledger_match",
            gross_return_on_capital=gross_return,
        )

    open_tx_hash, close_tx_hash = tx_match
    gas_tx_hashes = _unique_tx_hashes([open_tx_hash, close_tx_hash])
    gas_native_fee_wei = 0
    for tx_hash in gas_tx_hashes:
        receipt = receipt_by_tx.get(tx_hash.lower())
        if receipt is None:
            raise ValueError(f"missing receipt for episode gas tx_hash={tx_hash}")
        gas_native_fee_wei += receipt.native_fee_wei

    gas_status = "exact_open_close"
    if episode.close_attribution_status in {
        CLOSE_ATTRIBUTION_INTERIM_COLLECT,
        CLOSE_ATTRIBUTION_MIXED,
    }:
        gas_status = "open_close_only_interim_collect_excluded"

    return _feature_with_gas(
        episode,
        open_tx_hash=open_tx_hash,
        close_tx_hash=close_tx_hash,
        gas_tx_hashes=gas_tx_hashes,
        gas_native_fee_wei=gas_native_fee_wei,
        native_token_usd=native_token_usd,
        gas_attribution_status=gas_status,
        gross_return_on_capital=gross_return,
    )


def _feature_with_gas(
    episode: PaperLPEpisode,
    *,
    open_tx_hash: str,
    close_tx_hash: str,
    gas_tx_hashes: Sequence[str],
    gas_native_fee_wei: int | None,
    native_token_usd: Decimal | None,
    gas_attribution_status: str,
    gross_return_on_capital: Decimal | None,
) -> LPEpisodeFeature:
    gas_native_fee = (
        None
        if gas_native_fee_wei is None
        else Decimal(gas_native_fee_wei) / _WEI_PER_NATIVE
    )
    gas_cost_usd = None
    net_pnl = None
    net_return = None
    net_status = "native_price_missing"
    if gas_native_fee is None:
        net_status = "gas_attribution_unavailable"
    elif native_token_usd is not None:
        gas_cost_usd = gas_native_fee * native_token_usd
        net_pnl = episode.pnl - gas_cost_usd
        net_return = _return_on_capital(episode.opening_capital, net_pnl)
        net_status = "net_usd_available"

    return LPEpisodeFeature(
        pool=episode.pool,
        lp_owner=episode.lp_owner,
        tick_lower=episode.tick_lower,
        tick_upper=episode.tick_upper,
        open_ms=episode.open_ms,
        close_ms=episode.close_ms,
        duration_ms=episode.close_ms - episode.open_ms,
        opening_capital=episode.opening_capital,
        closing_capital=episode.closing_capital,
        gross_pnl=episode.pnl,
        gross_return_on_capital=gross_return_on_capital,
        start_price=episode.start_price,
        end_price=episode.end_price,
        lower_price=episode.lower_price,
        upper_price=episode.upper_price,
        closed_liquidity=episode.closed_liquidity,
        position_type=episode.position_type,
        delta_traversed=episode.delta_traversed,
        close_attribution_status=episode.close_attribution_status,
        close_attribution_source=episode.close_attribution_source,
        open_tx_hash=open_tx_hash,
        close_tx_hash=close_tx_hash,
        gas_tx_hashes="|".join(gas_tx_hashes),
        gas_native_fee_wei=gas_native_fee_wei,
        gas_native_fee=gas_native_fee,
        native_token_usd=native_token_usd,
        gas_cost_usd=gas_cost_usd,
        net_pnl_after_gas=net_pnl,
        net_return_on_capital=net_return,
        gas_attribution_status=gas_attribution_status,
        net_pnl_status=net_status,
    )


def _episode_tx_matches(
    ledger_rows: Sequence[LPLedgerRow],
) -> dict[tuple[str, str, int, int, int, int], _EpisodeTxCandidates]:
    opens: dict[tuple[str, str, int, int, int], list[LPLedgerRow]] = defaultdict(list)
    closes: dict[tuple[str, str, int, int, int], list[LPLedgerRow]] = defaultdict(list)
    for row in ledger_rows:
        if row.lp_owner is None or row.tick_lower is None or row.tick_upper is None:
            continue
        key = (
            _pool_name(row),
            row.lp_owner,
            row.tick_lower,
            row.tick_upper,
            row.timestamp_ms,
        )
        exact_opening = row.amount_attribution_status in _EXACT_OPENING_ATTRIBUTION_STATUSES
        if row.liquidity_delta > 0 and exact_opening:
            opens[key].append(row)
        elif row.liquidity_delta < 0:
            closes[key].append(row)

    matches: dict[tuple[str, str, int, int, int, int], _EpisodeTxCandidates] = {}
    for open_key, open_rows in opens.items():
        pool, owner, tick_lower, tick_upper, open_ms = open_key
        for close_key, close_rows in closes.items():
            close_pool, close_owner, close_lower, close_upper, close_ms = close_key
            if (pool, owner, tick_lower, tick_upper) != (
                close_pool,
                close_owner,
                close_lower,
                close_upper,
            ):
                continue
            key = (pool, owner, tick_lower, tick_upper, open_ms, close_ms)
            matches[key] = _EpisodeTxCandidates(
                open_rows=open_rows,
                close_rows=close_rows,
            )
    return matches


def _resolve_tx_match(
    episode: PaperLPEpisode,
    candidates: _EpisodeTxCandidates | None,
) -> tuple[str, str] | None:
    if candidates is None or len(candidates.open_rows) != 1:
        return None
    close_rows = candidates.close_rows
    if len(close_rows) != 1:
        close_rows = [
            row
            for row in close_rows
            if Decimal(str(abs(row.liquidity_delta))) == episode.closed_liquidity
        ]
    if len(close_rows) != 1:
        return None
    return candidates.open_rows[0].tx_hash, close_rows[0].tx_hash


def _episode_match_key(episode: PaperLPEpisode) -> tuple[str, str, int, int, int, int]:
    return (
        episode.pool,
        episode.lp_owner,
        episode.tick_lower,
        episode.tick_upper,
        episode.open_ms,
        episode.close_ms,
    )


def _receipt_gas_by_tx(receipt_rows: Sequence[dict[str, str]]) -> dict[str, _ReceiptGas]:
    receipts: dict[str, _ReceiptGas] = {}
    for row in receipt_rows:
        tx_hash = row["tx_hash"].strip().lower()
        if tx_hash in receipts:
            raise ValueError(f"duplicate receipt tx_hash={row['tx_hash']}")
        receipts[tx_hash] = _ReceiptGas(
            tx_hash=row["tx_hash"].strip(),
            native_fee_wei=int(row["native_fee_wei"]),
        )
    return receipts


def _unique_tx_hashes(tx_hashes: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for tx_hash in tx_hashes:
        normalized = tx_hash.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(tx_hash)
    return unique


def _return_on_capital(opening_capital: Decimal, pnl: Decimal) -> Decimal | None:
    if opening_capital == 0:
        return None
    return pnl / opening_capital
