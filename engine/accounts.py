"""HD wallet account management for multi-venue trading."""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.middleware import geth_poa_middleware  # type: ignore[attr-defined]
import structlog

from engine.config import settings

logger = structlog.get_logger()

# Enable HD wallet features
Account.enable_unaudited_hdwallet_features()  # type: ignore[no-untyped-call]

# Anvil's default test mnemonic
ANVIL_TEST_MNEMONIC = "test test test test test test test test test test test junk"


class AccountRole(str, Enum):
    """Roles for derived accounts."""

    UNI_BASE_LP = "uni-base-lp"
    UNI_BASE_TRADE = "uni-base-trade"
    BLOCKRADAR = "blockradar"
    QUIDAX_TRADE_FUND = "quidax-trade-fund"
    QUIDAX_LP = "quidax-lp"
    UNI_BSC_LP = "uni-bsc-lp"
    UNI_BSC_TRADE = "uni-bsc-trade"


@dataclass
class AccountConfig:
    """Configuration for a derived account."""

    role: AccountRole
    derivation_path: str
    chain_id: int
    rpc_url: str
    tokens: list[str]  # Token symbols this account holds
    min_balance_eth: Decimal = Decimal("0.01")  # Min ETH/native for gas
    min_balance_tokens: dict[str, Decimal] = field(default_factory=dict)  # Min per token


# Default account configurations
DEFAULT_ACCOUNT_CONFIGS = {
    AccountRole.UNI_BASE_LP: AccountConfig(
        role=AccountRole.UNI_BASE_LP,
        derivation_path="m/44'/60'/0'/1/0",
        chain_id=8453,  # Base
        rpc_url=settings.base_rpc_url,
        tokens=["cNGN", "USDC"],
        min_balance_eth=Decimal("0.001"),
        min_balance_tokens={"cNGN": Decimal("50000"), "USDC": Decimal("50")},
    ),
    AccountRole.UNI_BASE_TRADE: AccountConfig(
        role=AccountRole.UNI_BASE_TRADE,
        derivation_path="m/44'/60'/0'/1/1",
        chain_id=8453,  # Base
        rpc_url=settings.base_rpc_url,
        tokens=["cNGN", "USDC"],
        min_balance_eth=Decimal("0.001"),
        min_balance_tokens={"cNGN": Decimal("10000"), "USDC": Decimal("10")},
    ),
    AccountRole.BLOCKRADAR: AccountConfig(
        role=AccountRole.BLOCKRADAR,
        derivation_path="m/44'/60'/0'/2/0",
        chain_id=8453,  # Base
        rpc_url=settings.base_rpc_url,
        tokens=["cNGN", "USDT", "USDC"],
        min_balance_eth=Decimal("0.005"),
        min_balance_tokens={"cNGN": Decimal("50000"), "USDT": Decimal("100"), "USDC": Decimal("100")},
    ),
    AccountRole.QUIDAX_TRADE_FUND: AccountConfig(
        role=AccountRole.QUIDAX_TRADE_FUND,
        derivation_path="m/44'/60'/0'/3/0",
        chain_id=56,  # BSC
        rpc_url=settings.bsc_rpc_url,
        tokens=["cNGN", "USDT"],
        min_balance_eth=Decimal("0.005"),  # BNB for gas
        min_balance_tokens={"cNGN": Decimal("10000"), "USDT": Decimal("10")},
    ),
    AccountRole.QUIDAX_LP: AccountConfig(
        role=AccountRole.QUIDAX_LP,
        derivation_path="m/44'/60'/0'/3/1",
        chain_id=56,  # BSC
        rpc_url=settings.bsc_rpc_url,
        tokens=["cNGN", "USDT"],
        min_balance_eth=Decimal("0.005"),  # BNB for gas
        min_balance_tokens={"cNGN": Decimal("50000"), "USDT": Decimal("50")},
    ),
    AccountRole.UNI_BSC_LP: AccountConfig(
        role=AccountRole.UNI_BSC_LP,
        derivation_path="m/44'/60'/0'/4/0",
        chain_id=56,  # BSC
        rpc_url=settings.bsc_rpc_url,
        tokens=["cNGN", "USDT"],
        min_balance_eth=Decimal("0.001"),  # BNB for gas
        min_balance_tokens={"cNGN": Decimal("50000"), "USDT": Decimal("50")},
    ),
    AccountRole.UNI_BSC_TRADE: AccountConfig(
        role=AccountRole.UNI_BSC_TRADE,
        derivation_path="m/44'/60'/0'/4/1",
        chain_id=56,  # BSC
        rpc_url=settings.bsc_rpc_url,
        tokens=["cNGN", "USDT"],
        min_balance_eth=Decimal("0.001"),  # BNB for gas
        min_balance_tokens={"cNGN": Decimal("10000"), "USDT": Decimal("10")},
    ),
}


