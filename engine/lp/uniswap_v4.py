"""Uniswap V4 LP position manager — owns all LP business logic."""

from __future__ import annotations

import time as _time
from decimal import Decimal
from typing import Any, Optional, Protocol, TYPE_CHECKING

import structlog
from eth_abi import encode  # type: ignore[attr-defined]
from web3 import Web3
from web3.types import TxParams, Wei

from engine.types import LPPosition, Position, TxResult
from engine.config import DexParams
from engine.lp.types import (
    LPBalanceSwapResult,
    LPMarketSnapshot,
    LPPositionSnapshot,
    LPStaticPositionMetadata,
    _V4_LP_BURN_POSITION,
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_SETTLE_PAIR,
    _V4_LP_TAKE_PAIR,
)
from engine.venues.dex.shared import (
    ERC20_ABI,
    PositionState,
    _Q96,
    _tick_to_sqrt_x96,
    compute_required_ratio,
    sqrt_price_x96_to_decimal,
    tick_to_price,
)

if TYPE_CHECKING:
    from engine.venues.dex.v4 import V4ExecutionConfig
    from eth_account.signers.local import LocalAccount

logger = structlog.get_logger()

_TRANSFER_EVENT_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()


POSITION_MANAGER_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getPositionLiquidity",
        "outputs": [{"name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getPositionInfo",
        "outputs": [
            {
                "components": [
                    {"name": "currency0", "type": "address"},
                    {"name": "currency1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "hooks", "type": "address"},
                ],
                "name": "poolKey",
                "type": "tuple",
            },
            {"name": "info", "type": "bytes32"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "unlockData", "type": "bytes"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "modifyLiquidities",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]


def _sign_extend_24(v: int) -> int:
    """Sign-extend a 24-bit integer."""
    if v & 0x800000:
        return v - 0x1000000
    return v


def _decode_position_info(info_bytes32: bytes) -> tuple[int, int]:
    """Decode tickLower and tickUpper from PositionInfo bytes32.

    Layout: bits 8-31 = tickLower (24 bits), bits 32-55 = tickUpper (24 bits).
    """
    raw = int.from_bytes(info_bytes32, "big")
    tick_lower = _sign_extend_24((raw >> 8) & 0xFFFFFF)
    tick_upper = _sign_extend_24((raw >> 32) & 0xFFFFFF)
    return tick_lower, tick_upper


class V4TxContext(Protocol):
    """Transaction capabilities that V4PositionManager needs from the swap adapter.

    BaseV4DexAdapter satisfies this protocol structurally.
    """

    lp_account: "LocalAccount"
    w3: Any  # web3.Web3

    def _get_tx_params(
        self,
        account: "LocalAccount",
        block: Any = None,
    ) -> TxParams: ...

    async def _send_transaction(
        self,
        tx: TxParams,
        account: "LocalAccount",
        *,
        output_token: str | None = None,
    ) -> TxResult: ...

    def _build_swap_tx(
        self,
        token_in: str,
        amount_in: int,
        min_amount_out: int,
        *,
        account: "LocalAccount | None" = None,
    ) -> tuple[TxParams, int]: ...

    async def _approve_token_to_permit2_if_needed(
        self,
        token: str,
        *,
        account: "LocalAccount | None" = None,
    ) -> None: ...

    async def _approve_permit2_to_router_if_needed(
        self,
        token: str,
        *,
        account: "LocalAccount | None" = None,
    ) -> None: ...

    async def _approve_permit2_to_spender_if_needed(
        self,
        token: str,
        spender: str,
        *,
        account: "LocalAccount | None" = None,
    ) -> None: ...


class LPVenueProtocol(Protocol):
    """What LPRebalancer needs from an LP position manager."""

    name: str
    params: DexParams
    config: "V4ExecutionConfig"

    def get_owned_positions(self) -> list[int]: ...
    def get_position_state(self, token_id: int) -> "PositionState | None": ...
    def calculate_mint_amounts(self) -> tuple[int, int]: ...

    async def prepare_lp_balance(
        self,
        tick_lower: int,
        tick_upper: int,
    ) -> "LPBalanceSwapResult | None": ...

    async def mint_position(
        self,
        amount0: int,
        amount1: int,
        tick_lower: int,
        tick_upper: int,
    ) -> TxResult: ...

    async def remove_position(
        self,
        token_id: int,
        recipient: str | None = None,
    ) -> TxResult: ...


class V4PositionManager:
    """Manages all LP position operations for a single Uniswap V4 pool.

    Owns: position discovery, snapshot building, portfolio balance extraction,
    mint / remove / ratio-prep-swap, and LP-specific approval tracking.

    Write operations delegate transaction mechanics to the injected tx_context
    (a BaseV4DexAdapter instance).
    """

    POSITION_MANAGER_ABI = POSITION_MANAGER_ABI

    def __init__(
        self,
        config: "V4ExecutionConfig",
        state_view: Any,
        position_manager_contract: Any | None,
        params: DexParams,
        venue_name: str,
        tx_context: V4TxContext,
    ) -> None:
        self.config = config
        self._state_view = state_view
        self._position_manager_contract = position_manager_contract
        self.params = params
        self.name = venue_name
        self._tx = tx_context
        self._w3 = tx_context.w3
        self._lp_account = tx_context.lp_account
        self._lp_approvals_done: set[str] = set()
        self._pool_key: tuple[str, str, int, int, str] | None = None

    # === Pool key ===

    def _resolve_pool_key(self) -> tuple[str, str, int, int, str]:
        if self._pool_key is None:
            self._pool_key = self.config.resolve_pool_key()
        return self._pool_key

    # === Position queries ===

    def get_owned_positions(self) -> list[int]:
        """Get all position token IDs owned by LP wallet via ERC721 on PositionManager."""
        if not self._position_manager_contract:
            return []
        try:
            balance = self._position_manager_contract.functions.balanceOf(
                self._lp_account.address
            ).call()
            if balance == 0:
                return []
            try:
                return [
                    self._position_manager_contract.functions.tokenOfOwnerByIndex(
                        self._lp_account.address, i
                    ).call()
                    for i in range(balance)
                ]
            except Exception as e:
                logger.warning(
                    "token_of_owner_by_index_failed_falling_back_to_logs",
                    venue=self.name,
                    error=str(e),
                )
                return self._get_owned_positions_from_logs(expected_balance=balance)
        except Exception as e:
            logger.warning("get_owned_positions_failed", venue=self.name, error=str(e))
            return []

    def _get_owned_positions_from_logs(self, *, expected_balance: int | None = None) -> list[int]:
        """Fallback ownership discovery for non-enumerable PositionManager NFTs."""
        if not self._position_manager_contract:
            return []

        owner = Web3.to_checksum_address(self._lp_account.address)
        owner_topic = "0x" + owner[2:].lower().rjust(64, "0")

        try:
            incoming = self._w3.eth.get_logs({
                "address": self._position_manager_contract.address,
                "fromBlock": 0,
                "toBlock": "latest",
                "topics": [_TRANSFER_EVENT_TOPIC, None, owner_topic],
            })
            outgoing = self._w3.eth.get_logs({
                "address": self._position_manager_contract.address,
                "fromBlock": 0,
                "toBlock": "latest",
                "topics": [_TRANSFER_EVENT_TOPIC, owner_topic],
            })
        except Exception as e:
            logger.warning("get_owned_positions_logs_failed", venue=self.name, error=str(e))
            return []

        candidate_ids: set[int] = set()
        for log in incoming:
            if len(log["topics"]) > 3:
                candidate_ids.add(int(log["topics"][3].hex(), 16))
        for log in outgoing:
            if len(log["topics"]) > 3:
                candidate_ids.add(int(log["topics"][3].hex(), 16))

        owned: list[int] = []
        for token_id in sorted(candidate_ids):
            try:
                current_owner = self._position_manager_contract.functions.ownerOf(token_id).call()
            except Exception as e:
                logger.warning(
                    "position_owner_lookup_failed",
                    venue=self.name,
                    token_id=token_id,
                    error=str(e),
                )
                continue
            if Web3.to_checksum_address(current_owner) == owner:
                owned.append(token_id)

        if expected_balance is not None and len(owned) != expected_balance:
            logger.warning(
                "position_balance_mismatch_after_log_fallback",
                venue=self.name,
                expected_balance=expected_balance,
                discovered=len(owned),
                token_ids=owned,
            )

        return owned

    def _get_live_pool_snapshot(self) -> LPMarketSnapshot | None:
        """Return the latest live market state needed to evaluate LP positions."""
        try:
            pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
            slot0 = self._state_view.functions.getSlot0(pool_id_bytes).call()
            sqrt_price_x96 = Decimal(int(slot0[0]))
            current_tick = int(slot0[1])
            current_price = sqrt_price_x96_to_decimal(
                int(sqrt_price_x96),
                self.config.token0_decimals,
                self.config.token1_decimals,
            )
            if self.config.invert_price and current_price > 0:
                current_price = Decimal(1) / current_price

            pool_liquidity: Decimal | None
            try:
                pool_liquidity = Decimal(
                    self._state_view.functions.getLiquidity(pool_id_bytes).call()
                )
            except Exception as e:
                logger.warning("get_v4_pool_liquidity_failed", venue=self.name, error=str(e))
                pool_liquidity = None
            return LPMarketSnapshot(
                sqrt_price_x96=sqrt_price_x96,
                current_tick=current_tick,
                current_price=current_price,
                pool_liquidity=pool_liquidity,
            )
        except Exception as e:
            logger.warning("get_v4_live_pool_snapshot_failed", venue=self.name, error=str(e))
            return None

    def _get_static_position_metadata(
        self, token_id: int
    ) -> LPStaticPositionMetadata | None:
        """Read token ID, liquidity, and tick range without needing live pool state."""
        if not self._position_manager_contract:
            return None
        try:
            result = self._position_manager_contract.functions.getPositionInfo(token_id).call()
            info_bytes32: bytes = result[1]
            tick_lower, tick_upper = _decode_position_info(info_bytes32)

            try:
                liquidity = self._position_manager_contract.functions.getPositionLiquidity(
                    token_id
                ).call()
            except Exception:
                pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
                liquidity = self._state_view.functions.getPositionLiquidity(
                    pool_id_bytes,
                    self._position_manager_contract.address,
                    tick_lower,
                    tick_upper,
                    token_id.to_bytes(32, "big"),
                ).call()

            return LPStaticPositionMetadata(
                token_id=token_id,
                liquidity=liquidity,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                range_min=tick_to_price(
                    tick_lower,
                    self.config.token0_decimals,
                    self.config.token1_decimals,
                ),
                range_max=tick_to_price(
                    tick_upper,
                    self.config.token0_decimals,
                    self.config.token1_decimals,
                ),
            )
        except Exception as e:
            logger.warning(
                "get_v4_static_position_metadata_failed",
                venue=self.name,
                token_id=token_id,
                error=str(e),
            )
            return None

    def _build_position_state_from_metadata(
        self,
        metadata: LPStaticPositionMetadata,
        *,
        current_price: Decimal,
        current_tick: int,
    ) -> PositionState:
        in_range = metadata.tick_lower <= current_tick <= metadata.tick_upper
        return PositionState(
            token_id=metadata.token_id,
            liquidity=metadata.liquidity,
            tick_lower=metadata.tick_lower,
            tick_upper=metadata.tick_upper,
            tokens_owed_0=0,
            tokens_owed_1=0,
            price_lower=metadata.range_min,
            price_upper=metadata.range_max,
            current_price=current_price,
            in_range=in_range,
        )

    def _get_position_state_from_market(
        self,
        token_id: int,
        *,
        current_tick: int,
        current_price: Decimal,
    ) -> Optional[PositionState]:
        """Get position details using a shared live market snapshot."""
        if not self._position_manager_contract:
            return None
        metadata = self._get_static_position_metadata(token_id)
        if metadata is None or metadata.liquidity == 0:
            return None
        return self._build_position_state_from_metadata(
            metadata,
            current_price=current_price,
            current_tick=current_tick,
        )

    def get_position_state(self, token_id: int) -> Optional[PositionState]:
        """Get position details via PositionManager.getPositionInfo + live StateView."""
        market = self._get_live_pool_snapshot()
        if market is None:
            return None
        return self._get_position_state_from_market(
            token_id,
            current_tick=market.current_tick,
            current_price=market.current_price,
        )

    # === Portfolio balances ===

    def _empty_position_balances(self) -> dict[str, Decimal]:
        return {"cngn": Decimal(0), "usdt": Decimal(0), "usdc": Decimal(0)}

    def _add_symbol_balance(
        self,
        balances: dict[str, Decimal],
        symbol: str,
        amount: Decimal,
    ) -> None:
        key = symbol.lower()
        if key not in balances:
            logger.warning("unknown_lp_token_symbol", venue=self.name, symbol=symbol)
            return
        balances[key] += amount

    def _compute_lp_token_amounts_from_metadata(
        self,
        metadata: LPStaticPositionMetadata,
        sqrt_price_x96: Decimal,
    ) -> tuple[Decimal, Decimal]:
        pos_state = PositionState(
            token_id=metadata.token_id,
            liquidity=metadata.liquidity,
            tick_lower=metadata.tick_lower,
            tick_upper=metadata.tick_upper,
            tokens_owed_0=0,
            tokens_owed_1=0,
            price_lower=metadata.range_min,
            price_upper=metadata.range_max,
            current_price=Decimal("0"),
            in_range=False,
        )
        return self._compute_lp_token_amounts(pos_state, sqrt_price_x96)

    def get_portfolio_balances(self) -> dict[str, Decimal]:
        """Return LP token balances for portfolio exposure accounting only."""
        balances = self._empty_position_balances()
        token_ids = self.get_owned_positions()
        if not token_ids:
            return balances
        if len(token_ids) > 1:
            logger.warning(
                "multiple_lp_positions_portfolio_balances_unsupported",
                venue=self.name,
                token_ids=token_ids,
            )
            return balances

        metadata = self._get_static_position_metadata(token_ids[0])
        if metadata is None or metadata.liquidity == 0:
            return balances

        market = self._get_live_pool_snapshot()
        sqrt_price_x96 = market.sqrt_price_x96 if market is not None else None
        if sqrt_price_x96 is None:
            from engine.market.pool_state import get_cached_pool_state

            sqrt_price_x96, _, _, _ = get_cached_pool_state(self.config.pool_id)
        if sqrt_price_x96 is None:
            return balances

        amount0, amount1 = self._compute_lp_token_amounts_from_metadata(metadata, sqrt_price_x96)
        self._add_symbol_balance(balances, self.config.token0_symbol, amount0)
        self._add_symbol_balance(balances, self.config.token1_symbol, amount1)
        return balances

    # === Token amount math ===

    def _compute_lp_token_amounts(
        self,
        pos_state: PositionState,
        sqrt_price_x96: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Compute the exact token amounts held inside the LP NFT at the current price."""
        liquidity = pos_state.liquidity
        sqrt_lower = int(_tick_to_sqrt_x96(pos_state.tick_lower))
        sqrt_upper = int(_tick_to_sqrt_x96(pos_state.tick_upper))
        sqrt_price = int(sqrt_price_x96)

        t0_scale = Decimal(10 ** self.config.token0_decimals)
        t1_scale = Decimal(10 ** self.config.token1_decimals)

        if sqrt_price <= sqrt_lower:
            amount0 = Decimal(
                liquidity * _Q96 * (sqrt_upper - sqrt_lower) // (sqrt_lower * sqrt_upper)
            ) / t0_scale
            amount1 = Decimal(0)
        elif sqrt_price >= sqrt_upper:
            amount0 = Decimal(0)
            amount1 = Decimal(liquidity * (sqrt_upper - sqrt_lower) // _Q96) / t1_scale
        else:
            amount0 = Decimal(
                liquidity * _Q96 * (sqrt_upper - sqrt_price) // (sqrt_price * sqrt_upper)
            ) / t0_scale
            amount1 = Decimal(liquidity * (sqrt_price - sqrt_lower) // _Q96) / t1_scale

        return amount0, amount1

    def _build_lp_position_snapshot(
        self,
        pos_state: PositionState,
        *,
        sqrt_price_x96: Decimal,
        pool_liquidity: Decimal | None,
    ) -> LPPositionSnapshot:
        """Build a stable LP snapshot from one active NFT and live pool state."""
        amount0, amount1 = self._compute_lp_token_amounts(pos_state, sqrt_price_x96)

        if self.config.cngn_is_token0:
            cngn_price_usd = (sqrt_price_x96 / _Q96) ** 2
            position_value_usd = amount0 * cngn_price_usd + amount1
        else:
            dec_adj = Decimal(10 ** (self.config.token0_decimals - self.config.token1_decimals))
            cngn_price_usd = Decimal(1) / ((sqrt_price_x96 / _Q96) ** 2 * dec_adj)
            position_value_usd = amount0 + amount1 * cngn_price_usd

        our_share_pct: Decimal | None = None
        if pool_liquidity is not None and pool_liquidity > 0:
            our_share_pct = (
                Decimal(pos_state.liquidity) / pool_liquidity * Decimal(100)
                if pos_state.in_range
                else Decimal(0)
            )

        return LPPositionSnapshot(
            token_id=pos_state.token_id,
            liquidity=pos_state.liquidity,
            token0_amount=amount0,
            token1_amount=amount1,
            token0_symbol=self.config.token0_symbol,
            token1_symbol=self.config.token1_symbol,
            range_min=pos_state.price_lower,
            range_max=pos_state.price_upper,
            in_range=pos_state.in_range,
            position_value_usd=position_value_usd,
            our_share_pct=our_share_pct,
        )

    def _build_degraded_lp_snapshot(
        self,
        metadata: LPStaticPositionMetadata | None,
        *,
        token_id: int | None,
        message: str,
    ) -> LPPositionSnapshot:
        return LPPositionSnapshot(
            token_id=token_id,
            liquidity=metadata.liquidity if metadata is not None else None,
            token0_amount=None,
            token1_amount=None,
            token0_symbol=self.config.token0_symbol,
            token1_symbol=self.config.token1_symbol,
            range_min=metadata.range_min if metadata is not None else None,
            range_max=metadata.range_max if metadata is not None else None,
            in_range=None,
            position_value_usd=None,
            our_share_pct=None,
            snapshot_status="degraded",
            snapshot_message=message,
        )

    def get_active_lp_position_snapshot(self) -> LPPositionSnapshot | None:
        """Return the deployed LP composition, or a degraded presence-only summary."""
        token_ids = self.get_owned_positions()
        if not token_ids:
            return None
        if len(token_ids) > 1:
            logger.warning("multiple_lp_positions_detected", venue=self.name, token_ids=token_ids)
            return self._build_degraded_lp_snapshot(
                None,
                token_id=None,
                message=(
                    "Multiple LP NFTs detected; automatic LP management is halted until manual cleanup."
                ),
            )

        token_id = token_ids[0]
        metadata = self._get_static_position_metadata(token_id)
        if metadata is None:
            return self._build_degraded_lp_snapshot(
                None,
                token_id=token_id,
                message="LP position exists, but live composition is unavailable.",
            )
        if metadata.liquidity == 0:
            return self._build_degraded_lp_snapshot(
                metadata,
                token_id=token_id,
                message="LP NFT exists, but has no active liquidity.",
            )

        market = self._get_live_pool_snapshot()
        if market is None:
            return self._build_degraded_lp_snapshot(
                metadata,
                token_id=token_id,
                message="LP position exists, but live composition is unavailable.",
            )

        pos_state = self._build_position_state_from_metadata(
            metadata,
            current_price=market.current_price,
            current_tick=market.current_tick,
        )
        return self._build_lp_position_snapshot(
            pos_state,
            sqrt_price_x96=market.sqrt_price_x96,
            pool_liquidity=market.pool_liquidity,
        )

    # === Schema adapter ===

    async def get_position_as_schema(self) -> Position:
        """Return the active LP snapshot mapped to the Position API schema."""
        snapshot = self.get_active_lp_position_snapshot()
        balances = self._empty_position_balances()
        lp_position = None
        position_value_usd = None

        if snapshot is not None:
            if snapshot.token0_amount is not None:
                self._add_symbol_balance(balances, snapshot.token0_symbol, snapshot.token0_amount)
            if snapshot.token1_amount is not None:
                self._add_symbol_balance(balances, snapshot.token1_symbol, snapshot.token1_amount)
            lp_position = LPPosition(
                token_id=str(snapshot.token_id) if snapshot.token_id is not None else None,
                liquidity=str(snapshot.liquidity) if snapshot.liquidity is not None else None,
                range_min=snapshot.range_min,
                range_max=snapshot.range_max,
                in_range=snapshot.in_range,
                our_share_pct=snapshot.our_share_pct,
                snapshot_status=snapshot.snapshot_status,
                snapshot_message=snapshot.snapshot_message,
            )
            position_value_usd = snapshot.position_value_usd

        return Position(
            venue=self.name,
            pair=f"{self.config.token0_symbol}/{self.config.token1_symbol}",
            timestamp=int(_time.time() * 1000),
            balances=balances,
            lp_position=lp_position,
            position_value_usd=position_value_usd,
            volume_24h_usd=None,
        )

    # === Capital allocation ===

    def calculate_mint_amounts(self) -> tuple[int, int]:
        """Return full LP wallet balances as raw token amounts to deploy."""
        token0 = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.token0_address),
            abi=ERC20_ABI,
        )
        token1 = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.token1_address),
            abi=ERC20_ABI,
        )
        balance0 = token0.functions.balanceOf(self._lp_account.address).call()
        balance1 = token1.functions.balanceOf(self._lp_account.address).call()
        logger.info(
            "calculated_mint_amounts",
            venue=self.name,
            balance0=balance0 / 10 ** self.config.token0_decimals,
            balance1=balance1 / 10 ** self.config.token1_decimals,
        )
        return balance0, balance1

    # === LP-specific approvals ===

    async def _approve_lp_tokens_if_needed(self) -> None:
        """Approve LP account tokens for PositionManager settlement."""
        if not self._position_manager_contract:
            return
        pm_addr = self.config.position_manager
        for token_addr in [self.config.token0_address, self.config.token1_address]:
            cache_key = f"lp_pm_{token_addr.lower()}"
            if cache_key not in self._lp_approvals_done:
                token = self._w3.eth.contract(
                    address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
                )
                allowance = token.functions.allowance(
                    self._lp_account.address,
                    Web3.to_checksum_address(pm_addr),
                ).call()
                if allowance < 2 ** 128:
                    tx_params = self._tx._get_tx_params(self._lp_account)
                    tx_params["value"] = Wei(0)
                    tx_params["gas"] = 100_000
                    tx = token.functions.approve(
                        Web3.to_checksum_address(pm_addr), 2 ** 256 - 1
                    ).build_transaction(tx_params)
                    result = await self._tx._send_transaction(tx, self._lp_account)
                    if result.status != "confirmed":
                        logger.error(
                            "lp_token_approval_failed",
                            venue=self.name,
                            token=token_addr,
                            error=result.error,
                        )
                self._lp_approvals_done.add(cache_key)
            # V4 PositionManager settles token deltas via Permit2, so LP minting needs
            # Permit2 approval for the PositionManager spender in addition to any direct
            # ERC20 approval path the token may support.
            await self._tx._approve_token_to_permit2_if_needed(token_addr, account=self._lp_account)
            await self._tx._approve_permit2_to_spender_if_needed(
                token_addr,
                pm_addr,
                account=self._lp_account,
            )

    async def _ensure_lp_swap_approvals(self, token_in: str) -> None:
        """Ensure LP account has Permit2 + UniversalRouter approvals for a preparatory swap."""
        await self._tx._approve_token_to_permit2_if_needed(token_in, account=self._lp_account)
        await self._tx._approve_permit2_to_router_if_needed(token_in, account=self._lp_account)

    async def _swap_from_lp(self, token_in: str, amount_in: int, min_out: int) -> TxResult:
        """Swap from the LP account using the same V4 pool (preparatory ratio correction)."""
        await self._ensure_lp_swap_approvals(token_in)
        tx, _ = self._tx._build_swap_tx(token_in, amount_in, min_out, account=self._lp_account)
        estimate_params: TxParams = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": Wei(0),
        }
        estimated = self._w3.eth.estimate_gas(estimate_params)
        tx["gas"] = int(estimated * 1.2)
        token_out = (
            self.config.token1_address
            if token_in.lower() == self.config.token0_address.lower()
            else self.config.token0_address
        )
        return await self._tx._send_transaction(tx, self._lp_account, output_token=token_out)

    # === Write operations ===

    async def mint_position(
        self,
        amount0: int,
        amount1: int,
        tick_lower: int,
        tick_upper: int,
    ) -> TxResult:
        """Create new V4 LP position via PositionManager.modifyLiquidities."""
        if not self._position_manager_contract:
            return TxResult(hash="", status="failed", error="no position_manager configured")

        await self._approve_lp_tokens_if_needed()

        pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
        slot0 = self._state_view.functions.getSlot0(pool_id_bytes).call()
        sqrt_price_x96 = int(slot0[0])

        available0 = amount0
        available1 = amount1
        reserve_target0 = int(Decimal("0.01") * Decimal(10 ** self.config.token0_decimals))
        reserve_target1 = int(Decimal("0.01") * Decimal(10 ** self.config.token1_decimals))
        reserve0 = min(reserve_target0, available0 // 2)
        reserve1 = min(reserve_target1, available1 // 2)
        amount0 = max(available0 - reserve0, 0)
        amount1 = max(available1 - reserve1, 0)

        liquidity = self._compute_liquidity_from_amounts(
            sqrt_price_x96, tick_lower, tick_upper, amount0, amount1
        )
        if liquidity == 0:
            return TxResult(hash="", status="failed", error="computed zero liquidity")

        amount0_max = available0
        amount1_max = available1

        currency0, currency1, fee, tick_spacing, hooks = self._resolve_pool_key()
        pool_key = (currency0, currency1, fee, tick_spacing, hooks)

        actions = bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR])
        params = [
            encode(
                ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
                [pool_key, tick_lower, tick_upper, liquidity, amount0_max, amount1_max, self._lp_account.address, b""],
            ),
            encode(["address", "address"], [currency0, currency1]),
        ]
        unlock_data = encode(["bytes", "bytes[]"], [actions, params])

        deadline = self._w3.eth.get_block("latest")["timestamp"] + 300
        tx_params = self._tx._get_tx_params(self._lp_account)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = 2_000_000  # placeholder; replaced by estimate below
        tx = self._position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)
        estimate_params: TxParams = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": Wei(0),
        }
        estimated = self._w3.eth.estimate_gas(estimate_params)
        tx["gas"] = int(estimated * 1.2)

        logger.info(
            "v4_mint_position",
            venue=self.name,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
            amount0=amount0,
            amount1=amount1,
            amount0_max=amount0_max,
            amount1_max=amount1_max,
            reserve0=reserve0,
            reserve1=reserve1,
        )
        return await self._tx._send_transaction(tx, self._lp_account)

    async def remove_position(
        self, token_id: int, recipient: str | None = None
    ) -> TxResult:
        """Remove V4 LP position via modifyLiquidities (decrease + burn + take).

        recipient: address to send withdrawn tokens to. Defaults to the LP account.
                   Rebalance path passes no recipient (reminting immediately);
                   manual withdraw path passes the caller's destination address.
        """
        if not self._position_manager_contract:
            return TxResult(hash="", status="failed", error="no position_manager configured")

        metadata = self._get_static_position_metadata(token_id)
        if metadata is None:
            return TxResult(hash="", status="failed", error="position not found")

        currency0, currency1, _, _, _ = self._resolve_pool_key()
        to_addr = Web3.to_checksum_address(recipient or self._lp_account.address)

        if metadata.liquidity > 0:
            actions = bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_BURN_POSITION, _V4_LP_TAKE_PAIR])
            params = [
                encode(
                    ["uint256", "uint256", "uint128", "uint128", "bytes"],
                    [token_id, metadata.liquidity, 0, 0, b""],
                ),
                encode(
                    ["uint256", "uint128", "uint128", "bytes"],
                    [token_id, 0, 0, b""],
                ),
                encode(
                    ["address", "address", "address"],
                    [currency0, currency1, to_addr],
                ),
            ]
        else:
            actions = bytes([_V4_LP_BURN_POSITION])
            params = [
                encode(
                    ["uint256", "uint128", "uint128", "bytes"],
                    [token_id, 0, 0, b""],
                ),
            ]
        unlock_data = encode(["bytes", "bytes[]"], [actions, params])

        deadline = self._w3.eth.get_block("latest")["timestamp"] + 300
        tx_params = self._tx._get_tx_params(self._lp_account)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = 2_000_000  # placeholder; replaced by estimate below
        tx = self._position_manager_contract.functions.modifyLiquidities(
            unlock_data, deadline
        ).build_transaction(tx_params)
        estimate_params: TxParams = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": Wei(0),
        }
        estimated = self._w3.eth.estimate_gas(estimate_params)
        tx["gas"] = int(estimated * 1.2)

        logger.info(
            "v4_remove_position",
            venue=self.name,
            token_id=token_id,
            liquidity=metadata.liquidity,
            burn_only=metadata.liquidity == 0,
            recipient=to_addr,
        )
        return await self._tx._send_transaction(tx, self._lp_account)

    async def prepare_lp_balance(
        self, tick_lower: int, tick_upper: int
    ) -> LPBalanceSwapResult | None:
        """Swap LP wallet tokens to the ratio required by the pool at the current price.

        Reads the current sqrtPriceX96 from pool Slot0, computes the target token0/token1
        split using pure tick math, and swaps the surplus token if the imbalance exceeds 1%
        of total portfolio value. downside_skew is NOT used here — ratio is pool-state only.
        """
        pool_id_bytes = bytes.fromhex(self.config.pool_id[2:])
        slot0 = self._state_view.functions.getSlot0(pool_id_bytes).call()
        sqrt_price_x96 = int(slot0[0])

        token0 = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.token0_address), abi=ERC20_ABI
        )
        token1 = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.token1_address), abi=ERC20_ABI
        )
        balance0_raw = token0.functions.balanceOf(self._lp_account.address).call()
        balance1_raw = token1.functions.balanceOf(self._lp_account.address).call()
        balance0 = Decimal(balance0_raw) / Decimal(10 ** self.config.token0_decimals)
        balance1 = Decimal(balance1_raw) / Decimal(10 ** self.config.token1_decimals)

        if balance0 == 0 and balance1 == 0:
            return None

        r0, r1 = compute_required_ratio(
            tick_lower, tick_upper, sqrt_price_x96,
            self.config.token0_decimals, self.config.token1_decimals,
        )

        # Price of token0 in token1 units (for value normalisation)
        price = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2 * Decimal(
            10 ** self.config.token0_decimals
        ) / Decimal(10 ** self.config.token1_decimals)

        # Total value in token1 units
        total_value = balance0 * price + balance1
        if total_value == 0:
            return None

        # Target allocations
        denom = r0 * price + r1 if (r0 * price + r1) > 0 else Decimal(1)
        target0 = total_value * r0 / denom
        target1 = max(Decimal(0), total_value - target0 * price)

        imbalance = abs(balance0 - target0) * price
        threshold = total_value * Decimal("0.01")

        if imbalance <= threshold:
            logger.info(
                "lp_balance_already_correct",
                venue=self.name,
                balance0=float(balance0),
                balance1=float(balance1),
                target0=float(target0),
                target1=float(target1),
            )
            return None

        if balance0 > target0:
            surplus = balance0 - target0
            surplus_raw = int(surplus * Decimal(10 ** self.config.token0_decimals))
            min_out = int(surplus * price * Decimal("0.99") * Decimal(10 ** self.config.token1_decimals))
            token_in = self.config.token0_address
            token_out = self.config.token1_address
            direction = "token0_to_token1"
            logger.info(
                "lp_swap_to_ratio",
                venue=self.name,
                direction="token0→token1",
                surplus=float(surplus),
                min_out=min_out,
            )
            result = await self._swap_from_lp(token_in, surplus_raw, min_out)
        else:
            surplus = balance1 - target1
            surplus_raw = int(surplus * Decimal(10 ** self.config.token1_decimals))
            min_out_dec = surplus / price * Decimal("0.99")
            min_out = int(min_out_dec * Decimal(10 ** self.config.token0_decimals))
            token_in = self.config.token1_address
            token_out = self.config.token0_address
            direction = "token1_to_token0"
            logger.info(
                "lp_swap_to_ratio",
                venue=self.name,
                direction="token1→token0",
                surplus=float(surplus),
                min_out=min_out,
            )
            result = await self._swap_from_lp(token_in, surplus_raw, min_out)

        if result.status != "confirmed":
            logger.warning(
                "lp_ratio_swap_failed_skipping_mint", venue=self.name, error=result.error
            )
        return LPBalanceSwapResult(
            direction=direction,
            token_in=token_in,
            token_out=token_out,
            amount_in_raw=surplus_raw,
            min_out_raw=min_out,
            tx_result=result,
        )

    # === Helpers ===

    def _compute_liquidity_from_amounts(
        self,
        sqrt_price_x96: int,
        tick_lower: int,
        tick_upper: int,
        amount0: int,
        amount1: int,
    ) -> int:
        """Convert token amounts + tick range to V4 liquidity units."""
        sqrt_a = int(_tick_to_sqrt_x96(tick_lower))
        sqrt_b = int(_tick_to_sqrt_x96(tick_upper))
        sqrt_p = sqrt_price_x96

        if sqrt_p <= sqrt_a:
            if sqrt_b == sqrt_a:
                return 0
            return amount0 * sqrt_a * sqrt_b // ((sqrt_b - sqrt_a) * _Q96)
        elif sqrt_p >= sqrt_b:
            if sqrt_b == sqrt_a:
                return 0
            return amount1 * _Q96 // (sqrt_b - sqrt_a)
        else:
            L0 = amount0 * sqrt_p * sqrt_b // ((sqrt_b - sqrt_p) * _Q96) if sqrt_b > sqrt_p else 0
            L1 = amount1 * _Q96 // (sqrt_p - sqrt_a) if sqrt_p > sqrt_a else 0
            if L0 > 0 and L1 > 0:
                return min(L0, L1)
            return max(L0, L1)
