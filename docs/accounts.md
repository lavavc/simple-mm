# Account Management

HD wallet-based account management for secure, auditable multi-venue trading.

## Overview

The system uses **BIP44 HD wallet derivation** to generate all trading accounts from a single seed phrase. This provides:

- **Single backup**: One seed phrase to secure
- **Deterministic**: Same seed always generates same accounts
- **Role separation**: Different accounts for LP, trading, each venue
- **Anvil compatible**: Use test seed for local development

**Important**: Treasury is managed off-server. Only "hot" accounts with limited funds run on the server. The system surfaces alerts when accounts need refilling.

## Architecture

```
Master Seed (12 or 24 words, stored securely)
│
├── m/44'/60'/0'/1/0  → Aerodrome LP      (cNGN + USDC)
├── m/44'/60'/0'/1/1  → Aerodrome Trade   (cNGN + USDC)
├── m/44'/60'/0'/2/0  → Blockradar        (cNGN + USDT + USDC)
├── m/44'/60'/0'/3/0  → Quidax            (cNGN + USDT)
├── m/44'/60'/0'/4/0  → PancakeSwap LP    (cNGN + USDT, BSC)
└── m/44'/60'/0'/4/1  → PancakeSwap Trade (cNGN + USDT, BSC)
```

## Account Roles

| Role | Path | Chain | Tokens | Purpose |
|------|------|-------|--------|---------|
| `aerodrome-lp` | m/44'/60'/0'/1/0 | Base (8453) | cNGN, USDC | Liquidity provision |
| `aerodrome-trade` | m/44'/60'/0'/1/1 | Base (8453) | cNGN, USDC | Arbitrage swaps |
| `blockradar` | m/44'/60'/0'/2/0 | Base (8453) | cNGN, USDT, USDC | Wallet system funding |
| `quidax` | m/44'/60'/0'/3/0 | Ethereum (1) | cNGN, USDT | CEX deposit address |
| `pancakeswap-lp` | m/44'/60'/0'/4/0 | BSC (56) | cNGN, USDT | Liquidity provision (BSC) |
| `pancakeswap-trade` | m/44'/60'/0'/4/1 | BSC (56) | cNGN, USDT | Arbitrage swaps (BSC) |

## Configuration

### Environment Variables

```bash
# Use Anvil's test mnemonic (for local development only!)
USE_TEST_ACCOUNTS=true

# Production: Your 24-word BIP39 mnemonic
WALLET_MNEMONIC="word1 word2 word3 ... word24"

# Balance check interval (seconds)
BALANCE_CHECK_INTERVAL=300

# Token contract addresses (Base chain)
CNGN_CONTRACT_ADDRESS=0x46C85152bFe9f96829aA94755D9f915F9B10EF5F
USDC_CONTRACT_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
USDT_CONTRACT_ADDRESS=0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2
```

### Test Mode (Anvil)

For local development with Anvil:

```bash
USE_TEST_ACCOUNTS=true
```

This uses Anvil's default mnemonic:
```
test test test test test test test test test test test junk
```

**Never use test accounts in production.**

## Refill Thresholds

Each account has configurable minimum balance thresholds. When balances fall below these, alerts are created.

### Default Thresholds

| Role | Min ETH | Min cNGN | Min USDC | Min USDT |
|------|---------|----------|----------|----------|
| aerodrome-lp | 0.005 | 10,000 | 100 | - |
| aerodrome-trade | 0.005 | 5,000 | 50 | - |
| blockradar | 0.005 | 50,000 | 100 | 100 |
| quidax | 0.01 | 100,000 | - | 500 |

### Updating Thresholds

Via API:

```bash
PUT /api/accounts/aerodrome-lp/thresholds
Content-Type: application/json

{
  "min_balance_eth": "0.01",
  "min_balance_tokens": {
    "cNGN": "20000",
    "USDC": "200"
  }
}
```

## API Endpoints

### List Accounts

```bash
GET /api/accounts
```

Returns all configured accounts with addresses and derivation paths.

### Get Account Details

```bash
GET /api/accounts/{role}
```

Returns account info for a specific role.

### Check Balances

```bash
# All accounts
GET /api/accounts/balances

# Specific account
GET /api/accounts/{role}/balance
```

Returns current balances and refill status.

### Update Thresholds

```bash
PUT /api/accounts/{role}/thresholds
```

Requires authentication token.

## Refill Workflow

1. **Automatic Detection**: Balance monitoring runs every 5 minutes (configurable)

2. **Alert Creation**: When balance < threshold:
   - Alert stored in database (category: "refill")
   - WebSocket broadcast to dashboard
   - Logged with warning severity

3. **Manual Refill**:
   - View alerts via `GET /api/alerts?category=refill`
   - Transfer funds from treasury to the account address
   - Acknowledge alert via `POST /api/alerts/{id}/acknowledge`

4. **Next Check**: Balance monitoring confirms refill on next cycle

## WebSocket Events

### Refill Alert

```json
{
  "type": "refill_alert",
  "data": {
    "role": "aerodrome-lp",
    "address": "0x...",
    "chain_id": 8453,
    "native_balance": 0.003,
    "token_balances": {"cNGN": 5000, "USDC": 25},
    "reasons": ["Low ETH: 0.003 < 0.005 min", "Low cNGN: 5000 < 10000 min"]
  }
}
```

### Balance Update

```json
{
  "type": "account_balances",
  "data": [
    {
      "role": "aerodrome-lp",
      "address": "0x...",
      "chain_id": 8453,
      "native_balance": 0.05,
      "native_symbol": "ETH",
      "token_balances": {"cNGN": 50000, "USDC": 500},
      "needs_refill": false
    }
  ]
}
```

## Security Considerations

### Seed Phrase Storage

**Do NOT**:
- Store seed phrase in `.env` file in version control
- Log the seed phrase
- Share the seed phrase over insecure channels

**Do**:
- Use environment variables injected at runtime
- Use a secrets manager (AWS Secrets Manager, HashiCorp Vault, etc.)
- Keep backups in secure offline storage

### Hot Wallet Limits

Keep minimal funds in hot wallets:
- Only enough for daily operations
- Treasury holds bulk of funds offline
- Refill manually when needed

### Key Rotation

If you suspect compromise:
1. Generate new seed phrase
2. Update derivation in AccountManager
3. Transfer funds to new addresses
4. Update venue configurations

## Local Development with Anvil

Start Anvil:

```bash
anvil --fork-url https://mainnet.base.org
```

Configure `.env`:

```bash
USE_TEST_ACCOUNTS=true
BASE_RPC_URL=http://127.0.0.1:8545
```

The first 10 Anvil accounts are derived from the test mnemonic. The AccountManager uses the same derivation paths, so accounts will match.

## Troubleshooting

### "No mnemonic provided"

Either set `WALLET_MNEMONIC` environment variable or enable `USE_TEST_ACCOUNTS=true` for development.

### Balance shows -1

Token contract call failed. Check:
- RPC endpoint is accessible
- Token contract address is correct
- Chain ID matches

### Accounts not matching Anvil

Ensure derivation paths match. Anvil uses:
- Account 0: m/44'/60'/0'/0/0
- Account 1: m/44'/60'/0'/0/1
- etc.

Our accounts use different paths (m/44'/60'/0'/1/0, etc.) to separate by role.
