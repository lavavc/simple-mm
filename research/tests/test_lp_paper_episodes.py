from decimal import Decimal

from research.backtester.lp_paper_episodes import (
    PaperLPEpisode,
    analyze_paper_episode_attribution,
    classify_position_type,
    paper_win_score,
    reconstruct_paper_episodes,
)
from research.backtester.v4_lp_ledger import LPLedgerRow


def ledger_row(
    action_type: str,
    owner: str,
    tick_lower: int,
    tick_upper: int,
    liquidity_delta: int,
    amount0: Decimal = Decimal("0"),
    amount1: Decimal = Decimal("0"),
    collect_amount0: Decimal = Decimal("0"),
    collect_amount1: Decimal = Decimal("0"),
    price: Decimal = Decimal("1"),
    timestamp_ms: int = 1000,
    amount_attribution_status: str | None = None,
) -> LPLedgerRow:
    status = amount_attribution_status
    if status is None:
        status = "fixture_exact" if action_type == "mint" else "not_applicable"
    return LPLedgerRow(
        chain="base",
        pool_id="0xpool",
        block_number=timestamp_ms,
        block_time="2026-01-01T00:00:00+00:00",
        tx_hash=f"0x{timestamp_ms}",
        log_index=0,
        event_order=0,
        event_type=action_type,
        position_manager="0xpm",
        token_id=1,
        lp_owner=owner,
        owner_source="transfer",
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        liquidity_after=max(liquidity_delta, 0),
        amount0=amount0,
        amount1=amount1,
        amount0_actual=amount0,
        amount1_actual=amount1,
        amount0_attribution_source="fixture",
        amount1_attribution_source="fixture",
        amount_attribution_status=status,
        amount0_raw=str(amount0),
        amount1_raw=str(amount1),
        collect_amount0=collect_amount0,
        collect_amount1=collect_amount1,
        sqrt_price_x96_at_event=79228162514264337593543950336,
        tick_at_event=0,
        cngn_usd_price_at_event=price,
        timestamp_ms=timestamp_ms,
    )


def test_partial_burn_closes_first_position_and_splits_payout() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            price=Decimal("1"),
            timestamp_ms=1000,
        ),
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("20"),
            amount1=Decimal("20"),
            price=Decimal("1"),
            timestamp_ms=2000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-150,
            collect_amount0=Decimal("45"),
            collect_amount1=Decimal("45"),
            price=Decimal("1"),
            timestamp_ms=3000,
        ),
    ]

    episodes = reconstruct_paper_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].pool == "uni-base"
    assert episodes[0].closed_liquidity == Decimal("100")
    assert episodes[0].opening_capital == Decimal("20")
    assert episodes[0].closing_capital == Decimal("60")
    assert episodes[0].pnl == Decimal("40")
    assert episodes[0].close_attribution_status == "exact_collect"
    assert episodes[0].close_attribution_source == "exact_collect"


def test_overburn_is_capped_to_observed_open_liquidity() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            price=Decimal("1"),
            timestamp_ms=1000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-250,
            collect_amount0=Decimal("30"),
            collect_amount1=Decimal("30"),
            price=Decimal("1"),
            timestamp_ms=3000,
        ),
    ]

    episodes = reconstruct_paper_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].closed_liquidity == Decimal("100")
    assert episodes[0].closing_capital == Decimal("60")
    assert episodes[0].close_attribution_status == "exact_collect"


def test_ambiguous_opening_rows_are_excluded_from_exact_pnl_reconstruction() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            amount_attribution_status="ambiguous_multiple_add_actions",
            timestamp_ms=1000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-100,
            collect_amount0=Decimal("30"),
            collect_amount1=Decimal("30"),
            timestamp_ms=3000,
        ),
    ]

    assert reconstruct_paper_episodes(rows) == []


def test_partial_lot_realized_payout_is_carried_until_full_close() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            timestamp_ms=1000,
        ),
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("20"),
            amount1=Decimal("20"),
            timestamp_ms=2000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-150,
            collect_amount0=Decimal("45"),
            collect_amount1=Decimal("45"),
            timestamp_ms=3000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-50,
            collect_amount0=Decimal("15"),
            collect_amount1=Decimal("15"),
            timestamp_ms=4000,
        ),
    ]

    episodes = reconstruct_paper_episodes(rows)

    assert len(episodes) == 2
    assert episodes[1].closed_liquidity == Decimal("100")
    assert episodes[1].opening_capital == Decimal("40")
    assert episodes[1].closing_capital == Decimal("60")
    assert episodes[1].pnl == Decimal("20")


def test_same_transaction_collect_row_funds_liquidity_close() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            timestamp_ms=1000,
        ),
        ledger_row(
            "burn",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-100,
            timestamp_ms=3000,
        ),
        ledger_row(
            "collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=0,
            collect_amount0=Decimal("15"),
            collect_amount1=Decimal("15"),
            timestamp_ms=3000,
        ),
    ]

    episodes = reconstruct_paper_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].opening_capital == Decimal("20")
    assert episodes[0].closing_capital == Decimal("30")
    assert episodes[0].pnl == Decimal("10")
    assert episodes[0].close_attribution_status == "same_tx_collect"
    assert episodes[0].close_attribution_source == "same_tx_collect"


