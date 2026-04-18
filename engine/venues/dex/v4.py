"""Uniswap V4 execution adapters built on Universal Router."""

import asyncio
import time as _time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Literal

import structlog
from eth_abi import encode  # type: ignore[attr-defined]
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.middleware import geth_poa_middleware  # type: ignore[attr-defined]
from web3.types import BlockData, Nonce, TxParams, TxReceipt, Wei

from engine.types import Position, PriceQuote, TxResult
from engine.config import DexParams
from engine.web3_utils import as_hexstr, coerce_hex_bytes, coerce_hex_str
from engine.venues.base import VenueAdapter
from .shared import ERC20_ABI, MULTICALL3_ABI, MULTICALL3_ADDRESS, _decode_uint256, _encode_balance_of, sqrt_price_x96_to_decimal

logger = structlog.get_logger()

_UR_COMMAND_V4_SWAP = bytes([0x10])
_V4_ACTION_SWAP_EXACT_IN_SINGLE = 0x06
_V4_ACTION_SETTLE_ALL = 0x0C
_V4_ACTION_TAKE_ALL = 0x0F

_DEFAULT_APPROVAL_GAS = 100000
_DEFAULT_PERMIT2_APPROVAL_GAS = 120000
_PERMIT2_MAX_AMOUNT = (1 << 160) - 1
_PERMIT2_EXPIRATION = (1 << 48) - 1

STATE_VIEW_ABI = [
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getSlot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "protocolFee", "type": "uint24"},
            {"name": "lpFee", "type": "uint24"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getLiquidity",
        "outputs": [{"name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "poolId", "type": "bytes32"},
            {"name": "owner", "type": "address"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "salt", "type": "bytes32"},
        ],
        "name": "getPositionLiquidity",
        "outputs": [{"name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
]

UNIVERSAL_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "bytes", "name": "commands", "type": "bytes"},
            {"internalType": "bytes[]", "name": "inputs", "type": "bytes[]"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]

PERMIT2_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
            {"internalType": "uint48", "name": "nonce", "type": "uint48"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

POOL_MANAGER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "bytes32", "name": "id", "type": "bytes32"},
            {"indexed": True, "internalType": "address", "name": "currency0", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "currency1", "type": "address"},
            {"indexed": False, "internalType": "uint24", "name": "fee", "type": "uint24"},
            {"indexed": False, "internalType": "int24", "name": "tickSpacing", "type": "int24"},
            {"indexed": False, "internalType": "address", "name": "hooks", "type": "address"},
            {"indexed": False, "internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "internalType": "int24", "name": "tick", "type": "int24"},
        ],
        "name": "Initialize",
        "type": "event",
    }
]


@dataclass
class V4ExecutionConfig:
    chain_id: int
    chain_name: str
    rpc_url: str
    pool_manager: str
    state_view: str
    pool_id: str
    universal_router: str
    permit2: str
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    fee: Optional[int] = None
    tick_spacing: Optional[int] = None
    hooks: str = "0x0000000000000000000000000000000000000000"
    invert_price: bool = False
    cngn_is_token0: bool = False
    position_manager: str = ""

    def resolve_pool_key(self) -> tuple[str, str, int, int, str]:
        """Build the canonical pool key tuple from config fields.

        Raises ValueError if fee or tick_spacing are not set.
        """
        if self.fee is None or self.tick_spacing is None:
            raise ValueError(
                f"fee and tick_spacing must be set in config (pool_id={self.pool_id})"
            )
        return (
            Web3.to_checksum_address(self.token0_address),
            Web3.to_checksum_address(self.token1_address),
            int(self.fee),
            int(self.tick_spacing),
            Web3.to_checksum_address(self.hooks),
        )


