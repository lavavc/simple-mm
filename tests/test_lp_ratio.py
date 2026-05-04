"""Unit tests for compute_required_ratio and prepare_lp_balance."""

import math
from decimal import Decimal
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from eth_abi import decode  # type: ignore[attr-defined]
import pytest

from engine.types import TxResult
from engine.venues.dex.shared import _Q96, PositionState, compute_required_ratio
from engine.lp.types import (
    LPBalanceSwapResult,
    LPMarketSnapshot,
    LPStaticPositionMetadata,
    _V4_LP_BURN_POSITION,
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_SETTLE_PAIR,
    _V4_LP_TAKE_PAIR,
)
from engine.lp.uniswap_v4 import V4PositionManager
from engine.lp import strategy


def _price_to_sqrt_x96(price: float, token0_decimals: int = 6, token1_decimals: int = 6) -> int:
    """Convert a human-readable token1-per-token0 price to sqrtPriceX96."""
    dec_adj = 10 ** (token0_decimals - token1_decimals)
    return int(math.sqrt(price * dec_adj) * _Q96)


def _make_balance_adapter(
    sqrt_price_x96: int,
    balance0_raw: int,
    balance1_raw: int,
    swap_result: TxResult | None = None,
    token0_decimals: int = 6,
    token1_decimals: int = 6,
) -> SimpleNamespace:
    """Minimal self-mock for V4PositionManager.prepare_lp_balance."""
    pool_id = "0x" + "ab" * 32
    token0_addr = "0x" + "aa" * 20
    token1_addr = "0x" + "bb" * 20
    lp_addr = "0x" + "cc" * 20

    state_view = MagicMock()
    state_view.functions.getSlot0.return_value.call.return_value = [sqrt_price_x96, 0, 0, 0]

    token0 = MagicMock()
    token0.functions.balanceOf.return_value.call.return_value = balance0_raw
    token1 = MagicMock()
    token1.functions.balanceOf.return_value.call.return_value = balance1_raw

    w3 = MagicMock()
    w3.eth.contract.side_effect = [token0, token1]

    return SimpleNamespace(
        config=SimpleNamespace(
            pool_id=pool_id,
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            token0_address=token0_addr,
            token1_address=token1_addr,
            invert_price=False,
        ),
        name="uni-base",
        _lp_account=SimpleNamespace(address=lp_addr),
        _state_view=state_view,
        _w3=w3,
        _swap_from_lp=AsyncMock(
            return_value=swap_result or TxResult(hash="0xswap", status="confirmed")
        ),
    )


def _make_position_state(
    *,
    tick_lower: int = -1000,
    tick_upper: int = 1000,
    token_id: int = 77,
    current_price: Decimal = Decimal("1"),
    in_range: bool = True,
) -> PositionState:
    return PositionState(
        token_id=token_id,
        liquidity=1_000_000,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        tokens_owed_0=0,
        tokens_owed_1=0,
        price_lower=Decimal("0.9"),
        price_upper=Decimal("1.1"),
        current_price=current_price,
        in_range=in_range,
    )


def _make_lp_snapshot_adapter(
    pos_states: list[PositionState] | None,
    *,
    sqrt_price_x96: int,
    current_price: Decimal = Decimal("1"),
    pool_liquidity: Decimal = Decimal("5000000"),
    live_market: LPMarketSnapshot | None = None,
    include_live_market: bool = True,
) -> SimpleNamespace:
    positions = pos_states or []
    config = SimpleNamespace(
        pool_id="0x" + "ab" * 32,
        token0_decimals=6,
        token1_decimals=6,
        token0_symbol="cNGN",
        token1_symbol="USDC",
        cngn_is_token0=True,
        invert_price=False,
    )
    position_metadata = {
        pos.token_id: LPStaticPositionMetadata(
            token_id=pos.token_id,
            liquidity=pos.liquidity,
            tick_lower=pos.tick_lower,
            tick_upper=pos.tick_upper,
            range_min=pos.price_lower,
            range_max=pos.price_upper,
        )
        for pos in positions
    }
    adapter = SimpleNamespace(
        name="uni-base",
        config=config,
        get_owned_positions=lambda: [pos.token_id for pos in positions],
        _get_live_pool_snapshot=lambda: (
            live_market
            if live_market is not None
            else (
                LPMarketSnapshot(
                    sqrt_price_x96=Decimal(sqrt_price_x96),
                    current_tick=0,
                    current_price=current_price,
                    pool_liquidity=pool_liquidity,
                )
                if include_live_market
                else None
            )
        ),
        _get_static_position_metadata=lambda token_id: position_metadata.get(token_id),
        _lp_account=SimpleNamespace(address="0x" + "cc" * 20),
        position_manager_contract=object(),
        _state_view=MagicMock(),
    )
    adapter._compute_lp_token_amounts = MethodType(V4PositionManager._compute_lp_token_amounts, adapter)
    adapter._compute_lp_token_amounts_from_metadata = MethodType(
        V4PositionManager._compute_lp_token_amounts_from_metadata,
        adapter,
    )
    adapter._build_position_state_from_metadata = MethodType(
        V4PositionManager._build_position_state_from_metadata,
        adapter,
    )
    adapter._build_lp_position_snapshot = MethodType(V4PositionManager._build_lp_position_snapshot, adapter)
    adapter._build_degraded_lp_snapshot = MethodType(V4PositionManager._build_degraded_lp_snapshot, adapter)
    adapter._empty_position_balances = MethodType(V4PositionManager._empty_position_balances, adapter)
    adapter._add_symbol_balance = MethodType(V4PositionManager._add_symbol_balance, adapter)
    adapter.get_portfolio_balances = MethodType(V4PositionManager.get_portfolio_balances, adapter)
    adapter.get_active_lp_position_snapshot = MethodType(V4PositionManager.get_active_lp_position_snapshot, adapter)
    adapter.get_position_as_schema = MethodType(V4PositionManager.get_position_as_schema, adapter)
    return adapter


