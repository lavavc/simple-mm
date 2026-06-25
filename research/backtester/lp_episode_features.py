"""Derived paper LP episode features with receipt-backed gas fields."""

from __future__ import annotations

from bisect import bisect_right
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
    native_price_source: str
    native_price_max_age_ms: int | None
    gas_cost_usd: Decimal | None
    net_pnl_after_gas: Decimal | None
    net_return_on_capital: Decimal | None
    gas_attribution_status: str
    net_pnl_status: str


@dataclass(frozen=True)
class _ReceiptGas:
    chain: str
    tx_hash: str
    native_fee_wei: int


@dataclass(frozen=True)
class _EpisodeTxCandidates:
    open_rows: list[LPLedgerRow]
    close_rows: list[LPLedgerRow]


@dataclass(frozen=True)
class _GasTxEvent:
    tx_hash: str
    timestamp_ms: int


@dataclass(frozen=True)
class _ResolvedEpisodeTxs:
    open_tx_hash: str
    close_tx_hash: str
    gas_tx_events: list[_GasTxEvent]


@dataclass(frozen=True)
class _NativePrice:
    chain: str
    timestamp_ms: int
    native_token_usd: Decimal
    source: str


@dataclass(frozen=True)
class _NativePriceMatch:
    price: _NativePrice
    age_ms: int


@dataclass(frozen=True)
class _NativePriceLookup:
    prices_by_chain: dict[str, list[_NativePrice]]
    timestamps_by_chain: dict[str, list[int]]
    max_age_ms: int

    def previous_or_equal(self, chain: str, timestamp_ms: int) -> _NativePriceMatch:
        prices = self.prices_by_chain.get(chain)
        timestamps = self.timestamps_by_chain.get(chain)
        if prices is None or timestamps is None:
            raise ValueError(
                f"missing native price for chain={chain} "
                f"timestamp_ms={timestamp_ms} max_age_ms={self.max_age_ms}"
            )
        index = bisect_right(timestamps, timestamp_ms) - 1
        if index < 0:
            raise ValueError(
                f"missing native price for chain={chain} "
                f"timestamp_ms={timestamp_ms} max_age_ms={self.max_age_ms}"
            )
        price = prices[index]
        age_ms = timestamp_ms - price.timestamp_ms
        if age_ms > self.max_age_ms:
            raise ValueError(
                f"missing native price for chain={chain} "
                f"timestamp_ms={timestamp_ms} max_age_ms={self.max_age_ms}"
            )
        return _NativePriceMatch(price=price, age_ms=age_ms)


@dataclass(frozen=True)
class _GasPricing:
    gas_native_fee_wei: int | None
    gas_native_fee: Decimal | None
    native_token_usd: Decimal | None
    native_price_source: str
    native_price_max_age_ms: int | None
    gas_cost_usd: Decimal | None


def build_lp_episode_features(
    ledger_rows: Sequence[LPLedgerRow],
    receipt_rows: Sequence[dict[str, str]],
    *,
    native_token_usd: Decimal | None = None,
    native_price_rows: Sequence[dict[str, str]] | None = None,
    native_price_max_age_ms: int | None = None,
) -> list[LPEpisodeFeature]:
    native_price_lookup = _native_price_lookup(
        native_token_usd=native_token_usd,
        native_price_rows=native_price_rows,
        native_price_max_age_ms=native_price_max_age_ms,
    )
    episodes = reconstruct_paper_episodes(ledger_rows)
    receipt_by_tx = _receipt_gas_by_tx(receipt_rows)
    matches = _episode_tx_matches(ledger_rows)
    return [
        _episode_feature(
            episode,
            matches.get(_episode_match_key(episode)),
            receipt_by_tx,
            native_token_usd,
            native_price_lookup,
        )
        for episode in episodes
    ]


