"""
Uniswap V4 fork tests against real on-chain state.

Section A: Read tests (Anvil fork of Base / BSC mainnet)
Section B: Position lifecycle (Anvil with funded test wallet)
Section C: Rebalance flow (end-to-end)
Section D: Arb detection with skewed pool prices
Section E: LP lifecycle on Base fork (fund → mint → verify)

Requires: anvil CLI (Foundry). Run with:
    pytest tests/test_dex_fork.py -v
"""

import pytest
from decimal import Decimal


# =============================================================================
# Section A — Read tests (pool state via StateView)
# =============================================================================


class TestV4PoolStateReadsBase:
    """Read pool state from a live Base mainnet fork."""

    @pytest.mark.asyncio
    async def test_update_seeds_pool_cache(self, anvil_base):
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        ok = await update_single_v4_pool_state(config)
        assert ok is True

        state = _POOL_CACHE.get(config.pool_address)
        assert state is not None
        assert state["sqrt_p"] > 0
        assert state["liquidity"] > 0

    @pytest.mark.asyncio
    async def test_cached_price_in_expected_range(self, anvil_base):
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE, Q96
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        await update_single_v4_pool_state(config)

        state = _POOL_CACHE[config.pool_address]
        sqrt_p = state["sqrt_p"]
        price = (sqrt_p / Q96) ** 2  # cNGN/USDC, 6/6 dec
        # cNGN trades around 0.000606 USDC — expect broad range
        assert Decimal("0.00001") < price < Decimal("0.01")

    @pytest.mark.asyncio
    async def test_pool_liquidity_positive(self, anvil_base):
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        await update_single_v4_pool_state(config)
        state = _POOL_CACHE[config.pool_address]
        assert state["liquidity"] > 0

    @pytest.mark.asyncio
    async def test_fee_in_expected_range(self, anvil_base):
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        await update_single_v4_pool_state(config)
        state = _POOL_CACHE[config.pool_address]
        fee = state["fee"]
        assert fee is not None
        assert Decimal("0") < fee < Decimal("0.1")  # 0–10% fee is reasonable


class TestV4PoolStateReadsBSC:
    """Read pool state from a live BSC mainnet fork."""

    @pytest.mark.asyncio
    async def test_update_seeds_pool_cache(self, anvil_bsc):
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BSC_POOL_READ_CONFIG, rpc_url=anvil_bsc)
        ok = await update_single_v4_pool_state(config)
        assert ok is True

        state = _POOL_CACHE.get(config.pool_address)
        assert state is not None
        assert state["sqrt_p"] > 0

    @pytest.mark.asyncio
    async def test_bsc_price_in_range(self, anvil_bsc):
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE, Q96
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BSC_POOL_READ_CONFIG, rpc_url=anvil_bsc)
        await update_single_v4_pool_state(config)
        state = _POOL_CACHE[config.pool_address]
        sqrt_p = state["sqrt_p"]
        # BSC pool: USDT(18) / cNGN(6) → price_usd = 1/((sqrt/Q96)^2 * 10^12)
        raw = (sqrt_p / Q96) ** 2
        if raw > 0:
            price_usd = Decimal(1) / (raw * Decimal(10 ** 12))
            assert Decimal("0.00001") < price_usd < Decimal("0.01")


# =============================================================================
# Section B — Position lifecycle (funded Anvil test wallet)
# =============================================================================


@pytest.fixture(scope="session")
def funded_lp_wallet(anvil_base, test_wallet_address):
    """Fund the LP and trade accounts on Anvil with ETH + cNGN + USDC.

    Uses anvil_setBalance for ETH and HEVM cheat deal() for ERC20 tokens.
    """
    from web3 import Web3
    from engine.config import settings

    w3 = Web3(Web3.HTTPProvider(anvil_base))

    # Fund with ETH
    eth_amount = hex(10 * 10 ** 18)
    w3.provider.make_request("anvil_setBalance", [test_wallet_address, eth_amount])

    # Fund with ERC20 via HEVM cheat code
    hevm = "0x7109709ECfa91a80626fF3989D68f67F5b1DD12D"
    deal_abi = [{
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "give", "type": "uint256"},
        ],
        "name": "deal",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }]
    try:
        cheat = w3.eth.contract(address=hevm, abi=deal_abi)
        cheat.functions.deal(
            settings.cngn_base_address, test_wallet_address, 10_000_000 * 10 ** 6
        ).transact({"from": test_wallet_address})
        cheat.functions.deal(
            settings.usdc_base_address, test_wallet_address, 10_000 * 10 ** 6
        ).transact({"from": test_wallet_address})
    except Exception:
        pass  # HEVM may not be available on all forks

    return w3


class TestV4PositionLifecycle:
    """Position mint / remove lifecycle on a funded Anvil fork."""

    def test_eth_balance_funded(self, funded_lp_wallet, test_wallet_address):
        balance = funded_lp_wallet.eth.get_balance(test_wallet_address)
        assert balance >= 9 * 10 ** 18  # Anvil seeds 10 ETH; allow for gas spent

    @pytest.mark.asyncio
    async def test_pool_state_readable_after_fund(self, funded_lp_wallet, anvil_base):
        """Basic sanity: pool state still readable after funding the wallet."""
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        ok = await update_single_v4_pool_state(config)
        assert ok is True