class TestComputeRequiredRatio:
    """Tests for shared.compute_required_ratio."""

    def test_price_below_range_all_token0(self):
        """Price below range: position is entirely token0, r1 == 0."""
        sqrt_p = _price_to_sqrt_x96(0.5)  # below tick_lower=1000
        r0, r1 = compute_required_ratio(1000, 2000, sqrt_p, 6, 6)
        assert r0 > 0
        assert r1 == 0

    def test_price_above_range_all_token1(self):
        """Price above range: position is entirely token1, r0 == 0."""
        sqrt_p = _price_to_sqrt_x96(2.0)  # above tick_upper=-1000
        r0, r1 = compute_required_ratio(-2000, -1000, sqrt_p, 6, 6)
        assert r0 == 0
        assert r1 > 0

    def test_price_near_upper_less_token0(self):
        """Closer to upper tick → less token0 needed than at midpoint."""
        sqrt_mid = _price_to_sqrt_x96(0.5)
        sqrt_near_upper = _price_to_sqrt_x96(1.005)  # tick ≈ 50, close to upper=100
        r0_mid, _ = compute_required_ratio(-2000, 100, sqrt_mid, 6, 6)
        r0_upper, _ = compute_required_ratio(-2000, 100, sqrt_near_upper, 6, 6)
        assert r0_upper < r0_mid

    def test_decimal_adjustment_affects_r0(self):
        """Dec adjustment must change r0 when token decimals differ."""
        sqrt_p_6_6 = _price_to_sqrt_x96(1.0, 6, 6)
        sqrt_p_6_18 = _price_to_sqrt_x96(1.0, 6, 18)
        r0_equal, r1_equal = compute_required_ratio(-1000, 1000, sqrt_p_6_6, 6, 6)
        r0_diff, r1_diff = compute_required_ratio(-1000, 1000, sqrt_p_6_18, 6, 18)
        # For equal-decimal 6/6 at symmetric range: both tokens have equal value weight
        assert abs(float(r0_equal) - float(r1_equal)) / float(r1_equal) < 0.01, "6/6 should give ~50/50"
        # Different decimals produce different r0 (dec_adj changes the ratio)
        assert r0_diff != r0_equal


