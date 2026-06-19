---
title: Deployment Runbook
order: 5
---

## Account Structure

One BIP39 mnemonic derives five on-chain accounts. Set `WALLET_MNEMONIC` in `.env`.

| Role | Derivation path | Chain | Chain ID | Needs |
|---|---|---|---|---|
| `uni-base-lp` | m/44'/60'/0'/1/0 | Base | 8453 | ETH (gas), cNGN, USDC |
| `uni-base-trade` | m/44'/60'/0'/1/1 | Base | 8453 | ETH (gas), cNGN, USDC |
| `blockradar` | m/44'/60'/0'/2/0 | Base | 8453 | ETH (gas), cNGN, USDC — source for Blockradar deposits |
| `uni-bsc-lp` | m/44'/60'/0'/4/0 | BSC | 56 | BNB (gas), cNGN, USDT |
| `uni-bsc-trade` | m/44'/60'/0'/4/1 | BSC | 56 | BNB (gas), cNGN, USDT |

To view all derived addresses and funding requirements:

```bash
docker run --rm --env-file /opt/repo/.env ghcr.io/lavavc/automated-infra:latest python3 scripts/show_accounts.py
```

## Inventory model

Global portfolio totals treat account roles and venue positions differently:

- on-chain inventory comes from the managed HD-wallet roles above
- deployed LP inventory is added separately from the `uni-base` and `uni-bsc` LP NFTs
- off-chain exchange inventory is added from `quidax` and, when separately configured, `quidax-lp`

In practice that means the DEX trade accounts, the Blockradar account, and any rare residual balances left on the DEX LP wallets are on-chain inventory. Quidax balances are off-chain exchange inventory and come from the configured Quidax user ids.

## What needs funding and when

### LP management

Fund the LP wallet for each pool that should be actively managed:

- **`uni-base-lp`** on Base: ETH for gas, cNGN, and USDC.
- **`uni-bsc-lp`** on BSC: BNB for gas, cNGN, and USDT.

The LP path deploys the full LP wallet balance. Single-token funding is valid:
the engine swaps to the required ratio before minting when the next LP cycle
runs.

### Arbitrage execution

Fund the trade wallets only when live DEX legs should execute:

- **`uni-base-trade`** on Base: ETH for gas, cNGN, and USDC.
- **`uni-bsc-trade`** on BSC: BNB for gas, cNGN, and USDT.

Quidax-side inventory is venue-local and independent from the on-chain trade
wallets. A fill on Quidax does not make inventory available on Base or BSC.

### External venues — depositing via the engine

**Blockradar**: Transfer USDC or cNGN from an HD wallet account to the Blockradar master wallet via `POST /api/venues/blockradar/deposit` (requires `ENGINE_API_TOKEN`). Blockradar's price quote API requires non-zero liquidity — price will show as unavailable until funded.

**Quidax**: The deposit addresses are configured via `QUIDAX_TRADE_ADDRESS` and `QUIDAX_LP_ADDRESS`. The engine routes orders and balance checks using the Sub-account User IDs (`QUIDAX_USER_ID` and `QUIDAX_LP_USER_ID`). You can configure the engine for trading-only, MMing-only, or both:
- **Trading only**: Set `QUIDAX_USER_ID`. Leave `QUIDAX_LP_USER_ID` empty.
- **MM'ing only (LP)**: Set `QUIDAX_LP_USER_ID`. Leave `QUIDAX_USER_ID` empty.
- **Both**: Set both IDs. They must have **distinct deposit addresses**, otherwise the engine will skip the LP venue to prevent double-counting.

Quidax detects on-chain deposits asynchronously via webhook.

## Environment variables checklist

Required secrets — these have no code defaults and must be set:

```
WALLET_MNEMONIC=             # 12 or 24 word BIP39 phrase
BLOCKRADAR_API_KEY=          # from Blockradar dashboard
BLOCKRADAR_WALLET_ID=        # master wallet ID
BLOCKRADAR_DEPOSIT_ADDRESS=  # on-chain address to fund the master wallet (from Blockradar dashboard)
QUIDAX_API_KEY=              # Operator API key from Quidax Account Settings
QUIDAX_USER_ID=              # (Optional) sub-account ID for trading. Leave empty to disable trade.
QUIDAX_LP_USER_ID=           # (Optional) sub-account ID for MM'ing. Leave empty to disable MM.
QUIDAX_TRADE_ADDRESS=        # trade deposit address
QUIDAX_LP_ADDRESS=           # MM'ing deposit address
ENGINE_API_TOKEN=            # any secret string, protects direct API access
ALCHEMY_KEY=                 # recommended; otherwise public RPC nodes are used
TELEGRAM_BOT_TOKEN=          # from @BotFather
TELEGRAM_CHAT_ID=            # operator group chat ID (negative integer)
```

Live trading toggles are loaded through `engine/config.py` and can be overridden
from `.env`:

```bash
ARB_DETECTION_ENABLED=true
ARB_EXECUTE_CEX_DEX_ENABLED=true
ARB_EXECUTE_DEX_DEX_ENABLED=true
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

Every pull request and every push to `main` on [lavavc/automated-infra](https://github.com/lavavc/automated-infra):
1. **test** job — builds the `typecheck` Docker target to run strict `mypy` on `engine/`, then builds the `test` Docker target to run the default pytest suite.
2. The default pytest run intentionally skips `tests/test_dex_fork.py`, because those tests require Foundry's `anvil` plus fork-capable RPC endpoints.
3. **deploy** job (only on pushes to `main`, and only if the checks pass) — builds and pushes the `production` image to `ghcr.io/lavavc/automated-infra:latest`, then SSHs to the server and runs `docker compose pull && docker compose up -d`.

## Capital allocation

The engine deploys the full LP wallet balance for each venue. There are no separate `deploy_token0` / `deploy_token1` knobs in the live LP path.

Operationally that means:

- fund the LP wallet with however much capital should be deployed
- keep trade wallets separate for arb execution
- if tokens remain on an LP wallet after an unwind or failed mint, they count as on-chain inventory until redeployed

## Stopping and starting trading

All operational controls go through the Telegram operator bot. See [LP Operations](lp/operations) for the full command reference (`/pause`, `/resume`, `/withdraw`, `/shutdown`).

## Starting the engine (local dev)

```bash
source .venv/bin/activate
python -m engine.main
```

## Known operational risk

### Infinite token approvals
`engine/lp/uniswap_v4.py` (`V4PositionManager._approve_lp_tokens_if_needed`) approves `2**256 - 1` (unlimited) for each token before the
first swap or mint. If the router contract were compromised the entire wallet balance would
be at risk. Should approve only the amount needed per transaction.

---

## Diving deeper

For a map of how the code is structured — which directories own which concerns, the import rules between layers, and where to start reading for each subsystem — see [Architecture](architecture). For details on LP strategy parameters and rebalance logic, see [Liquidity Provision](lp). For arb detection and execution, see [Arbitrage](arbitrage).
