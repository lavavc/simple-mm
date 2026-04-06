"""In-process test doubles for DEX and CEX adapters.

Importable from both conftest.py and test modules.
"""

from decimal import Decimal
from types import SimpleNamespace

from engine.api.schemas import LPPosition, Position, TxResult
from tests.conftest_params import make_dex_params
from engine.venues.dex.lp_v4 import V4LPAdapter
from engine.venues.dex.shared import PositionState


class _DummyBalanceCall:
    def call(self) -> int:
        return 0


class _DummyFunctions:
    def balanceOf(self, _address: str) -> _DummyBalanceCall:
        return _DummyBalanceCall()


class _DummyContract:
    functions = _DummyFunctions()


class FakeDexAdapter:
    """In-process double for V4LPAdapter. No Web3, no RPC.

    Configurable to succeed or fail on any operation.
    Tracks minted positions and transfers for assertions.
    """

    def __init__(
        self,
        name: str = "uni-base",
        token0_bal: Decimal = Decimal("500000"),
        token1_bal: Decimal = Decimal("600"),
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
        self._remove_fails = remove_fails
        self._mint_fails = mint_fails
        self.params = make_dex_params(rebalance_threshold_percent=Decimal("2.0"))
        self.minted: list[dict] = []
        self._position_balances = {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}

        class _Config:
            token0_decimals = 6
            token1_decimals = 6
            token0_symbol = "cNGN"
            token1_symbol = "USDC"
            token0_address = "0xcngn"
            token1_address = "0xusdc"
            invert_price = False

        self.config = _Config()
        self.config.tick_spacing = tick_spacing
        self.stable_address = "0xstable"
        self.cngn_address = "0xcngn"
        self.stable_decimals = self.config.token1_decimals
        self.cngn_decimals = self.config.token0_decimals
        self.stable_token = _DummyContract()
        self.cngn_token = _DummyContract()
        self.trade_account = SimpleNamespace(address="0xFAKEDEX00000000000000000000000000000001")

    def get_owned_positions(self) -> list[int]:
        return [p.token_id for p in self._positions]

    def get_position_state(self, token_id: int) -> "PositionState | None":
        return next((p for p in self._positions if p.token_id == token_id), None)

    def get_portfolio_balances(self) -> dict[str, Decimal]:
        if len(self._positions) > 1:
            return {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}
        return dict(self._position_balances)

    async def get_position(self) -> Position:
        lp_position = None
        if self._positions:
            token_id = str(self._positions[0].token_id) if len(self._positions) == 1 else None
            snapshot_message = None
            if len(self._positions) > 1:
                snapshot_message = (
                    "Multiple LP NFTs detected; automatic LP management is halted until manual cleanup."
                )
            lp_position = LPPosition(
                token_id=token_id,
                liquidity=str(self._positions[0].liquidity) if len(self._positions) == 1 else None,
                range_min=self._positions[0].price_lower if len(self._positions) == 1 else None,
                range_max=self._positions[0].price_upper if len(self._positions) == 1 else None,
                in_range=self._positions[0].in_range if len(self._positions) == 1 else None,
                our_share_pct=None,
                snapshot_status="live" if len(self._positions) == 1 else "degraded",
                snapshot_message=snapshot_message,
            )
        return Position(
            venue=self.name,
            pair="cNGN/USDC",
            timestamp=0,
            balances=dict(self._position_balances),
            lp_position=lp_position,
            position_value_usd=None,
        )

    def calculate_mint_amounts(self) -> tuple[int, int]:
        return (
            int(self._token0_bal * Decimal(10 ** self.config.token0_decimals)),
            int(self._token1_bal * Decimal(10 ** self.config.token1_decimals)),
        )

    def simulate_swap(self, token_in: str, amount_in: int, min_out: int) -> None:
        return None

    async def ensure_trade_approvals(self) -> None:
        return None

    async def prepare_lp_balance(self, tick_lower: int, tick_upper: int) -> None:
        """No-op in tests — ratio swap is tested separately in test_lp_ratio.py."""
        pass

    async def remove_position(self, token_id: int, recipient: str | None = None) -> TxResult:
        if self._remove_fails:
            return TxResult(hash="", status="failed", error="simulated remove failure")
        self._positions = [p for p in self._positions if p.token_id != token_id]
        return TxResult(hash="0xabcdremove", status="confirmed")

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


# Register FakeDexAdapter as a virtual subclass of V4LPAdapter so that
# isinstance(fake, V4LPAdapter) returns True in scheduler tests.
V4LPAdapter.register(FakeDexAdapter)
