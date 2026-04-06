"""Unit tests for compute_required_ratio and prepare_lp_balance."""

import math
from decimal import Decimal
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from eth_abi import decode  # type: ignore[attr-defined]
import pytest

from engine.api.schemas import TxResult
from engine.venues.dex.shared import _Q96, PositionState, compute_required_ratio
from engine.venues.dex.lp_v4 import (
    LPBalanceSwapResult,
    LPMarketSnapshot,
    LPStaticPositionMetadata,
    V4LPAdapter,
)


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
    """Minimal self-mock for prepare_lp_balance."""
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

    adapter = SimpleNamespace(
        config=SimpleNamespace(
            pool_id=pool_id,
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            token0_address=token0_addr,
            token1_address=token1_addr,
            invert_price=False,
        ),
        name="uni-base",
        lp_account=SimpleNamespace(address=lp_addr),
        state_view=state_view,
        token0=token0,
        token1=token1,
        _swap_from_lp=AsyncMock(
            return_value=swap_result or TxResult(hash="0xswap", status="confirmed")
        ),
    )
    adapter._compute_required_ratio = lambda tl, tu, sp: compute_required_ratio(
        tl, tu, sp, adapter.config.token0_decimals, adapter.config.token1_decimals
    )
    return adapter


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
        lp_account=SimpleNamespace(address="0x" + "cc" * 20),
        position_manager_contract=object(),
        state_view=MagicMock(),
    )
    adapter._compute_lp_token_amounts = MethodType(V4LPAdapter._compute_lp_token_amounts, adapter)
    adapter._compute_lp_token_amounts_from_metadata = MethodType(
        V4LPAdapter._compute_lp_token_amounts_from_metadata,
        adapter,
    )
    adapter._build_position_state_from_metadata = MethodType(
        V4LPAdapter._build_position_state_from_metadata,
        adapter,
    )
    adapter._build_lp_position_snapshot = MethodType(V4LPAdapter._build_lp_position_snapshot, adapter)
    adapter._build_degraded_lp_snapshot = MethodType(V4LPAdapter._build_degraded_lp_snapshot, adapter)
    adapter._empty_position_balances = MethodType(V4LPAdapter._empty_position_balances, adapter)
    adapter._add_symbol_balance = MethodType(V4LPAdapter._add_symbol_balance, adapter)
    adapter.get_portfolio_balances = MethodType(V4LPAdapter.get_portfolio_balances, adapter)
    adapter.get_active_lp_position_snapshot = MethodType(V4LPAdapter.get_active_lp_position_snapshot, adapter)
    adapter.get_position = MethodType(V4LPAdapter.get_position, adapter)
    return adapter


class TestComputeRequiredRatio:
    """Tests for shared.compute_required_ratio."""

    def test_symmetric_range_price_at_midpoint(self):
        """When price is inside the range, both r0 and r1 are non-zero."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        r0, r1 = compute_required_ratio(-1000, 1000, sqrt_p, 6, 6)
        assert r0 > 0
        assert r1 > 0

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
    """Tests for V4LPAdapter.prepare_lp_balance via mocked RPC."""

    @pytest.mark.asyncio
    async def test_no_swap_when_empty(self):
        """Both balances zero: returns immediately, no swap."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        adapter = _make_balance_adapter(sqrt_p, 0, 0)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_swap_when_balanced(self):
        """Balances already at target ratio (within 1%): no swap triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        # Symmetric range at price ≈ 1: target is approximately 50/50 by value.
        # Fund with equal raw amounts — should be close enough to skip the swap.
        adapter = _make_balance_adapter(sqrt_p, 500_000_000, 500_000_000)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_not_called()

    @pytest.mark.asyncio
    async def test_swap_token0_to_token1_when_excess_token0(self):
        """All token0, none of token1: surplus token0 swap is triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        # Entirely in token0, none in token1 — large imbalance
        adapter = _make_balance_adapter(sqrt_p, 1_000_000_000, 0)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        call_args = adapter._swap_from_lp.call_args[0]
        assert call_args[0].lower() == adapter.config.token0_address.lower()

    @pytest.mark.asyncio
    async def test_swap_token1_to_token0_when_excess_token1(self):
        """All token1, none of token0: surplus token1 swap is triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        adapter = _make_balance_adapter(sqrt_p, 0, 1_000_000_000)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        call_args = adapter._swap_from_lp.call_args[0]
        assert call_args[0].lower() == adapter.config.token1_address.lower()

    @pytest.mark.asyncio
    async def test_failed_swap_logs_warning_and_returns(self):
        """If the ratio swap fails, prepare_lp_balance logs and returns False."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        failed = TxResult(hash="", status="failed", error="reverted")
        adapter = _make_balance_adapter(sqrt_p, 1_000_000_000, 0, swap_result=failed)
        result = await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
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
        await V4LPAdapter.prepare_lp_balance(adapter, tick_lower, tick_upper)
        # Should not have swapped token1→token0 (that would be wrong direction)
        if adapter._swap_from_lp.called:
            call_args = adapter._swap_from_lp.call_args[0]
            assert call_args[0].lower() == adapter.config.token0_address.lower(), \
                "Wrong swap direction: target1 went negative, triggering spurious token1→token0 swap"