def _episode_feature(
    episode: PaperLPEpisode,
    tx_candidates: _EpisodeTxCandidates | None,
    receipt_by_tx: dict[str, _ReceiptGas],
    native_token_usd: Decimal | None,
    native_price_lookup: _NativePriceLookup | None,
) -> LPEpisodeFeature:
    gross_return = _return_on_capital(episode.opening_capital, episode.pnl)
    tx_match = _resolve_tx_match(episode, tx_candidates)
    if tx_match is None:
        return _feature_with_gas(
            episode,
            open_tx_hash="",
            close_tx_hash="",
            gas_tx_hashes=[],
            gas_pricing=_unavailable_gas_pricing(native_token_usd),
            gas_attribution_status="ambiguous_ledger_match",
            gross_return_on_capital=gross_return,
        )

    gas_pricing = _price_gas_events(
        tx_match.gas_tx_events,
        receipt_by_tx,
        native_token_usd=native_token_usd,
        native_price_lookup=native_price_lookup,
    )

    gas_status = "exact_open_close"
    if episode.close_attribution_status in {
        CLOSE_ATTRIBUTION_INTERIM_COLLECT,
        CLOSE_ATTRIBUTION_MIXED,
    }:
        gas_status = "open_close_only_interim_collect_excluded"

    return _feature_with_gas(
        episode,
        open_tx_hash=tx_match.open_tx_hash,
        close_tx_hash=tx_match.close_tx_hash,
        gas_tx_hashes=[event.tx_hash for event in tx_match.gas_tx_events],
        gas_pricing=gas_pricing,
        gas_attribution_status=gas_status,
        gross_return_on_capital=gross_return,
    )


def _feature_with_gas(
    episode: PaperLPEpisode,
    *,
    open_tx_hash: str,
    close_tx_hash: str,
    gas_tx_hashes: Sequence[str],
    gas_pricing: _GasPricing,
    gas_attribution_status: str,
    gross_return_on_capital: Decimal | None,
) -> LPEpisodeFeature:
    net_pnl = None
    net_return = None
    net_status = "native_price_missing"
    if gas_pricing.gas_native_fee is None:
        net_status = "gas_attribution_unavailable"
    elif gas_pricing.gas_cost_usd is not None:
        net_pnl = episode.pnl - gas_pricing.gas_cost_usd
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
        gas_native_fee_wei=gas_pricing.gas_native_fee_wei,
        gas_native_fee=gas_pricing.gas_native_fee,
        native_token_usd=gas_pricing.native_token_usd,
        native_price_source=gas_pricing.native_price_source,
        native_price_max_age_ms=gas_pricing.native_price_max_age_ms,
        gas_cost_usd=gas_pricing.gas_cost_usd,
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
) -> _ResolvedEpisodeTxs | None:
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
    open_row = candidates.open_rows[0]
    close_row = close_rows[0]
    return _ResolvedEpisodeTxs(
        open_tx_hash=open_row.tx_hash,
        close_tx_hash=close_row.tx_hash,
        gas_tx_events=_unique_gas_tx_events(
            [
                _GasTxEvent(open_row.tx_hash, open_row.timestamp_ms),
                _GasTxEvent(close_row.tx_hash, close_row.timestamp_ms),
            ]
        ),
    )


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
            chain=row["chain"].strip(),
            tx_hash=row["tx_hash"].strip(),
            native_fee_wei=int(row["native_fee_wei"]),
        )
    return receipts


def _unique_gas_tx_events(events: Sequence[_GasTxEvent]) -> list[_GasTxEvent]:
    unique: list[_GasTxEvent] = []
    timestamps_by_tx: dict[str, int] = {}
    seen: set[str] = set()
    for event in events:
        normalized = event.tx_hash.lower()
        if normalized in seen:
            if timestamps_by_tx[normalized] != event.timestamp_ms:
                raise ValueError(
                    f"same tx_hash matched multiple timestamps tx_hash={event.tx_hash}"
                )
            continue
        seen.add(normalized)
        timestamps_by_tx[normalized] = event.timestamp_ms
        unique.append(event)
    return unique


def _return_on_capital(opening_capital: Decimal, pnl: Decimal) -> Decimal | None:
    if opening_capital == 0:
        return None
    return pnl / opening_capital