class BaseV4DexAdapter(VenueAdapter):
    """Minimal execution adapter for Uniswap V4 pools via Universal Router."""

    name: str

    def __init__(
        self,
        config: V4ExecutionConfig,
        lp_private_key: str,
        trade_private_key: str,
        strategy_params: DexParams,
    ):
        self.config = config
        self.params = strategy_params

        self.w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        if config.chain_id in (56, 97):
            self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        self.lp_account = self.w3.eth.account.from_key(lp_private_key)
        self.trade_account = self.w3.eth.account.from_key(trade_private_key)

        self.state_view = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.state_view),
            abi=STATE_VIEW_ABI,
        )
        self.universal_router = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.universal_router),
            abi=UNIVERSAL_ROUTER_ABI,
        )
        self.permit2 = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.permit2),
            abi=PERMIT2_ABI,
        )
        self.pool_manager = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.pool_manager),
            abi=POOL_MANAGER_ABI,
        )

        self.token0 = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.token0_address),
            abi=ERC20_ABI,
        )
        self.token1 = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.token1_address),
            abi=ERC20_ABI,
        )

        self._nonce_locks: dict[str, asyncio.Lock] = {}
        self._pool_key: Optional[tuple[str, str, int, int, str]] = None
        self._approvals_done: set[str] = set()

    @property
    def cngn_decimals(self) -> int:
        return self.config.token1_decimals if self.config.invert_price else self.config.token0_decimals

    @property
    def stable_decimals(self) -> int:
        return self.config.token0_decimals if self.config.invert_price else self.config.token1_decimals

    @property
    def stable_address(self) -> str:
        return self.config.token0_address if self.config.invert_price else self.config.token1_address

    @property
    def cngn_address(self) -> str:
        return self.config.token1_address if self.config.invert_price else self.config.token0_address

    @property
    def stable_token(self) -> Any:
        return self.token0 if self.config.invert_price else self.token1

    @property
    def cngn_token(self) -> Any:
        return self.token1 if self.config.invert_price else self.token0

    async def get_position(self) -> Position:
        multicall = self.w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI,
        )
        results = multicall.functions.aggregate3([
            (Web3.to_checksum_address(self.config.token0_address), True, _encode_balance_of(self.lp_account.address)),
            (Web3.to_checksum_address(self.config.token0_address), True, _encode_balance_of(self.trade_account.address)),
            (Web3.to_checksum_address(self.config.token1_address), True, _encode_balance_of(self.lp_account.address)),
            (Web3.to_checksum_address(self.config.token1_address), True, _encode_balance_of(self.trade_account.address)),
        ]).call()

        t0_amount = Decimal(
            (_decode_uint256(results[0][1]) if results[0][0] else 0)
            + (_decode_uint256(results[1][1]) if results[1][0] else 0)
        ) / Decimal(10 ** self.config.token0_decimals)
        t1_amount = Decimal(
            (_decode_uint256(results[2][1]) if results[2][0] else 0)
            + (_decode_uint256(results[3][1]) if results[3][0] else 0)
        ) / Decimal(10 ** self.config.token1_decimals)

        balances = {"cngn": Decimal(0), "usdt": Decimal(0), "usdc": Decimal(0)}
        for sym, amount in [
            (self.config.token0_symbol.lower(), t0_amount),
            (self.config.token1_symbol.lower(), t1_amount),
        ]:
            if sym in balances:
                balances[sym] = amount
            else:
                balances["usdt"] = amount

        return Position(
            venue=self.name,
            pair=f"{self.config.token0_symbol}/{self.config.token1_symbol}",
            timestamp=int(_time.time() * 1000),
            balances=balances,
            lp_position=None,
            position_value_usd=None,
            volume_24h_usd=None,
        )

    async def get_current_price(self) -> Optional[PriceQuote]:
        try:
            pool_id = bytes.fromhex(self.config.pool_id[2:])
            slot0 = self.state_view.functions.getSlot0(pool_id).call()
            sqrt_price_x96 = int(slot0[0])
            mid = sqrt_price_x96_to_decimal(
                sqrt_price_x96,
                self.config.token0_decimals,
                self.config.token1_decimals,
            )
            if self.config.invert_price and mid > 0:
                mid = Decimal(1) / mid
            return PriceQuote(
                source=f"{self.name}_pool",
                timestamp=int(_time.time() * 1000),
                bid=mid,
                ask=mid,
                mid=mid,
            )
        except Exception as e:
            logger.error("v4_price_fetch_failed", venue=self.name, error=str(e))
            return None

    async def ensure_trade_approvals(self) -> None:
        for token in [self.config.token0_address, self.config.token1_address]:
            await self._approve_token_to_permit2_if_needed(token)
            await self._approve_permit2_to_router_if_needed(token)

    def _build_swap_tx(
        self,
        token_in: str,
        amount_in: int,
        min_amount_out: int,
        *,
        account: LocalAccount | None = None,
    ) -> tuple[TxParams, int]:
        """Build the Universal Router swap transaction. Returns (tx, deadline).

        Makes network calls: fetches latest block for deadline, resolves pool key,
        and queries current nonce + gas price via _get_tx_params.
        account: signing account (default: self.trade_account).
        """
        acct = account or self.trade_account
        token_out = (
            self.config.token1_address
            if token_in.lower() == self.config.token0_address.lower()
            else self.config.token0_address
        )
        currency0, currency1, fee, tick_spacing, hooks = self._resolve_pool_key()
        zero_for_one = token_in.lower() == currency0.lower()
        block = self.w3.eth.get_block("latest")
        deadline = block["timestamp"] + 300
        actions = bytes([_V4_ACTION_SWAP_EXACT_IN_SINGLE, _V4_ACTION_SETTLE_ALL, _V4_ACTION_TAKE_ALL])
        params = [
            encode(
                ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
                [((currency0, currency1, fee, tick_spacing, hooks), zero_for_one, amount_in, min_amount_out, b"")],
            ),
            encode(["address", "uint256"], [token_in, amount_in]),
            encode(["address", "uint256"], [token_out, min_amount_out]),
        ]
        v4_input = encode(["bytes", "bytes[]"], [actions, params])
        tx_params = self._get_tx_params(acct, block=block)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = 2_000_000  # overwritten by estimate_gas × 1.2 in swap()
        tx = self.universal_router.functions.execute(_UR_COMMAND_V4_SWAP, [v4_input], deadline).build_transaction(tx_params)
        return tx, deadline

    def simulate_swap(self, token_in: str, amount_in: int, min_amount_out: int) -> str | None:
        """Run the swap preflight (eth_call) without sending. Returns error string or None if ok."""
        tx, _ = self._build_swap_tx(token_in, amount_in, min_amount_out)
        try:
            call_params: TxParams = {
                "from": tx["from"],
                "to": tx["to"],
                "data": tx["data"],
                "value": tx.get("value", Wei(0)),
            }
            self.w3.eth.call(call_params)
            return None
        except Exception as e:
            return str(e)

    async def swap(self, token_in: str, amount_in: int, min_amount_out: int) -> TxResult:
        await self.ensure_trade_approvals()

        token_out = (
            self.config.token1_address
            if token_in.lower() == self.config.token0_address.lower()
            else self.config.token0_address
        )
        logger.info(
            "v4_swap_build_started",
            venue=self.name,
            account=self.trade_account.address,
            token_in=Web3.to_checksum_address(token_in),
            token_out=Web3.to_checksum_address(token_out),
            amount_in=amount_in,
            min_amount_out=min_amount_out,
        )

        tx, deadline = self._build_swap_tx(token_in, amount_in, min_amount_out)
        estimated = self.w3.eth.estimate_gas(
            {
                "from": tx["from"],
                "to": tx["to"],
                "data": tx["data"],
                "value": tx.get("value", Wei(0)),
            }
        )
        tx["gas"] = int(estimated * 1.2)
        logger.info("v4_swap_gas_estimated", venue=self.name, estimated=estimated, gas_limit=tx["gas"])
        return await self._send_transaction(tx, self.trade_account, output_token=token_out)

    def _resolve_pool_key(self) -> tuple[str, str, int, int, str]:
        if self._pool_key is not None:
            return self._pool_key

        try:
            self._pool_key = self.config.resolve_pool_key()
            logger.info(
                "v4_pool_key_resolved",
                venue=self.name,
                pool_id=self.config.pool_id,
                fee=self._pool_key[2],
                tick_spacing=self._pool_key[3],
                hooks=self._pool_key[4],
                source="config",
            )
            return self._pool_key
        except ValueError as exc:
            logger.warning("v4_pool_key_config_incomplete", venue=self.name, reason=str(exc))

        event_topic = as_hexstr(self.w3.keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex())
        logs = self.w3.eth.get_logs({
            "address": Web3.to_checksum_address(self.config.pool_manager),
            "topics": [event_topic, as_hexstr(self.config.pool_id)],
            "fromBlock": 0,
            "toBlock": "latest",
        })
        if not logs:
            raise ValueError(f"Could not resolve pool key for {self.name}: no Initialize event for {self.config.pool_id}")

        decoded = self.pool_manager.events.Initialize().process_log(logs[0])
        args = decoded["args"]
        self._pool_key = (
            Web3.to_checksum_address(args["currency0"]),
            Web3.to_checksum_address(args["currency1"]),
            int(args["fee"]),
            int(args["tickSpacing"]),
            Web3.to_checksum_address(args["hooks"]),
        )
        logger.info(
            "v4_pool_key_resolved",
            venue=self.name,
            pool_id=self.config.pool_id,
            fee=self._pool_key[2],
            tick_spacing=self._pool_key[3],
            hooks=self._pool_key[4],
            source="chain",
        )
        return self._pool_key

    def _get_tx_params(
        self,
        account: LocalAccount,
        block: BlockData | None = None,
    ) -> TxParams:
        if block is None:
            block = self.w3.eth.get_block("latest")
        if "baseFeePerGas" in block:
            base_fee = int(block["baseFeePerGas"])
        else:
            base_fee = int(self.w3.eth.gas_price)
        priority_fee = Wei(self.w3.to_wei(0.1, "gwei"))
        max_fee_per_gas = Wei((2 * base_fee) + int(priority_fee))

        logger.info(
            "tx_fee_params",
            venue=self.name,
            account=account.address,
            chain_id=self.config.chain_id,
            base_fee_wei=base_fee,
            priority_fee_wei=int(priority_fee),
            max_fee_per_gas_wei=int(max_fee_per_gas),
        )

        return {
            "from": account.address,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": self.config.chain_id,
        }

    def _parse_swap_output_raw(self, receipt: TxReceipt, token_out: str) -> Optional[int]:
        """Extract actual output token amount from a V4 Swap event in the receipt logs."""
        from engine.market.dex_volume import V4_SWAP_TOPIC
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics:
                continue
            t0_hex = coerce_hex_str(topics[0]).lower()
            if t0_hex != V4_SWAP_TOPIC.lower():
                continue
            data = coerce_hex_bytes(log.get("data", "0x"))
            if len(data) < 64:
                continue
            amount0 = int.from_bytes(data[0:32], "big", signed=True)
            amount1 = int.from_bytes(data[32:64], "big", signed=True)
            # The output token has a negative delta (pool paid it out).
            if token_out.lower() == self.config.token0_address.lower():
                return abs(amount0) if amount0 < 0 else None
            return abs(amount1) if amount1 < 0 else None
        return None

    async def _send_transaction(
        self,
        tx: TxParams,
        account: LocalAccount,
        *,
        output_token: str | None = None,
    ) -> TxResult:
        try:
            if account.address not in self._nonce_locks:
                self._nonce_locks[account.address] = asyncio.Lock()

            async with self._nonce_locks[account.address]:
                tx["nonce"] = Nonce(self.w3.eth.get_transaction_count(account.address, "pending"))
                signed = account.sign_transaction(tx)  # type: ignore[no-untyped-call]
                tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)

            receipt: TxReceipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            status: Literal["confirmed", "failed"] = "confirmed" if receipt["status"] == 1 else "failed"
            log_fn = logger.info if status == "confirmed" else logger.error
            tx_hash_str = coerce_hex_str(tx_hash)
            log_fn(
                "transaction_sent",
                venue=self.name,
                account=account.address,
                hash=tx_hash_str,
                status=status,
                gas_used=receipt["gasUsed"],
            )
            output_raw = self._parse_swap_output_raw(receipt, output_token) if output_token and status == "confirmed" else None
            return TxResult(
                hash=tx_hash_str,
                status=status,
                gas_used=receipt["gasUsed"],
                output_raw=output_raw,
            )
        except Exception as e:
            logger.error("transaction_failed", venue=self.name, account=account.address, error=str(e))
            return TxResult(hash="", status="failed", error=str(e))

    async def _approve_token_to_permit2_if_needed(
        self,
        token: str,
        *,
        account: LocalAccount | None = None,
    ) -> None:
        acct = account or self.trade_account
        cache_key = f"erc20_permit2_{token.lower()}_{acct.address.lower()}"
        if cache_key in self._approvals_done:
            return

        token_contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        current = token_contract.functions.allowance(
            acct.address,
            Web3.to_checksum_address(self.config.permit2),
        ).call()
        logger.info(
            "token_to_permit2_allowance_state",
            venue=self.name,
            token=token,
            owner=acct.address,
            spender=self.config.permit2,
            allowance=current,
        )
        if current >= 1:
            self._approvals_done.add(cache_key)
            return

        logger.info("approving_token_to_permit2", venue=self.name, token=token, account=acct.address)
        tx_params = self._get_tx_params(acct)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = _DEFAULT_APPROVAL_GAS
        # Unlimited approval to Permit2 — Permit2 then enforces per-spender allowances.
        # See runbook.md "Known issues" for the infinite approval risk note.
        tx = token_contract.functions.approve(
            Web3.to_checksum_address(self.config.permit2),
            2**256 - 1,
        ).build_transaction(tx_params)
        result = await self._send_transaction(tx, acct)
        if result.status != "confirmed":
            logger.error("token_to_permit2_approval_failed", venue=self.name, token=token, error=result.error)
        else:
            self._approvals_done.add(cache_key)

    async def _approve_permit2_to_router_if_needed(
        self,
        token: str,
        *,
        account: LocalAccount | None = None,
    ) -> None:
        await self._approve_permit2_to_spender_if_needed(
            token,
            self.config.universal_router,
            account=account,
        )

    async def _approve_permit2_to_spender_if_needed(
        self,
        token: str,
        spender: str,
        *,
        account: LocalAccount | None = None,
    ) -> None:
        acct = account or self.trade_account
        spender_checksum = Web3.to_checksum_address(spender)
        cache_key = f"permit2_spender_{token.lower()}_{spender_checksum.lower()}_{acct.address.lower()}"
        if cache_key in self._approvals_done:
            return

        amount, expiration, _ = self.permit2.functions.allowance(
            acct.address,
            Web3.to_checksum_address(token),
            spender_checksum,
        ).call()
        logger.info(
            "permit2_spender_allowance_state",
            venue=self.name,
            token=token,
            owner=acct.address,
            spender=spender_checksum,
            amount=amount,
            expiration=expiration,
        )
        if amount >= 1 and expiration > int(_time.time()) + 3600:
            self._approvals_done.add(cache_key)
            return

        logger.info(
            "approving_permit2_to_spender",
            venue=self.name,
            token=token,
            spender=spender_checksum,
            account=acct.address,
        )
        tx_params = self._get_tx_params(acct)
        tx_params["value"] = Wei(0)
        tx_params["gas"] = _DEFAULT_PERMIT2_APPROVAL_GAS
        tx = self.permit2.functions.approve(
            Web3.to_checksum_address(token),
            spender_checksum,
            _PERMIT2_MAX_AMOUNT,
            _PERMIT2_EXPIRATION,
        ).build_transaction(tx_params)
        result = await self._send_transaction(tx, acct)
        if result.status != "confirmed":
            logger.error(
                "permit2_spender_approval_failed",
                venue=self.name,
                token=token,
                spender=spender_checksum,
                error=result.error,
            )
        else:
            self._approvals_done.add(cache_key)