class TestPrepareLpBalance:
    """Tests for V4PositionManager.prepare_lp_balance via mocked RPC."""

    @pytest.mark.asyncio
    async def test_no_swap_when_balanced(self):
        """Balances already at target ratio (within 1%): no swap triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        # Symmetric range at price ≈ 1: target is approximately 50/50 by value.
        # Fund with equal raw amounts — should be close enough to skip the swap.
        adapter = _make_balance_adapter(sqrt_p, 500_000_000, 500_000_000)
        await V4PositionManager.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_not_called()

    @pytest.mark.asyncio
    async def test_swap_token0_to_token1_when_excess_token0(self):
        """All token0, none of token1: surplus token0 swap is triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        # Entirely in token0, none in token1 — large imbalance
        adapter = _make_balance_adapter(sqrt_p, 1_000_000_000, 0)
        await V4PositionManager.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        call_args = adapter._swap_from_lp.call_args[0]
        assert call_args[0].lower() == adapter.config.token0_address.lower()

    @pytest.mark.asyncio
    async def test_swap_token1_to_token0_when_excess_token1(self):
        """All token1, none of token0: surplus token1 swap is triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        adapter = _make_balance_adapter(sqrt_p, 0, 1_000_000_000)
        await V4PositionManager.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        call_args = adapter._swap_from_lp.call_args[0]
        assert call_args[0].lower() == adapter.config.token1_address.lower()

    @pytest.mark.asyncio
    async def test_failed_swap_logs_warning_and_returns(self):
        """If the ratio swap fails, prepare_lp_balance logs and returns False."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        failed = TxResult(hash="", status="failed", error="reverted")
        adapter = _make_balance_adapter(sqrt_p, 1_000_000_000, 0, swap_result=failed)
        result = await V4PositionManager.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        assert isinstance(result, LPBalanceSwapResult)
        assert result.tx_result.status == "failed"

    @pytest.mark.asyncio
    async def test_target1_never_negative_near_upper_bound(self):
        """target1 is clamped to 0 when floating-point drift makes it negative."""
        # Price very close to upper tick — r1 ≈ 0, target0 * price ≈ total_value.
        # Without max(0, ...), target1 can go slightly negative and trigger a wrong swap.
        tick_lower, tick_upper = -2000, 100
        sqrt_near_upper = _price_to_sqrt_x96(1.009)  # tick ≈ 90, very close to upper=100
        # Fund only token0 (worst case for the negative-target1 scenario)
        adapter = _make_balance_adapter(sqrt_near_upper, 1_000_000_000, 0)
        await V4PositionManager.prepare_lp_balance(adapter, tick_lower, tick_upper)
        # Should not have swapped token1→token0 (that would be wrong direction)
        if adapter._swap_from_lp.called:
            call_args = adapter._swap_from_lp.call_args[0]
            assert call_args[0].lower() == adapter.config.token0_address.lower(), \
                "Wrong swap direction: target1 went negative, triggering spurious token1→token0 swap"