def _native_price_lookup(
    *,
    native_token_usd: Decimal | None,
    native_price_rows: Sequence[dict[str, str]] | None,
    native_price_max_age_ms: int | None,
) -> _NativePriceLookup | None:
    if native_token_usd is not None and native_price_rows is not None:
        raise ValueError("native_token_usd and native_price_rows are mutually exclusive")
    if native_price_rows is None:
        if native_price_max_age_ms is not None:
            raise ValueError("native_price_max_age_ms requires native_price_rows")
        return None
    if native_price_max_age_ms is None:
        raise ValueError("native_price_rows requires native_price_max_age_ms")
    if native_price_max_age_ms < 0:
        raise ValueError("native_price_max_age_ms must be non-negative")

    prices_by_chain: dict[str, list[_NativePrice]] = defaultdict(list)
    seen_keys: set[tuple[str, int]] = set()
    for row in native_price_rows:
        chain = row["chain"].strip()
        timestamp_ms = int(row["timestamp_ms"])
        key = (chain, timestamp_ms)
        if key in seen_keys:
            raise ValueError(f"duplicate native price chain={chain} timestamp_ms={timestamp_ms}")
        seen_keys.add(key)
        price = Decimal(row["native_token_usd"])
        if price <= 0:
            raise ValueError(
                f"native_token_usd must be positive chain={chain} timestamp_ms={timestamp_ms}"
            )
        source = row["source"].strip()
        if not source:
            raise ValueError(f"blank native price source chain={chain} timestamp_ms={timestamp_ms}")
        prices_by_chain[chain].append(
            _NativePrice(
                chain=chain,
                timestamp_ms=timestamp_ms,
                native_token_usd=price,
                source=source,
            )
        )

    if not prices_by_chain:
        raise ValueError("empty native_price_rows")

    sorted_prices_by_chain = {
        chain: sorted(rows, key=lambda price: price.timestamp_ms)
        for chain, rows in prices_by_chain.items()
    }
    timestamps_by_chain = {
        chain: [price.timestamp_ms for price in rows]
        for chain, rows in sorted_prices_by_chain.items()
    }
    return _NativePriceLookup(
        prices_by_chain=sorted_prices_by_chain,
        timestamps_by_chain=timestamps_by_chain,
        max_age_ms=native_price_max_age_ms,
    )


def _price_gas_events(
    gas_tx_events: Sequence[_GasTxEvent],
    receipt_by_tx: dict[str, _ReceiptGas],
    *,
    native_token_usd: Decimal | None,
    native_price_lookup: _NativePriceLookup | None,
) -> _GasPricing:
    gas_native_fee_wei = 0
    gas_cost_usd = Decimal("0") if (
        native_token_usd is not None or native_price_lookup is not None
    ) else None
    max_price_age_ms: int | None = None
    price_sources: list[str] = []

    for event in gas_tx_events:
        receipt = receipt_by_tx.get(event.tx_hash.lower())
        if receipt is None:
            raise ValueError(f"missing receipt for episode gas tx_hash={event.tx_hash}")
        gas_native_fee_wei += receipt.native_fee_wei
        gas_native_fee = Decimal(receipt.native_fee_wei) / _WEI_PER_NATIVE
        if native_token_usd is not None:
            gas_cost_usd = _add_gas_cost(gas_cost_usd, gas_native_fee * native_token_usd)
            price_sources.append("manual_constant")
        elif native_price_lookup is not None:
            price_match = native_price_lookup.previous_or_equal(receipt.chain, event.timestamp_ms)
            gas_cost_usd = _add_gas_cost(
                gas_cost_usd,
                gas_native_fee * price_match.price.native_token_usd,
            )
            max_price_age_ms = (
                price_match.age_ms
                if max_price_age_ms is None
                else max(max_price_age_ms, price_match.age_ms)
            )
            price_sources.append(price_match.price.source)

    total_gas_native_fee = Decimal(gas_native_fee_wei) / _WEI_PER_NATIVE
    weighted_native_price = None
    if gas_cost_usd is not None:
        weighted_native_price = (
            gas_cost_usd / total_gas_native_fee
            if total_gas_native_fee != 0
            else native_token_usd
        )

    return _GasPricing(
        gas_native_fee_wei=gas_native_fee_wei,
        gas_native_fee=total_gas_native_fee,
        native_token_usd=weighted_native_price,
        native_price_source="|".join(_unique_strings(price_sources)),
        native_price_max_age_ms=max_price_age_ms,
        gas_cost_usd=gas_cost_usd,
    )


def _unavailable_gas_pricing(native_token_usd: Decimal | None) -> _GasPricing:
    return _GasPricing(
        gas_native_fee_wei=None,
        gas_native_fee=None,
        native_token_usd=native_token_usd,
        native_price_source="manual_constant" if native_token_usd is not None else "",
        native_price_max_age_ms=None,
        gas_cost_usd=None,
    )


def _add_gas_cost(existing: Decimal | None, increment: Decimal) -> Decimal:
    if existing is None:
        raise ValueError("gas_cost_usd accumulator is unavailable")
    return existing + increment


def _unique_strings(values: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
