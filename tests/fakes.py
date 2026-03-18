"""In-process test doubles for DEX and CEX adapters.

Importable from both conftest.py and test modules.
"""

from decimal import Decimal

from engine.api.schemas import DexParams, TxResult
from engine.venues.dex.base import BaseDexAdapter, PositionState


class FakeDexAdapter:
    """In-process double for BaseDexAdapter. No Web3, no RPC.

    Configurable to succeed or fail on any operation.
    Tracks minted positions and transfers for assertions.
    """

    def __init__(
        self,
        name: str = "uni-base",
        token0_bal: Decimal = Decimal("500000"),
        token1_bal: Decimal = Decimal("600"),
        trade0_bal: Decimal = Decimal("100000"),
        trade1_bal: Decimal = Decimal("200"),
        position: "PositionState | None" = None,
        remove_fails: bool = False,
        mint_fails: bool = False,
        tick_spacing: int = 60,
    ):
        self.name = name
        self.paused = False
        self._positions: list[PositionState] = [position] if position else []
        self._token0_bal = token0_bal
        self._token1_bal = token1_bal
        self._trade0_bal = trade0_bal
        self._trade1_bal = trade1_bal
        self._remove_fails = remove_fails
        self._mint_fails = mint_fails
        self.params = DexParams(
            deploy_token0=token0_bal,
            deploy_token1=token1_bal,
            rebalance_threshold_percent=Decimal("2.0"),
        )
        self.minted: list[dict] = []
        self.transfers: list[dict] = []

        class _Config:
            token0_decimals = 6
            token1_decimals = 6
            token0_symbol = "cNGN"
            token1_symbol = "USDC"

        self.config = _Config()
        self.config.tick_spacing = tick_spacing

    def get_owned_positions(self) -> list[int]:
        return [p.token_id for p in self._positions]

    def get_position_state(self, token_id: int) -> "PositionState | None":
        return next((p for p in self._positions if p.token_id == token_id), None)

    def calculate_mint_amounts(self) -> tuple[int, int]:
        return (
            int(self._token0_bal * Decimal(10 ** self.config.token0_decimals)),
            int(self._token1_bal * Decimal(10 ** self.config.token1_decimals)),
        )

    def get_trade_token_balances(self) -> tuple[Decimal, Decimal]:
        return self._trade0_bal, self._trade1_bal

    async def remove_position(self, token_id: int) -> TxResult:
        if self._remove_fails:
            return TxResult(hash="", status="failed", error="simulated remove failure")
        self._positions = [p for p in self._positions if p.token_id != token_id]
        return TxResult(hash="0xabcdremove", status="confirmed")

    async def transfer_from_trade_to_lp(self, token_index: int, amount: Decimal) -> TxResult:
        self.transfers.append({"token_index": token_index, "amount": amount})
        if token_index == 0:
            self._trade0_bal -= amount
            self._token0_bal += amount
        else:
            self._trade1_bal -= amount
            self._token1_bal += amount
        return TxResult(hash="0xabcdtransfer", status="confirmed")

    async def mint_position(self, amount0: int, amount1: int, tick_lower: int, tick_upper: int) -> TxResult:
        if self._mint_fails:
            return TxResult(hash="", status="failed", error="simulated mint failure")
        token_id = 100 + len(self._positions)
        new_pos = PositionState(
            token_id=token_id,
            liquidity=1_000_000,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            tokens_owed_0=0,
            tokens_owed_1=0,
            price_lower=Decimal("0.0005"),
            price_upper=Decimal("0.0007"),
            current_price=Decimal("0.000606"),
            in_range=True,
        )
        self._positions.append(new_pos)
        self.minted.append({"amount0": amount0, "amount1": amount1, "tick_lower": tick_lower, "tick_upper": tick_upper})
        return TxResult(hash="0xabcdmint", status="confirmed")

    def calculate_tick_range(self, prices, recovery_price=None) -> tuple[int, int]:
        return -1000, 1000


class FakeCexAdapter:
    """In-process double for CEX venue. Configurable market order outcomes."""

    def __init__(
        self,
        buy_success: bool = True,
        sell_success: bool = True,
        buy_cngn: Decimal = Decimal("1000"),
        buy_price: Decimal = Decimal("0.000606"),
        sell_cngn: Decimal = Decimal("1000"),
        sell_price: Decimal = Decimal("0.000607"),
    ):
        self._buy_success = buy_success
        self._sell_success = sell_success
        self._buy_cngn = buy_cngn
        self._buy_price = buy_price
        self._sell_cngn = sell_cngn
        self._sell_price = sell_price

    async def place_market_order(self, side: str, amount: Decimal):
        if side == "buy":
            if self._buy_success:
                return True, self._buy_cngn, self._buy_price, None
            return False, Decimal("0"), self._buy_price, "simulated buy failure"
        if self._sell_success:
            return True, self._sell_cngn, self._sell_price, None
        return False, Decimal("0"), self._sell_price, "simulated sell failure"


# Register FakeDexAdapter as a virtual subclass of BaseDexAdapter so that
# isinstance(fake, BaseDexAdapter) returns True in scheduler tests.
BaseDexAdapter.register(FakeDexAdapter)