class TestActiveLpPositionSnapshot:
    """Tests for V4PositionManager.get_active_lp_position_snapshot."""

    def test_live_pool_snapshot_keeps_slot0_when_liquidity_read_fails(self):
        state_view = MagicMock()
        state_view.functions.getSlot0.return_value.call.return_value = [
            _price_to_sqrt_x96(1.0),
            0,
            0,
            0,
        ]
        state_view.functions.getLiquidity.return_value.call.side_effect = RuntimeError("liquidity unavailable")
        adapter = SimpleNamespace(
            name="uni-base",
            config=SimpleNamespace(
                pool_id="0x" + "ab" * 32,
                token0_decimals=6,
                token1_decimals=6,
                invert_price=False,
            ),
            _state_view=state_view,
        )

        result = V4PositionManager._get_live_pool_snapshot(adapter)

        assert result is not None
        assert result.current_tick == 0
        assert result.current_price == Decimal("1")
        assert result.pool_liquidity is None

    def test_snapshot_below_range_is_all_token0(self):
        pos_state = _make_position_state(tick_lower=1000, tick_upper=2000, in_range=False)
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(0.5))
        result = V4PositionManager.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token0_amount > 0
        assert result.token1_amount == 0
        assert result.snapshot_status == "live"

    def test_snapshot_above_range_is_all_token1(self):
        pos_state = _make_position_state(tick_lower=-2000, tick_upper=-1000, in_range=False)
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(2.0))
        result = V4PositionManager.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token0_amount == 0
        assert result.token1_amount > 0

    def test_snapshot_returns_degraded_summary_for_multiple_positions(self):
        adapter = _make_lp_snapshot_adapter(
            [_make_position_state(token_id=77), _make_position_state(token_id=78)],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
        )

        result = V4PositionManager.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token_id is None
        assert result.snapshot_status == "degraded"
        assert result.liquidity is None
        assert result.range_min is None
        assert result.token0_amount is None
        assert (
            result.snapshot_message
            == "Multiple LP NFTs detected; automatic LP management is halted until manual cleanup."
        )

    def test_snapshot_returns_degraded_summary_when_live_pool_state_unavailable(self):
        pos_state = _make_position_state(token_id=77, in_range=True)
        adapter = _make_lp_snapshot_adapter(
            [pos_state],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
            include_live_market=False,
        )

        result = V4PositionManager.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.snapshot_status == "degraded"
        assert result.token_id == 77
        assert result.token0_amount is None
        assert result.position_value_usd is None
        assert result.range_min == Decimal("0.9")
        assert result.snapshot_message == "LP position exists, but live composition is unavailable."

    def test_snapshot_returns_degraded_summary_for_zero_liquidity_nft(self):
        pos_state = _make_position_state(token_id=77, in_range=True)
        pos_state.liquidity = 0
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(1.0))

        result = V4PositionManager.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token_id == 77
        assert result.liquidity == 0
        assert result.snapshot_status == "degraded"
        assert result.token0_amount is None
        assert result.snapshot_message == "LP NFT exists, but has no active liquidity."

    def test_portfolio_balances_use_shared_pool_cache_when_live_unavailable(self, monkeypatch):
        pos_state = _make_position_state(token_id=77, in_range=True)
        adapter = _make_lp_snapshot_adapter(
            [pos_state],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
            include_live_market=False,
        )

        monkeypatch.setattr(
            "engine.market.pool_state.get_cached_pool_state",
            lambda _pool_id: (Decimal(_price_to_sqrt_x96(1.0)), Decimal("5000000"), 123.0, None),
        )

        balances = V4PositionManager.get_portfolio_balances(adapter)

        assert balances["cngn"] > 0
        assert balances["usdc"] > 0
        assert balances["usdt"] == 0

    def test_portfolio_balances_skip_multiple_positions(self):
        adapter = _make_lp_snapshot_adapter(
            [_make_position_state(token_id=77), _make_position_state(token_id=78)],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
        )

        balances = V4PositionManager.get_portfolio_balances(adapter)

        assert balances == {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}

    def test_get_owned_positions_falls_back_to_transfer_logs_when_not_enumerable(self):
        owner = "0x" + "cc" * 20
        token_ids = [101, 202]

        position_manager_contract = MagicMock()
        position_manager_contract.address = "0x" + "dd" * 20
        position_manager_contract.functions.balanceOf.return_value.call.return_value = 2
        position_manager_contract.functions.tokenOfOwnerByIndex.return_value.call.side_effect = Exception("no data")

        def owner_of_side_effect(token_id: int):
            call = MagicMock()
            call.call.return_value = owner
            return call

        position_manager_contract.functions.ownerOf.side_effect = owner_of_side_effect

        log_entries = [
            {"topics": [b"", b"", b"", token_id.to_bytes(32, "big")]}
            for token_id in token_ids
        ]
        w3 = MagicMock()
        w3.eth.get_logs.side_effect = [log_entries, []]

        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            _lp_account=SimpleNamespace(address=owner),
            _w3=w3,
        )
        adapter._get_owned_positions_from_logs = MethodType(
            V4PositionManager._get_owned_positions_from_logs,
            adapter,
        )

        owned = V4PositionManager.get_owned_positions(adapter)

        assert owned == token_ids

    def test_get_owned_positions_caches_fallback_and_skips_not_minted_tokens(self):
        owner = "0x" + "cc" * 20
        valid_token_id = 202
        burned_token_id = 101

        position_manager_contract = MagicMock()
        position_manager_contract.address = "0x" + "dd" * 20
        position_manager_contract.functions.balanceOf.return_value.call.return_value = 1
        position_manager_contract.functions.tokenOfOwnerByIndex.return_value.call.side_effect = Exception("no data")

        owner_lookup_calls: list[int] = []

        def owner_of_side_effect(token_id: int):
            call = MagicMock()
            owner_lookup_calls.append(token_id)
            if token_id == burned_token_id:
                call.call.side_effect = Exception("execution reverted: NOT_MINTED")
            else:
                call.call.return_value = owner
            return call

        position_manager_contract.functions.ownerOf.side_effect = owner_of_side_effect

        log_entries = [
            {"topics": [b"", b"", b"", burned_token_id.to_bytes(32, "big")]},
            {"topics": [b"", b"", b"", valid_token_id.to_bytes(32, "big")]},
        ]
        w3 = MagicMock()
        w3.eth.get_logs.side_effect = [log_entries, [], log_entries, []]

        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            _lp_account=SimpleNamespace(address=owner),
            _w3=w3,
        )
        adapter._get_owned_positions_from_logs = MethodType(
            V4PositionManager._get_owned_positions_from_logs,
            adapter,
        )

        first_owned = V4PositionManager.get_owned_positions(adapter)
        second_owned = V4PositionManager.get_owned_positions(adapter)

        assert first_owned == [valid_token_id]
        assert second_owned == [valid_token_id]
        assert getattr(adapter, "_token_index_lookup_supported") is False
        assert getattr(adapter, "_known_not_minted_token_ids") == {burned_token_id}
        assert position_manager_contract.functions.tokenOfOwnerByIndex.return_value.call.call_count == 1
        assert owner_lookup_calls == [burned_token_id, valid_token_id, valid_token_id]

    def test_static_position_metadata_uses_position_manager_liquidity_by_token_id(self):
        token_id = 77
        tick_lower = -120
        tick_upper = 120
        raw = ((tick_lower & 0xFFFFFF) << 8) | ((tick_upper & 0xFFFFFF) << 32)
        info_bytes32 = raw.to_bytes(32, "big")

        position_manager_contract = MagicMock()
        position_manager_contract.address = "0x" + "dd" * 20
        position_manager_contract.functions.getPoolAndPositionInfo.return_value.call.return_value = [
            ("0x" + "aa" * 20, "0x" + "bb" * 20, 1500, 30, "0x" + "00" * 20),
            info_bytes32,
        ]
        position_manager_contract.functions.getPositionLiquidity.return_value.call.return_value = 123456

        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            config=SimpleNamespace(
                pool_id="0x" + "ab" * 32,
                token0_decimals=6,
                token1_decimals=6,
                invert_price=False,
            ),
        )

        metadata = V4PositionManager._get_static_position_metadata(adapter, token_id)

        assert metadata is not None
        assert metadata.token_id == token_id
        assert metadata.liquidity == 123456
        position_manager_contract.functions.getPositionLiquidity.assert_called_once_with(token_id)

    def test_static_position_metadata_inverts_display_range_for_inverted_venues(self):
        token_id = 77
        tick_lower = -120
        tick_upper = 120
        raw = ((tick_lower & 0xFFFFFF) << 8) | ((tick_upper & 0xFFFFFF) << 32)
        info_bytes32 = raw.to_bytes(32, "big")

        position_manager_contract = MagicMock()
        position_manager_contract.address = "0x" + "dd" * 20
        position_manager_contract.functions.getPoolAndPositionInfo.return_value.call.return_value = [
            ("0x" + "aa" * 20, "0x" + "bb" * 20, 1200, 24, "0x" + "00" * 20),
            info_bytes32,
        ]
        position_manager_contract.functions.getPositionLiquidity.return_value.call.return_value = 123456

        adapter = SimpleNamespace(
            name="uni-bsc",
            _position_manager_contract=position_manager_contract,
            config=SimpleNamespace(
                pool_id="0x" + "ab" * 32,
                token0_decimals=18,
                token1_decimals=6,
                invert_price=True,
            ),
        )

        metadata = V4PositionManager._get_static_position_metadata(adapter, token_id)

        assert metadata is not None
        assert metadata.range_min < metadata.range_max

    def test_calculate_tick_range_inverts_prices_for_inverted_venues(self):
        params = SimpleNamespace(
            lookback_points=None,
            ewma_lambda=Decimal("0.975"),
            sd_multiplier=Decimal("3.0"),
            downside_skew=Decimal("0.5"),
            min_tick_width=100,
            max_tick_width=1000,
        )

        direct = strategy.calculate_tick_range(
            [Decimal("1400"), Decimal("1420"), Decimal("1410")],
            params,
            tick_spacing=24,
            token0_decimals=18,
            token1_decimals=6,
            invert_price=False,
            venue_name="direct",
        )
        inverted = strategy.calculate_tick_range(
            [Decimal("1") / Decimal("1400"), Decimal("1") / Decimal("1420"), Decimal("1") / Decimal("1410")],
            params,
            tick_spacing=24,
            token0_decimals=18,
            token1_decimals=6,
            invert_price=True,
            venue_name="inverted",
        )

        assert direct == inverted

    @pytest.mark.asyncio
    async def test_lp_token_approvals_include_permit2_for_position_manager(self):
        token0 = MagicMock()
        token0.functions.allowance.return_value.call.return_value = 2 ** 200
        token1 = MagicMock()
        token1.functions.allowance.return_value.call.return_value = 2 ** 200
        w3 = MagicMock()
        w3.eth.contract.side_effect = [token0, token1]
        tx_context = SimpleNamespace(
            _approve_token_to_permit2_if_needed=AsyncMock(),
            _approve_permit2_to_spender_if_needed=AsyncMock(),
        )
        adapter = SimpleNamespace(
            _position_manager_contract=object(),
            config=SimpleNamespace(
                token0_address="0x" + "aa" * 20,
                token1_address="0x" + "bb" * 20,
                position_manager="0x" + "dd" * 20,
            ),
            _lp_account=SimpleNamespace(address="0x" + "cc" * 20),
            _w3=w3,
            _tx=tx_context,
            _lp_approvals_done=set(),
            name="uni-base",
        )

        await V4PositionManager._approve_lp_tokens_if_needed(adapter)

        assert tx_context._approve_token_to_permit2_if_needed.await_count == 2
        assert tx_context._approve_permit2_to_spender_if_needed.await_count == 2
        spender_args = [call.args[1] for call in tx_context._approve_permit2_to_spender_if_needed.await_args_list]
        assert spender_args == ["0x" + "dd" * 20, "0x" + "dd" * 20]

    @pytest.mark.asyncio
    async def test_mint_position_encodes_expected_actions(self):
        position_manager_contract = MagicMock()
        position_manager_contract.functions.modifyLiquidities.return_value.build_transaction.return_value = {
            "from": "0x" + "cc" * 20,
            "to": "0x" + "dd" * 20,
            "data": "0xabc",
        }
        state_view = MagicMock()
        state_view.functions.getSlot0.return_value.call.return_value = [123456789, 0, 0, 0]
        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            _compute_liquidity_from_amounts=MagicMock(return_value=987654321),
            _resolve_pool_key=lambda: ("0x" + "aa" * 20, "0x" + "bb" * 20, 1500, 30, "0x" + "00" * 20),
            _lp_account=SimpleNamespace(address="0x" + "cc" * 20),
            _state_view=state_view,
            _w3=MagicMock(),
            _tx=SimpleNamespace(
                _get_tx_params=lambda account: {"from": account.address},
                _send_transaction=AsyncMock(return_value=TxResult(hash="0xmint", status="confirmed")),
            ),
            params=SimpleNamespace(max_slippage_percent=Decimal("1.0")),
            config=SimpleNamespace(pool_id="0x" + "ab" * 32, token0_decimals=6, token1_decimals=6),
        )
        adapter._w3.eth.get_block.return_value = {"timestamp": 100}
        adapter._w3.eth.estimate_gas.return_value = 100_000
        adapter._approve_lp_tokens_if_needed = AsyncMock()

        result = await V4PositionManager.mint_position(adapter, 1_000_000, 2_000_000, -120, 120)

        assert result.status == "confirmed"
        adapter._compute_liquidity_from_amounts.assert_called_once_with(
            123456789,
            -120,
            120,
            990_000,
            1_990_000,
        )
        unlock_data, deadline = position_manager_contract.functions.modifyLiquidities.call_args.args
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
        assert actions == bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR])
        assert len(params) == 2
        mint_params = decode(
            ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
            params[0],
        )
        _pool_key, _tick_lower, _tick_upper, liquidity, amount0_max, amount1_max, _recipient, _hook = mint_params
        assert liquidity == 987654321
        assert amount0_max == 1_000_000
        assert amount1_max == 2_000_000
        assert deadline == 400

    @pytest.mark.asyncio
    async def test_mint_position_caps_reserve_for_small_balances(self):
        position_manager_contract = MagicMock()
        position_manager_contract.functions.modifyLiquidities.return_value.build_transaction.return_value = {
            "from": "0x" + "cc" * 20,
            "to": "0x" + "dd" * 20,
            "data": "0xabc",
        }
        state_view = MagicMock()
        state_view.functions.getSlot0.return_value.call.return_value = [123456789, 0, 0, 0]

        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            _compute_liquidity_from_amounts=MagicMock(return_value=10),
            _resolve_pool_key=lambda: ("0x" + "aa" * 20, "0x" + "bb" * 20, 1500, 30, "0x" + "00" * 20),
            _lp_account=SimpleNamespace(address="0x" + "cc" * 20),
            _state_view=state_view,
            _w3=MagicMock(),
            _tx=SimpleNamespace(
                _get_tx_params=lambda account: {"from": account.address},
                _send_transaction=AsyncMock(return_value=TxResult(hash="0xmint", status="confirmed")),
            ),
            params=SimpleNamespace(max_slippage_percent=Decimal("0")),
            config=SimpleNamespace(pool_id="0x" + "ab" * 32, token0_decimals=6, token1_decimals=6),
        )
        adapter._w3.eth.get_block.return_value = {"timestamp": 100}
        adapter._w3.eth.estimate_gas.return_value = 100_000
        adapter._approve_lp_tokens_if_needed = AsyncMock()

        result = await V4PositionManager.mint_position(adapter, 100, 10_000, -120, 120)

        assert result.status == "confirmed"
        adapter._compute_liquidity_from_amounts.assert_called_once_with(
            123456789,
            -120,
            120,
            50,
            5_000,
        )
        unlock_data, _deadline = position_manager_contract.functions.modifyLiquidities.call_args.args
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
        assert actions == bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR])
        mint_params = decode(
            ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
            params[0],
        )
        _pool_key, _tick_lower, _tick_upper, liquidity, amount0_max, amount1_max, _recipient, _hook = mint_params
        assert liquidity == 10
        assert amount0_max == 100
        assert amount1_max == 10_000

    @pytest.mark.asyncio
    async def test_remove_position_encodes_expected_actions_for_live_liquidity(self):
        token_id = 77
        recipient = "0x" + "ee" * 20
        position_manager_contract = MagicMock()
        position_manager_contract.functions.modifyLiquidities.return_value.build_transaction.return_value = {
            "from": "0x" + "cc" * 20,
            "to": "0x" + "dd" * 20,
            "data": "0xabc",
        }
        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            _get_static_position_metadata=lambda _token_id: LPStaticPositionMetadata(
                token_id=token_id,
                liquidity=123456,
                tick_lower=-1000,
                tick_upper=1000,
                range_min=Decimal("0.9"),
                range_max=Decimal("1.1"),
            ),
            _resolve_pool_key=lambda: ("0x" + "aa" * 20, "0x" + "bb" * 20, 1500, 30, "0x" + "00" * 20),
            _lp_account=SimpleNamespace(address="0x" + "cc" * 20),
            _w3=MagicMock(),
            _tx=SimpleNamespace(
                _get_tx_params=lambda account: {"from": account.address},
                _send_transaction=AsyncMock(return_value=TxResult(hash="0xremove", status="confirmed")),
            ),
        )
        adapter._w3.eth.get_block.return_value = {"timestamp": 100}
        adapter._w3.eth.estimate_gas.return_value = 100_000

        result = await V4PositionManager.remove_position(adapter, token_id, recipient=recipient)

        assert result.status == "confirmed"
        unlock_data, deadline = position_manager_contract.functions.modifyLiquidities.call_args.args
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
        assert actions == bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_BURN_POSITION, _V4_LP_TAKE_PAIR])
        assert len(params) == 3
        assert deadline == 400

    @pytest.mark.asyncio
    async def test_remove_position_burns_zero_liquidity_shell(self):
        token_id = 77
        recipient = "0x" + "ee" * 20
        position_manager_contract = MagicMock()
        position_manager_contract.functions.modifyLiquidities.return_value.build_transaction.return_value = {
            "from": "0x" + "cc" * 20,
            "to": "0x" + "dd" * 20,
            "data": "0xabc",
        }
        adapter = SimpleNamespace(
            name="uni-base",
            _position_manager_contract=position_manager_contract,
            _get_static_position_metadata=lambda _token_id: LPStaticPositionMetadata(
                token_id=token_id,
                liquidity=0,
                tick_lower=-1000,
                tick_upper=1000,
                range_min=Decimal("0.9"),
                range_max=Decimal("1.1"),
            ),
            _resolve_pool_key=lambda: ("0x" + "aa" * 20, "0x" + "bb" * 20, 0, 0, "0x" + "00" * 20),
            _lp_account=SimpleNamespace(address="0x" + "cc" * 20),
            _w3=MagicMock(),
            _tx=SimpleNamespace(
                _get_tx_params=lambda account: {"from": account.address},
                _send_transaction=AsyncMock(return_value=TxResult(hash="0xremove", status="confirmed")),
            ),
        )
        adapter._w3.eth.get_block.return_value = {"timestamp": 100}
        adapter._w3.eth.estimate_gas.return_value = 100_000

        result = await V4PositionManager.remove_position(adapter, token_id, recipient=recipient)

        assert result.status == "confirmed"
        unlock_data, deadline = position_manager_contract.functions.modifyLiquidities.call_args.args
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
        assert actions == bytes([_V4_LP_BURN_POSITION])
        assert len(params) == 1
        assert deadline == 400


