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

   Public ingress (TLS + vhost) is handled by the host's existing **nginx-proxy +
   acme-companion** stack. The engine container self-registers via the
   `VIRTUAL_HOST` / `VIRTUAL_PORT` / `LETSENCRYPT_HOST` / `LETSENCRYPT_EMAIL` env vars
   in `docker-compose.yml`, and must be attached to nginx-proxy's Docker network (the
   `networks:` block in `docker-compose.yml`). Confirm the network name with:
   ```bash
   docker inspect nginx-proxy --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
   ```
   The engine itself stays bound to `127.0.0.1:8000`; nginx-proxy reaches it over the
   shared Docker network.

3. At your DNS provider, create an **A record** for `your-domain.com` pointing to the
   server's public IP.

4. Copy your `.env` to the server:
   ```bash
   cat .env | ssh root@<server-ip> "cat > /opt/repo/.env"
   ```

The dashboard is **public and read-only** — anyone can view it, no login. Mutating
API endpoints require `ENGINE_API_TOKEN`. The one unauthenticated state-mutating
endpoint, `POST /api/webhooks/quidax`, is locked to `QUIDAX_WEBHOOK_ALLOWED_IPS`
(default the server's own IP), enforced against the `X-Real-IP` header that
nginx-proxy sets to the real client address; update that setting if the source moves.

## CI/CD pipeline

The [CI / Deploy workflow](https://github.com/lavavc/automated-infra/blob/main/.github/workflows/deploy.yml) runs on pull requests, pushes to `main`, and pushes of release tags matching `v*`.

1. **test** job — builds the `typecheck` Docker target to run strict `mypy` on `engine/`, then builds the `test` Docker target to run the default pytest suite. The suite skips `tests/test_dex_fork.py`, which requires Foundry's `anvil` plus fork-capable RPC endpoints.
2. **deploy** job — runs only for a pushed `v*` tag after tests pass. It uses the `production` environment and a self-hosted runner. Configure required reviewers on that environment if release approval is required; referencing the environment alone does not require manual approval.
3. Deployment builds and pushes the `production` image as both `ghcr.io/lavavc/automated-infra:latest` and a commit-SHA tag. The runner copies `docker-compose.yml` to `/opt/repo/`, then runs `docker compose pull` and `docker compose up -d --remove-orphans` against that file.

Merge changes to `main` after CI passes; merging does not deploy. When ready to release, tag the intended commit on `main` with a new `v*` version and push that tag. CI runs again before deployment. There is no need to cancel CI to avoid deploying a normal branch or `main` push.

UI hotfixes copied directly into a running container survive a container restart but are replaced when the container is recreated. Include their source changes in the next tagged release. Venue parameters saved through `PUT /api/venues/{venue}/params`, including LP rebalance thresholds, are persisted in the mounted database and restored at startup.

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
python -m engine
```

## Known issues to fix before trading real money

### HIGH — infinite token approvals
`engine/lp/uniswap_v4.py` (`V4PositionManager._approve_lp_tokens_if_needed`) approves `2**256 - 1` (unlimited) for each token before the
first swap or mint. If the router contract were compromised the entire wallet balance would
be at risk. Should approve only the amount needed per transaction.

### LOW — quidax position sync disabled
`QuidaxAdapter.get_position()` returns a stub. When order-ladder trading on Quidax is
ready, restore the authenticated `/users/me/wallets` call and verify the API key has the
correct permission scope.

---

## Diving deeper

For a map of how the code is structured — which directories own which concerns, the import rules between layers, and where to start reading for each subsystem — see [Architecture](architecture). For details on LP strategy parameters and rebalance logic, see [Liquidity Provision](lp). For arb detection and execution, see [Arbitrage](arbitrage).