def test_interim_collect_row_is_carried_until_lot_closes() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            timestamp_ms=1000,
        ),
        ledger_row(
            "collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=0,
            collect_amount0=Decimal("15"),
            collect_amount1=Decimal("15"),
            timestamp_ms=2000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-100,
            timestamp_ms=3000,
        ),
    ]

    episodes = reconstruct_paper_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].opening_capital == Decimal("20")
    assert episodes[0].closing_capital == Decimal("30")
    assert episodes[0].pnl == Decimal("10")
    assert episodes[0].close_attribution_status == "interim_collect"
    assert episodes[0].close_attribution_source == "interim_collect"


def test_zero_collect_close_is_flagged() -> None:
    rows = [
        ledger_row(
            "mint",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=100,
            amount0=Decimal("10"),
            amount1=Decimal("10"),
            timestamp_ms=1000,
        ),
        ledger_row(
            "burn_collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=-100,
            timestamp_ms=3000,
        ),
    ]

    episodes = reconstruct_paper_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].closing_capital == Decimal("0")
    assert episodes[0].close_attribution_status == "zero_collect_close"
    assert episodes[0].close_attribution_source == "none"


def test_unmatched_collect_rows_are_reported() -> None:
    rows = [
        ledger_row(
            "collect",
            owner="0xlp",
            tick_lower=-100,
            tick_upper=100,
            liquidity_delta=0,
            collect_amount0=Decimal("15"),
            collect_amount1=Decimal("15"),
            timestamp_ms=2000,
        ),
    ]

    attribution = analyze_paper_episode_attribution(rows)

    assert attribution.episodes == []
    assert attribution.unmatched_collect_rows == 1
    assert attribution.unmatched_collect_capital == Decimal("30")


def test_position_taxonomy_matches_paper_figure_three() -> None:
    lower = Decimal("1.0")
    upper = Decimal("2.0")
    cases = [
        (Decimal("0.5"), Decimal("0.8"), 1),
        (Decimal("0.8"), Decimal("0.5"), 2),
        (Decimal("0.5"), Decimal("1.5"), 3),
        (Decimal("1.5"), Decimal("0.5"), 4),
        (Decimal("1.2"), Decimal("1.8"), 5),
        (Decimal("1.8"), Decimal("1.2"), 6),
        (Decimal("1.5"), Decimal("2.5"), 7),
        (Decimal("2.5"), Decimal("1.5"), 8),
        (Decimal("0.5"), Decimal("2.5"), 9),
        (Decimal("2.5"), Decimal("0.5"), 10),
        (Decimal("2.2"), Decimal("2.5"), 11),
        (Decimal("2.5"), Decimal("2.2"), 12),
        (Decimal("1.5"), Decimal("1.5"), 13),
        (Decimal("0.5"), Decimal("0.5"), 14),
        (Decimal("2.5"), Decimal("2.5"), 15),
    ]

    for start_price, end_price, expected_type in cases:
        assert classify_position_type(
            start_price,
            end_price,
            lower,
            upper,
            Decimal("0"),
        ) == expected_type


def test_paper_win_score_integrates_realized_cumulative_pnl_path() -> None:
    episodes = [
        PaperLPEpisode(
            "uni-base",
            "0xlp",
            -100,
            100,
            0,
            2_500,
            Decimal("100"),
            Decimal("110"),
            Decimal("10"),
            Decimal("1"),
            Decimal("1.1"),
            Decimal("0.9"),
            Decimal("1.2"),
            Decimal("100"),
            5,
            Decimal("0.1"),
        ),
        PaperLPEpisode(
            "uni-base",
            "0xlp",
            -100,
            100,
            0,
            5_000,
            Decimal("100"),
            Decimal("90"),
            Decimal("-20"),
            Decimal("1"),
            Decimal("0.9"),
            Decimal("0.9"),
            Decimal("1.2"),
            Decimal("100"),
            6,
            Decimal("-0.1"),
        ),
        PaperLPEpisode(
            "uni-base",
            "0xlp",
            -100,
            100,
            0,
            7_500,
            Decimal("100"),
            Decimal("120"),
            Decimal("20"),
            Decimal("1"),
            Decimal("1.2"),
            Decimal("0.9"),
            Decimal("1.2"),
            Decimal("100"),
            5,
            Decimal("0.2"),
        ),
    ]

    assert paper_win_score(episodes, 0, 10_000) == Decimal("0.6666666666666666666666666667")


def test_paper_win_score_returns_neutral_when_cumulative_path_has_no_excursion() -> None:
    flat_episode = PaperLPEpisode(
        "uni-base",
        "0xlp",
        -100,
        100,
        0,
        5_000,
        Decimal("100"),
        Decimal("100"),
        Decimal("0"),
        Decimal("1"),
        Decimal("1.1"),
        Decimal("0.9"),
        Decimal("1.2"),
        Decimal("100"),
        13,
        Decimal("0"),
    )

    assert paper_win_score([flat_episode], 0, 10_000) == Decimal("0.5")