class TestV4GetPosition:
    @pytest.mark.asyncio
    async def test_get_position_keeps_lp_visible_when_snapshot_is_degraded(self):
        pos_state = _make_position_state(token_id=77, in_range=True)
        adapter = _make_lp_snapshot_adapter(
            [pos_state],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
            include_live_market=False,
        )

        result = await V4PositionManager.get_position_as_schema(adapter)

        assert result.lp_position is not None
        assert result.lp_position.snapshot_status == "degraded"
        assert result.balances == {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}


# =============================================================================
# LPRebalancer topup trigger logic
# =============================================================================


def _make_fake_venue(
    *,
    token_ids: list[int],
    position_state: "PositionState | None",
    amount0: int,
    amount1: int,
    increase_result: "TxResult",
    token0_symbol: str = "cNGN",
    token0_decimals: int = 18,
    token1_decimals: int = 6,
) -> SimpleNamespace:
    """Minimal fake LPVenueProtocol for rebalancer topup tests."""
    from engine.config import DexParams
    from decimal import Decimal

    venue = SimpleNamespace()
    venue.name = "uni-base"
    venue.params = DexParams(
        sd_multiplier=Decimal("2.75"),
        ewma_lambda=Decimal("0.975"),
        downside_skew=Decimal("0.45"),
        min_tick_width=100,
        max_tick_width=1000,
        rebalance_threshold_percent=Decimal("10"),
        max_slippage_percent=Decimal("1"),
    )
    venue.config = SimpleNamespace(
        token0_symbol=token0_symbol,
        token1_symbol="USDC",
        token0_address="0x" + "aa" * 20,
        token1_address="0x" + "bb" * 20,
        token0_decimals=token0_decimals,
        token1_decimals=token1_decimals,
    )
    venue.get_owned_positions = lambda: token_ids
    venue.get_position_state = lambda token_id: position_state
    venue.calculate_mint_amounts = lambda: (amount0, amount1)
    venue.prepare_lp_balance = AsyncMock(return_value=None)
    venue.increase_liquidity = AsyncMock(return_value=increase_result)
    venue.mint_position = AsyncMock(return_value=TxResult(hash="0xmint", status="confirmed"))
    venue.remove_position = AsyncMock(return_value=TxResult(hash="0xrm", status="confirmed"))
    return venue


