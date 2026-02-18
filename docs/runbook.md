# CNGN Engine — Deployment Runbook

## Account Structure

One BIP39 mnemonic derives five accounts. Set `WALLET_MNEMONIC` in `.env`.

| Role | Derivation path | Chain | Chain ID | Needs |
|---|---|---|---|---|
| `aerodrome-lp` | m/44'/60'/0'/1/0 | Base | 8453 | ETH (gas), cNGN, USDC |
| `aerodrome-trade` | m/44'/60'/0'/1/1 | Base | 8453 | ETH (gas), cNGN, USDC |
| `blockradar` | m/44'/60'/0'/2/0 | Base | 8453 | ETH (gas), cNGN, USDC — source for Blockradar deposits |
| `quidax` | m/44'/60'/0'/3/0 | Mainnet | 1 | ETH (gas), cNGN, USDT — source for Quidax deposits |
| `pancakeswap-lp` | m/44'/60'/0'/4/0 | BSC | 56 | BNB (gas), cNGN, USDT |

To view all derived addresses and funding requirements:

```bash
docker run --rm --env-file /opt/repo/.env ghcr.io/lavavc/automated-infra:latest python3 scripts/show_accounts.py
```

## What needs funding and when

### Phase 1 (now) — price feeds + LP on Aerodrome

Fund only **`aerodrome-lp`** on Base:
- ETH: ≥ 0.005 (gas — bridged from Ethereum or bought on Base)
- cNGN: however much liquidity you want to deploy
- USDC: paired amount at the current price ratio

USDT is **not needed** for Base/Aerodrome. The cNGN/USDC pair is used.

### Phase 2 (arbitrage execution, not yet live)

Also fund **`aerodrome-trade`** on Base (same tokens) for DEX swap legs.

### Phase 3 (PancakeSwap BSC, not yet live)

Fund **`pancakeswap-lp`** on BSC:
- BNB: ≥ 0.005 (gas)
- cNGN: `0xa8aea66b361a8d53e8865c62d142167af28af058`
- USDT: `0x55d398326f99059fF775485246999027B3197955`

### External venues — depositing via the engine

**Blockradar**: Send USDC or cNGN on Base from any HD wallet account:

```
POST /api/venues/blockradar/deposit
Authorization: Bearer <DASHBOARD_API_TOKEN>
{"role": "aerodrome-lp", "token": "USDC", "amount": "500"}
```

Funds go to `0x0839578d121a5b99ae5BF6dC604Bbf247E51C584`. Blockradar's price quote API requires non-zero liquidity — price will show as unavailable until funded.

**Quidax**: Fetch a deposit address for a currency, then send on-chain:

```
GET /api/venues/quidax/deposit-address/cngn
GET /api/venues/quidax/deposit-address/usdt
```

Quidax detects on-chain deposits asynchronously (webhook-based). Send from the `quidax` HD wallet role or any external wallet.

## Environment variables checklist

```
WALLET_MNEMONIC=           # 12 or 24 word BIP39 phrase
BLOCKRADAR_API_KEY=        # from Blockradar dashboard
BLOCKRADAR_WALLET_ID=      # master wallet ID
BLOCKRADAR_DEPOSIT_ADDRESS=  # on-chain address to fund the master wallet (from Blockradar dashboard)
QUIDAX_API_KEY=            # from Quidax account settings
DASHBOARD_API_TOKEN=       # any secret string, used for protected API calls
BASE_RPC_URL=              # defaults to https://mainnet.base.org (fine for now)
BSC_RPC_URL=               # defaults to https://bsc-dataseed.binance.org (fine for now)
```

## Server setup (first time)

1. SSH onto the server as root.
2. Run the setup script (creates `github-runner` user, installs Actions runner, skips ufw since Docker manages iptables directly):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/lavavc/automated-infra/main/deploy/setup.sh | bash
   ```
3. When prompted, paste the GitHub Actions runner token from **Settings → Actions → Runners → New self-hosted runner**.
4. Copy your `.env` to the server:
   ```bash
   cat .env | ssh root@<server-ip> "cat > /opt/repo/.env"
   ```

## CI/CD pipeline

Every push to `main` on [lavavc/automated-infra](https://github.com/lavavc/automated-infra):
1. **test** job — builds the `test` Docker target; runs `pytest` inside the container.
2. **deploy** job (only if tests pass) — builds and pushes the `production` image to `ghcr.io/lavavc/automated-infra:latest`, then SSHs to the server and runs `docker compose pull && docker compose up -d`.

## Capital allocation

Each DEX venue has two explicit fields controlling how much to deploy as liquidity:

| Field | Default | Meaning |
|---|---|---|
| `deploy_token0` | `0` | Absolute cNGN amount to use for LP |
| `deploy_token1` | `0` | Absolute USDC/USDT amount to use for LP |

Defaults to `0` — nothing is deployed until you explicitly configure amounts. The engine caps each value to the actual wallet balance, so you can safely set large numbers without risk of overdraft.

Set via the API (auth required):
```
PATCH /api/venues/aerodrome/params
{"deploy_token0": "500000", "deploy_token1": "600"}
```

## Starting the engine (local dev)

```bash
source .venv/bin/activate
python -m engine
```

## Known issues to fix before trading real money

### HIGH — infinite token approvals
`engine/venues/dex/base.py` approves `2**256 - 1` (unlimited) for each token before the
first swap or mint. If the router contract were compromised the entire wallet balance would
be at risk. Should approve only the amount needed per transaction.

### MEDIUM — arbitrage execution not implemented
`ArbitrageExecutor.execute_dex_buy/sell` raise `NotImplementedError`. The
`ARBITRAGE_EXECUTION_ENABLED` flag is currently `false` in `.env`, which is correct.
Do not set it to `true` until the executor is implemented.

### LOW — quidax position sync disabled
`QuidaxAdapter.get_position()` returns a stub. When order-ladder trading on Quidax is
ready, restore the authenticated `/users/me/wallets` call and verify the API key has the
correct permission scope.