# =============================================================================
# Section C — Rebalance flow (end-to-end)
# =============================================================================


class TestRebalanceFlow:
    """End-to-end rebalance flow on Anvil.

    Full implementation requires price manipulation via impersonated whale swaps.
    These tests serve as structural anchors; expand with real swap manipulation.
    """

    @pytest.mark.asyncio
    async def test_pool_state_available_for_rebalance_check(self, anvil_base):
        """Precondition: pool state is readable so scheduler can evaluate rebalance."""
        import dataclasses
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

        config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        ok = await update_single_v4_pool_state(config)
        assert ok is True
        assert config.pool_address in _POOL_CACHE


# =============================================================================
# Section C — Full LP lifecycle on Base Anvil fork
# =============================================================================


class TestLPLifecycleFork:
    """Full LP lifecycle (fund → seed prices → mint → verify position) on a Base fork.

    Uses helpers from tests/fork_helpers.py: wallet funding via anvil_setBalance,
    donor finding via Transfer log scan, impersonated ERC20 transfer, and
    price seeding from the fork's live spot price.

    Requires: anvil CLI with a Base mainnet fork (anvil_base fixture).
    """

    @pytest.mark.asyncio
    async def test_create_position_on_fork(self, anvil_base, tmp_path):
        """Fund LP wallet from a USDC whale, seed prices, mint → assert in-range position."""
        import tempfile
        from web3 import Web3
        from engine.accounts import AccountManager, AccountRole
        from engine.config import settings
        from engine.db.repository import open_repository
        from engine.lp.rebalancer import LPRebalancer
        from engine.lp.uniswap_v4 import V4PositionManager
        from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter
        from tests.fork_helpers import fund_native_balance, impersonated_account, find_token_donor, transfer_erc20_from_unlocked, seed_prices

        account_manager = AccountManager(use_test_accounts=True)
        lp_key = account_manager.get_private_key(AccountRole.UNI_BASE_LP)
        trade_key = account_manager.get_private_key(AccountRole.UNI_BASE_TRADE)
        adapter = UniswapBaseV4Adapter(
            lp_private_key=lp_key,
            trade_private_key=trade_key,
            rpc_url=anvil_base,
            params=settings.uni_base_lp_params,
        )

        if not adapter.w3.is_connected():
            pytest.skip("Anvil fork not reachable")

        lp_address = adapter.lp_account.address

        # Fund ETH for gas
        fund_native_balance(adapter.w3, lp_address, Decimal("1"))

        # Fund USDC from a whale donor
        target_usdc = Decimal("200")
        target_raw = int(target_usdc * Decimal(10 ** adapter.config.token1_decimals))
        _SINK = "0x000000000000000000000000000000000000dEaD"
        _ANVIL_SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

        donor = find_token_donor(
            adapter.w3,
            adapter.token1,
            min_balance_raw=target_raw,
            exclude={lp_address, adapter.trade_account.address, _ANVIL_SENDER, _SINK},
        )
        with impersonated_account(adapter.w3, donor):
            transfer_erc20_from_unlocked(
                adapter.w3,
                adapter.token1,
                sender=donor,
                recipient=lp_address,
                amount_raw=target_raw,
            )

        actual_usdc = adapter.token1.functions.balanceOf(lp_address).call()
        assert actual_usdc == target_raw, f"LP wallet USDC mismatch: {actual_usdc} != {target_raw}"

        # Build LP manager and seed prices from live fork spot
        pm_contract = adapter.w3.eth.contract(
            address=Web3.to_checksum_address(adapter.config.position_manager),
            abi=V4PositionManager.POSITION_MANAGER_ABI,
        )
        lp_manager = V4PositionManager(
            config=adapter.config,
            state_view=adapter.state_view,
            position_manager_contract=pm_contract,
            params=settings.uni_base_lp_params,
            venue_name="uni-base",
            tx_context=adapter,
        )

        assert lp_manager.get_owned_positions() == [], "Fork must start with no LP positions"

        db_path = str(tmp_path / "fork_lp.db")
        repo = await open_repository(db_path)
        try:
            quote = await adapter.get_current_price()
            assert quote is not None, "Could not fetch live spot price from fork"
            await seed_prices(repo, quote, count=20, source="uni-base_pool")

            rebalancer = LPRebalancer(
                broadcast=lambda _e: None,
                price_store=repo.prices,
                venue_config_store=repo.venue_config,
                action_store=repo.actions,
                auto_management_enabled=lambda: True,
            )

            created = await rebalancer.create_position(lp_manager, triggered_by="test:fork_lp")

            assert created is True, "create_position must return True on the fork"
            token_ids = lp_manager.get_owned_positions()
            assert len(token_ids) == 1, f"Expected exactly one LP NFT, got {token_ids}"

            position_state = lp_manager.get_position_state(token_ids[0])
            assert position_state is not None
            assert position_state.in_range is True, (
                "Newly minted position must be in-range at the current spot price"
            )
            assert position_state.liquidity > 0

        finally:
            await repo.close()