def _make_rebalancer() -> "LPRebalancer":
    from engine.lp.rebalancer import LPRebalancer
    from unittest.mock import MagicMock, AsyncMock

    action_store = MagicMock()
    action_store.insert_action = AsyncMock()
    return LPRebalancer(
        broadcast=lambda _: None,
        price_store=MagicMock(),
        venue_config_store=MagicMock(),
        action_store=action_store,
    )


class TestTopupTrigger:
    """Unit tests for the idle-fund topup branch in LPRebalancer."""

    def _in_range_position(self) -> "PositionState":
        return _make_position_state(
            token_id=42,
            tick_lower=-1000,
            tick_upper=1000,
            current_price=Decimal("1"),
            in_range=True,
        )

    @pytest.mark.asyncio
    async def test_topup_fires_when_idle_cngn_exceeds_threshold(self):
        """If idle cNGN > lp_topup_threshold_cngn, increase_liquidity is called."""
        from engine.config import settings
        threshold_raw = int(settings.lp_topup_threshold_cngn * 10 ** 18)
        idle0 = threshold_raw + 1  # just above threshold

        venue = _make_fake_venue(
            token_ids=[42],
            position_state=self._in_range_position(),
            amount0=idle0,
            amount1=0,
            increase_result=TxResult(hash="0xtopup", status="confirmed"),
        )
        rebalancer = _make_rebalancer()
        await rebalancer._check_and_rebalance_locked(venue)
        venue.increase_liquidity.assert_called_once()

    @pytest.mark.asyncio
    async def test_topup_fires_when_idle_usdc_exceeds_threshold(self):
        """If idle USDC > lp_topup_threshold_usdc, increase_liquidity is called."""
        from engine.config import settings
        threshold_raw = int(settings.lp_topup_threshold_usdc * 10 ** 6)
        idle1 = threshold_raw + 1  # just above threshold

        venue = _make_fake_venue(
            token_ids=[42],
            position_state=self._in_range_position(),
            amount0=0,
            amount1=idle1,
            increase_result=TxResult(hash="0xtopup", status="confirmed"),
        )
        rebalancer = _make_rebalancer()
        await rebalancer._check_and_rebalance_locked(venue)
        venue.increase_liquidity.assert_called_once()

    @pytest.mark.asyncio
    async def test_topup_does_not_fire_below_threshold(self):
        """Idle funds below both thresholds: increase_liquidity must NOT be called."""
        from engine.config import settings
        below0 = int(settings.lp_topup_threshold_cngn * 10 ** 18) - 1
        below1 = int(settings.lp_topup_threshold_usdc * 10 ** 6) - 1

        venue = _make_fake_venue(
            token_ids=[42],
            position_state=self._in_range_position(),
            amount0=below0,
            amount1=below1,
            increase_result=TxResult(hash="0xtopup", status="confirmed"),
        )
        rebalancer = _make_rebalancer()
        await rebalancer._check_and_rebalance_locked(venue)
        venue.increase_liquidity.assert_not_called()

    @pytest.mark.asyncio
    async def test_topup_not_triggered_when_out_of_range(self):
        """Out-of-range position with idle funds goes through rebalance, not topup."""
        from engine.config import settings
        idle0 = int(settings.lp_topup_threshold_cngn * 10 ** 18) * 10  # far above threshold

        out_of_range_pos = _make_position_state(
            token_id=42,
            tick_lower=-1000,
            tick_upper=1000,
            current_price=Decimal("10"),  # well above upper range
            in_range=False,
        )
        # Need a proper price_upper for out-of-range distance calc
        from engine.venues.dex.shared import PositionState as PS
        out_of_range_pos.price_upper = Decimal("1.1")
        out_of_range_pos.price_lower = Decimal("0.9")

        venue = _make_fake_venue(
            token_ids=[42],
            position_state=out_of_range_pos,
            amount0=idle0,
            amount1=0,
            increase_result=TxResult(hash="0xtopup", status="confirmed"),
        )
        # remove_position needs a valid TxResult; mint_position also
        rebalancer = _make_rebalancer()
        await rebalancer._check_and_rebalance_locked(venue)
        venue.increase_liquidity.assert_not_called()
