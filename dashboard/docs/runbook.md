---
title: Deployment Runbook
order: 4
---

## Account Structure

One BIP39 mnemonic derives seven accounts. Set `WALLET_MNEMONIC` in `.env`.

| Role | Derivation path | Chain | Chain ID | Needs |
|---|---|---|---|---|
| `uni-base-lp` | m/44'/60'/0'/1/0 | Base | 8453 | ETH (gas), cNGN, USDC |
| `uni-base-trade` | m/44'/60'/0'/1/1 | Base | 8453 | ETH (gas), cNGN, USDC |
| `blockradar` | m/44'/60'/0'/2/0 | Base | 8453 | ETH (gas), cNGN, USDC — source for Blockradar deposits |
| `quidax-trade-fund` | m/44'/60'/0'/3/0 | BSC | 56 | BNB (gas), cNGN, USDT — source for Quidax arb deposits |
| `quidax-lp` | m/44'/60'/0'/3/1 | BSC | 56 | BNB (gas), cNGN, USDT — source for Quidax LP deposits |
| `uni-bsc-lp` | m/44'/60'/0'/4/0 | BSC | 56 | BNB (gas), cNGN, USDT |
| `uni-bsc-trade` | m/44'/60'/0'/4/1 | BSC | 56 | BNB (gas), cNGN, USDT |

To view all derived addresses and funding requirements:

```bash
docker run --rm --env-file /opt/repo/.env ghcr.io/lavavc/automated-infra:latest python3 scripts/show_accounts.py
```

## What needs funding and when

### Phase 1 (now) — price feeds + LP on Uniswap Base

Fund only **`uni-base-lp`** on Base:
- ETH: ≥ 0.001 (gas — bridged from Ethereum or bought on Base)
- cNGN: however much liquidity you want to deploy
- USDC: paired amount at the current price ratio

USDT is **not needed** for Base/Uniswap. The cNGN/USDC pair is used.

### Phase 2 (arbitrage execution)

Also fund **`uni-base-trade`** on Base (same tokens) for DEX swap legs.

### Phase 3 (Uniswap BSC)

Fund **`uni-bsc-lp`** and **`uni-bsc-trade`** on BSC:
- BNB: ≥ 0.001 (gas)
- cNGN: `0xa8aea66b361a8d53e8865c62d142167af28af058`
- USDT: `0x55d398326f99059fF775485246999027B3197955`

### External venues — depositing via the engine

**Blockradar**: Transfer USDC or cNGN from an HD wallet account to the Blockradar master wallet via `POST /api/venues/blockradar/deposit` (requires `ENGINE_API_TOKEN`). Blockradar's price quote API requires non-zero liquidity — price will show as unavailable until funded.

**Quidax**: The static deposit address is configured in `QUIDAX_DEPOSIT_ADDRESS`. Send cNGN or USDT on-chain from the `quidax-trade-fund` or `quidax-lp` HD wallet role or any external wallet. Quidax detects on-chain deposits asynchronously via webhook.

## Environment variables checklist

Required secrets — these have no code defaults and must be set:

```
WALLET_MNEMONIC=             # 12 or 24 word BIP39 phrase
BLOCKRADAR_API_KEY=          # from Blockradar dashboard
BLOCKRADAR_WALLET_ID=        # master wallet ID
BLOCKRADAR_DEPOSIT_ADDRESS=  # on-chain address to fund the master wallet (from Blockradar dashboard)
QUIDAX_API_KEY=              # from Quidax arb account settings
ENGINE_API_TOKEN=            # any secret string, protects direct API access
ALCHEMY_KEY=                 # recommended; otherwise public RPC nodes are used
TELEGRAM_BOT_TOKEN=          # from @BotFather
TELEGRAM_CHAT_ID=            # operator group chat ID (negative integer)
```

When enabling live trading, set in `engine/config.py`:
```python
arb_execute_cex_dex_enabled: bool = True   # or dex_dex
```

All other tunable parameters (arbitrage thresholds, scheduler intervals, fee estimates) have code defaults in `engine/config.py`. Override in `.env` only when the default needs changing for a specific deployment. See `.env.example` for a full list of overridable variables.

## Server setup (first time)

1. SSH onto the server as root.
2. Run the setup script:
   ```bash
   bash ./deploy/setup.sh
   ```
   The script will prompt you for:
   - **GitHub Actions runner token** — from **Settings → Actions → Runners → New self-hosted runner**
   - **Dashboard hostname** — e.g. `engine.yourdomain.com`
   - **Cloudflare login** — a URL will appear; open it in your browser to authenticate

3. Copy your `.env` to the server:
   ```bash
   cat .env | ssh root@<server-ip> "cat > /opt/repo/.env"
   ```

4. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com), create an **Access application**:
   - Application type: Self-hosted
   - Application domain: your hostname
   - Policy: Allow → Emails → add your email address

The dashboard is then accessible only after Cloudflare identity verification. The port `8000` is bound to `127.0.0.1` — not reachable from the public internet directly.

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

Configure in `engine/config.py` under `DexParams` defaults and restart the engine.

## Stopping and starting trading

All operational controls go through the Telegram operator bot. See [LP Operations](lp/operations) for the full command reference (`/pause`, `/resume`, `/withdraw`, `/shutdown`).

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

### LOW — quidax position sync disabled
`QuidaxAdapter.get_position()` returns a stub. When order-ladder trading on Quidax is
ready, restore the authenticated `/users/me/wallets` call and verify the API key has the
correct permission scope.