class TestActiveLpPositionSnapshot:
    """Tests for V4LPAdapter.get_active_lp_position_snapshot."""

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
            state_view=state_view,
        )

        result = V4LPAdapter._get_live_pool_snapshot(adapter)

        assert result is not None
        assert result.current_tick == 0
        assert result.current_price == Decimal("1")
        assert result.pool_liquidity is None

    def test_returns_none_when_no_active_position(self):
        adapter = _make_lp_snapshot_adapter(
            [],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
            include_live_market=False,
        )

        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

        assert result is None

    def test_snapshot_below_range_is_all_token0(self):
        pos_state = _make_position_state(tick_lower=1000, tick_upper=2000, in_range=False)
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(0.5))
        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token0_amount > 0
        assert result.token1_amount == 0
        assert result.snapshot_status == "live"

    def test_snapshot_above_range_is_all_token1(self):
        pos_state = _make_position_state(tick_lower=-2000, tick_upper=-1000, in_range=False)
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(2.0))
        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token0_amount == 0
        assert result.token1_amount > 0

    def test_snapshot_in_range_has_both_tokens(self):
        pos_state = _make_position_state(in_range=True)
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(1.0))
        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

        assert result is not None
        assert result.token0_amount > 0
        assert result.token1_amount > 0
        assert result.snapshot_status == "live"

    def test_snapshot_returns_degraded_summary_for_multiple_positions(self):
        adapter = _make_lp_snapshot_adapter(
            [_make_position_state(token_id=77), _make_position_state(token_id=78)],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
        )

        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

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

        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

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

        result = V4LPAdapter.get_active_lp_position_snapshot(adapter)

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

        balances = V4LPAdapter.get_portfolio_balances(adapter)

        assert balances["cngn"] > 0
        assert balances["usdc"] > 0
        assert balances["usdt"] == 0

    def test_portfolio_balances_skip_multiple_positions(self):
        adapter = _make_lp_snapshot_adapter(
            [_make_position_state(token_id=77), _make_position_state(token_id=78)],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
        )

        balances = V4LPAdapter.get_portfolio_balances(adapter)

        assert balances == {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}

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
            position_manager_contract=position_manager_contract,
            _get_static_position_metadata=lambda _token_id: LPStaticPositionMetadata(
                token_id=token_id,
                liquidity=0,
                tick_lower=-1000,
                tick_upper=1000,
                range_min=Decimal("0.9"),
                range_max=Decimal("1.1"),
            ),
            _resolve_pool_key=lambda: ("0x" + "aa" * 20, "0x" + "bb" * 20, 0, 0, "0x" + "00" * 20),
            lp_account=SimpleNamespace(address="0x" + "cc" * 20),
            w3=MagicMock(),
            _get_tx_params=lambda account: {"from": account.address},
            _send_transaction=AsyncMock(return_value=TxResult(hash="0xremove", status="confirmed")),
        )
        adapter.w3.eth.get_block.return_value = {"timestamp": 100}
        adapter.w3.eth.estimate_gas.return_value = 100_000

        result = await V4LPAdapter.remove_position(adapter, token_id, recipient=recipient)

        assert result.status == "confirmed"
        unlock_data, deadline = position_manager_contract.functions.modifyLiquidities.call_args.args
        actions, params = decode(["bytes", "bytes[]"], unlock_data)
        assert actions == bytes([3])
        assert len(params) == 1
        assert deadline == 400


class TestV4GetPosition:
    @pytest.mark.asyncio
    async def test_get_position_returns_deployed_lp_only(self):
        pos_state = _make_position_state(token_id=77, in_range=True)
        adapter = _make_lp_snapshot_adapter([pos_state], sqrt_price_x96=_price_to_sqrt_x96(1.0))

        result = await V4LPAdapter.get_position(adapter)

        assert result.balances["cngn"] > 0
        assert result.balances["usdc"] > 0
        assert result.lp_position is not None
        assert result.lp_position.token_id == "77"
        assert result.lp_position.snapshot_status == "live"

    @pytest.mark.asyncio
    async def test_get_position_returns_zero_balances_when_no_nft(self):
        adapter = _make_lp_snapshot_adapter([], sqrt_price_x96=_price_to_sqrt_x96(1.0))

        result = await V4LPAdapter.get_position(adapter)

        assert result.balances == {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}
        assert result.lp_position is None

    @pytest.mark.asyncio
    async def test_get_position_keeps_lp_visible_when_snapshot_is_degraded(self):
        pos_state = _make_position_state(token_id=77, in_range=True)
        adapter = _make_lp_snapshot_adapter(
            [pos_state],
            sqrt_price_x96=_price_to_sqrt_x96(1.0),
            include_live_market=False,
        )

        result = await V4LPAdapter.get_position(adapter)

        assert result.lp_position is not None
        assert result.lp_position.snapshot_status == "degraded"
        assert result.balances == {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}