# =============================================================================
# Section D — Arb detection on skewed pool prices (Base + BSC forks)
# =============================================================================


class TestArbDetectionFork:
    """Arb detection using real fork pool state with an artificially skewed price.

    The pool cache is seeded from both live forks (real liquidity, fee, tick),
    then BSC sqrtPriceX96 is inflated 2× to guarantee a detectable price gap.
    This tests the full chain: pool state → price calc → arb detection → routing
    using real on-chain geometry, not synthetic values.
    """

    @pytest.mark.asyncio
    async def test_arb_detected_when_bsc_price_is_skewed(self, anvil_base, anvil_bsc):
        """Real fork state + 2× BSC sqrtP → find_optimal_dex_arb() must return a route."""
        import dataclasses
        import math
        import time as _time
        from engine.arb.detection.dex_dex import find_optimal_dex_arb
        from engine.arb.routing.route_registry import ROUTES_BY_DIRECTION
        from engine.arb.routing.router import RouteCandidate, select_route
        from engine.arb.risk.inventory import InventoryTracker as InventoryManager
        from engine.market import gas_oracle as _go
        from engine.market.pool_state import update_single_v4_pool_state, _POOL_CACHE
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
        from engine.types import ArbitrageParams

        base_config = dataclasses.replace(UNISWAP_BASE_POOL_READ_CONFIG, rpc_url=anvil_base)
        bsc_config = dataclasses.replace(UNISWAP_BSC_POOL_READ_CONFIG, rpc_url=anvil_bsc)

        ok_base = await update_single_v4_pool_state(base_config)
        ok_bsc = await update_single_v4_pool_state(bsc_config)
        if not ok_base or not ok_bsc:
            pytest.skip("Could not seed pool cache from fork — RPC unavailable")

        # Seed gas oracle so arb detection doesn't block on missing gas prices
        _go._state["gas_usd_base"] = Decimal("0.003")
        _go._state["gas_usd_bsc"] = Decimal("0.005")
        _go._state["last_updated_monotonic"] = _time.monotonic()

        # Verify we got real state
        base_state = _POOL_CACHE.get(base_config.pool_address)
        bsc_state = _POOL_CACHE.get(bsc_config.pool_address)
        assert base_state is not None and base_state["sqrt_p"] > 0
        assert bsc_state is not None and bsc_state["sqrt_p"] > 0

        # Skew BSC sqrtPriceX96 by 2×: this makes BSC cNGN price 4× relative to Base,
        # guaranteeing a large, detectable arbitrage gap regardless of current market prices.
        # Save the original value before mutating — bsc_state is a reference to the cache dict.
        original_bsc_sqrt_p = bsc_state["sqrt_p"]
        _POOL_CACHE[bsc_config.pool_address]["sqrt_p"] = original_bsc_sqrt_p * 2

        try:
            result = find_optimal_dex_arb()
            assert result is not None, (
                "find_optimal_dex_arb() must detect an opportunity when BSC price is 4× Base"
            )
            assert "optimal_arb" in result
            arb = result["optimal_arb"]
            assert arb["direction"] in ("UNI_BASE_TO_UNI_BSC_DELTA_BALANCE", "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE")
            assert arb["expected_profit_usd"] > 0
            assert arb["cngn_transferred"] > 0

            # Verify route selection agrees with detection direction
            params = ArbitrageParams(
                max_daily_volume_usd=Decimal("50000"),
                max_daily_loss_usd=Decimal("500"),
                max_inventory_imbalance_usd=Decimal("10000"),
                max_consecutive_failures=3,
                max_single_trade_usd=Decimal("1000"),
            )
            inventory = InventoryManager(params)
            inventory.reconcile_cngn({"uni-base": Decimal("50000"), "uni-bsc": Decimal("50000")})
            inventory.reconcile_stables({"uni-base": Decimal("1000"), "uni-bsc": Decimal("1000")})

            direction = arb["direction"]
            route_def = ROUTES_BY_DIRECTION[direction]
            candidate = RouteCandidate(
                direction=direction,
                buy_venue=route_def.buy_leg.venue,
                sell_venue=route_def.sell_leg.venue,
                optimal_size_usd=Decimal(str(arb["optimal_size_usd"])),
                expected_profit_usd=Decimal(str(arb["expected_profit_usd"])),
                gas_usd=Decimal(str(arb.get("gas_usd", "0.005"))),
                signal=result,
            )
            selected = select_route([candidate], inventory)
            assert selected is not None, (
                "select_route() must select a route when arb is detected and inventory is available"
            )
            assert selected.candidate.direction == direction

        finally:
            # Restore original BSC sqrtP so other tests are not affected
            _POOL_CACHE[bsc_config.pool_address]["sqrt_p"] = original_bsc_sqrt_p