@dataclass
class AccountBalance:
    """Balance information for an account."""

    role: str
    address: str
    chain_id: int
    native_balance: Decimal  # ETH or native token
    native_symbol: str
    token_balances: dict[str, Decimal]
    needs_refill: bool
    refill_reasons: list[str]


class AccountManager:
    """
    Manages HD-derived accounts for multi-venue trading.

    Derives all accounts from a single seed phrase using BIP44 paths.
    Supports both test mode (Anvil mnemonic) and production mode.
    """

    def __init__(
        self,
        mnemonic: Optional[str] = None,
        use_test_accounts: bool = False,
        account_configs: Optional[dict[AccountRole, AccountConfig]] = None,
    ):
        """
        Initialize account manager.

        Args:
            mnemonic: BIP39 mnemonic phrase (24 words recommended for production)
            use_test_accounts: If True, use Anvil's test mnemonic
            account_configs: Custom account configurations (uses defaults if not provided)
        """
        if use_test_accounts:
            self._mnemonic = ANVIL_TEST_MNEMONIC
            logger.warning("using_test_mnemonic", warning="DO NOT USE IN PRODUCTION")
        elif mnemonic:
            self._mnemonic = mnemonic
        else:
            # Try to load from environment
            self._mnemonic = os.environ.get("WALLET_MNEMONIC", "")
            if not self._mnemonic:
                raise ValueError(
                    "No mnemonic provided. Set WALLET_MNEMONIC env var or pass mnemonic parameter."
                )

        self._configs = account_configs or DEFAULT_ACCOUNT_CONFIGS
        self._accounts: dict[AccountRole, LocalAccount] = {}
        self._web3_instances: dict[int, Web3] = {}  # chain_id -> Web3
        self._decimals_cache: dict[str, int] = {}  # contract_address -> decimals (immutable)

        # Derive all accounts
        self._derive_accounts()

    def _derive_accounts(self) -> None:
        """Derive all configured accounts from the mnemonic."""
        for role, config in self._configs.items():
            account = Account.from_mnemonic(
                self._mnemonic,
                account_path=config.derivation_path,
            )
            self._accounts[role] = account
            logger.info(
                "account_derived",
                role=role.value,
                address=account.address,
                path=config.derivation_path,
            )

    def _get_web3(self, chain_id: int, rpc_url: str) -> Web3:
        """Get or create Web3 instance for a chain."""
        if chain_id not in self._web3_instances:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if chain_id in (56, 97):  # BSC mainnet and testnet are POA chains
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            self._web3_instances[chain_id] = w3
        return self._web3_instances[chain_id]

    def get_account(self, role: AccountRole) -> LocalAccount:
        """Get the account for a specific role."""
        if role not in self._accounts:
            raise ValueError(f"No account configured for role: {role}")
        return self._accounts[role]

    def get_private_key(self, role: AccountRole) -> str:
        """Get the private key for a specific role (hex string with 0x prefix)."""
        account = self.get_account(role)
        return str(account.key.hex())

    def get_address(self, role: AccountRole) -> str:
        """Get the address for a specific role."""
        account = self.get_account(role)
        return str(account.address)

    def get_config(self, role: AccountRole) -> AccountConfig:
        """Get the configuration for a specific role."""
        if role not in self._configs:
            raise ValueError(f"No config for role: {role}")
        return self._configs[role]

    def list_accounts(self) -> dict[str, str]:
        """List all account roles and their addresses."""
        return {role.value: account.address for role, account in self._accounts.items()}

    async def get_balance(
        self,
        role: AccountRole,
        token_contracts: Optional[dict[Any, Any]] = None,
    ) -> AccountBalance:
        """
        Get balance for an account including native and token balances.

        Args:
            role: Account role to check
            token_contracts: Either dict[str, str] (legacy) or dict[int, dict[str, str]] keyed by chain_id

        Returns:
            AccountBalance with current balances and refill status
        """
        config = self.get_config(role)
        account = self.get_account(role)
        w3 = self._get_web3(config.chain_id, config.rpc_url)

        # Support both flat {symbol: addr} and chain-keyed {chain_id: {symbol: addr}}
        if token_contracts and isinstance(next(iter(token_contracts)), int):
            token_contracts = token_contracts.get(config.chain_id, {})

        # Get native balance
        native_balance_wei = w3.eth.get_balance(account.address)
        native_balance = Decimal(native_balance_wei) / Decimal(10**18)

        # Determine native symbol based on chain
        native_symbols = {
            1: "ETH",
            8453: "ETH",  # Base uses ETH
            56: "BNB",    # BSC
        }
        native_symbol = native_symbols.get(config.chain_id, "ETH")

        # Get token balances
        token_balances: dict[str, Decimal] = {}
        if token_contracts:
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "type": "function",
                },
                {
                    "constant": True,
                    "inputs": [],
                    "name": "decimals",
                    "outputs": [{"name": "", "type": "uint8"}],
                    "type": "function",
                },
            ]

            for symbol, contract_addr in token_contracts.items():
                if symbol not in config.tokens:
                    continue
                try:
                    contract = w3.eth.contract(
                        address=Web3.to_checksum_address(contract_addr),
                        abi=erc20_abi,
                    )
                    balance_raw = contract.functions.balanceOf(account.address).call()
                    if contract_addr not in self._decimals_cache:
                        self._decimals_cache[contract_addr] = contract.functions.decimals().call()
                    decimals = self._decimals_cache[contract_addr]
                    token_balances[symbol] = Decimal(balance_raw) / Decimal(10**decimals)
                except Exception as e:
                    logger.warning(
                        "token_balance_check_failed",
                        role=role.value,
                        token=symbol,
                        error=str(e),
                    )
                    token_balances[symbol] = Decimal("-1")  # Indicates error

        # Check if refill needed
        refill_reasons = []

        if native_balance < config.min_balance_eth:
            refill_reasons.append(
                f"Low {native_symbol}: {native_balance:.4f} < {config.min_balance_eth} min"
            )

        for symbol, min_balance in config.min_balance_tokens.items():
            current = token_balances.get(symbol, Decimal("0"))
            if current >= 0 and current < min_balance:
                refill_reasons.append(
                    f"Low {symbol}: {current:.2f} < {min_balance} min"
                )

        return AccountBalance(
            role=role.value,
            address=account.address,
            chain_id=config.chain_id,
            native_balance=native_balance,
            native_symbol=native_symbol,
            token_balances=token_balances,
            needs_refill=len(refill_reasons) > 0,
            refill_reasons=refill_reasons,
        )

    async def check_all_balances(
        self,
        token_contracts: Optional[dict[Any, Any]] = None,
    ) -> list[AccountBalance]:
        """
        Check balances for all accounts.

        Args:
            token_contracts: Dict of token symbol -> contract address

        Returns:
            List of AccountBalance for all configured accounts
        """
        balances = []
        for role in self._accounts.keys():
            try:
                balance = await self.get_balance(role, token_contracts)
                balances.append(balance)
            except Exception as e:
                logger.error("balance_check_failed", role=role.value, error=str(e))
        return balances

    async def transfer_erc20(
        self,
        role: AccountRole,
        token_address: str,
        to_address: str,
        amount: Decimal,
    ) -> str:
        """Transfer ERC20 tokens from a role account. Returns tx hash."""
        config = self.get_config(role)
        account = self.get_account(role)
        w3 = self._get_web3(config.chain_id, config.rpc_url)

        transfer_abi = [
            {
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [
                    {"name": "to", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "stateMutability": "nonpayable",
                "type": "function",
            },
        ]

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=transfer_abi
        )
        decimals = contract.functions.decimals().call()
        amount_raw = int(amount * Decimal(10**decimals))

        tx = contract.functions.transfer(
            Web3.to_checksum_address(to_address), amount_raw
        ).build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "chainId": config.chain_id,
            }
        )

        signed = w3.eth.account.sign_transaction(tx, account.key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        logger.info(
            "erc20_transfer_sent",
            role=role.value,
            token=token_address,
            to=to_address,
            amount=str(amount),
            tx_hash=tx_hash.hex(),
        )
        return str(tx_hash.hex())

    async def transfer_native(
        self,
        role: AccountRole,
        to_address: str,
        amount: Decimal,
    ) -> str:
        """Transfer native gas token (e.g., ETH/BNB) from a role account. Returns tx hash."""
        config = self.get_config(role)
        account = self.get_account(role)
        w3 = self._get_web3(config.chain_id, config.rpc_url)

        amount_wei = int(amount * Decimal(10**18))
        tx = {
            "from": account.address,
            "to": Web3.to_checksum_address(to_address),
            "value": amount_wei,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": config.chain_id,
            "gas": 21000,
            "gasPrice": w3.eth.gas_price,
        }

        signed = w3.eth.account.sign_transaction(tx, account.key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        logger.info(
            "native_transfer_sent",
            role=role.value,
            to=to_address,
            amount=str(amount),
            tx_hash=tx_hash.hex(),
        )
        return str(tx_hash.hex())

    def update_thresholds(
        self,
        role: AccountRole,
        min_balance_eth: Optional[Decimal] = None,
        min_balance_tokens: Optional[dict[str, Decimal]] = None,
    ) -> None:
        """
        Update refill thresholds for an account.

        Args:
            role: Account role to update
            min_balance_eth: New minimum native balance
            min_balance_tokens: New minimum token balances
        """
        if role not in self._configs:
            raise ValueError(f"No config for role: {role}")

        config = self._configs[role]
        if min_balance_eth is not None:
            config.min_balance_eth = min_balance_eth
        if min_balance_tokens is not None:
            config.min_balance_tokens.update(min_balance_tokens)

        logger.info(
            "thresholds_updated",
            role=role.value,
            min_eth=float(config.min_balance_eth),
            min_tokens={k: float(v) for k, v in config.min_balance_tokens.items()},
        )
