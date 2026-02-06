## What'sImplemented

### 1. BlockradarAdapter (engine/venues/wallet/blockradar.py)

Functionality:
  - get_rate(pair) - Fetches current buy/sell rates from Blockradar API
  - set_rate(pair, buy_rate, sell_rate) - Sets new swap rates
  - sync_rates(reference_price) - Auto-calculates and sets rates based on reference price ± spread
  - get_current_price() - Returns current rates as a PriceQuote
  - get_position() - Returns empty position (Blockradar is rate-setting only, not custody)

Configuration:

```py
WalletParams(
      spread_bps: int = 15  # 0.15% spread each direction
  )
```

### 2. Scheduler Integration

  - Runs every 300s (5 minutes) via _sync_blockradar_rates()
  - Fetches reference price from Bybit P2P
  - Calculates: buy_rate = reference - spread, sell_rate = reference + spread
  - Sets rates for both CNGN/USDT and CNGN/USDC pairs

### 3. Account Management

  - blockradar role defined in AccountManager (path: m/44'/60'/0'/2/0)
  - Balance monitoring with thresholds: 50k cNGN, $100 USDC, $100 USDT
  - Alerts when refill needed

---
## What's Missing / Needs Work

### 1. No Actual Balance Tracking

The current get_position() returns zeros because Blockradar is treated as "rate-setting only":
```
  async def get_position(self) -> Position:
      # Blockradar doesn't hold funds - return empty position
      return Position(balances={"cngn": 0, "usdt": 0, "usdc": 0})
```

Reality: Blockradar does hold funds - users swap against your liquidity. We need to track:
  - How much cNGN/USDT/USDC is available for swaps
  - Net flow (are we accumulating cNGN or stablecoins?)

Needed: API integration to fetch wallet balances.

### 2. No Inventory Management

When users swap:
  - User buys cNGN → You receive USDT, lose cNGN
  - User sells cNGN → You receive cNGN, lose USDT

  Over time, you'll become imbalanced. Currently there's no:
  - Tracking of net position
  - Alerts when one side is depleting
  - Auto-hedging or rebalancing triggers

### 3. No Transaction/Swap Logging

No visibility into:
  - Individual swaps that occurred
  - Volume per pair
  - P&L from spread earned

Needed: Webhook receiver or polling for swap events.

### 4. No Cross-Venue Coordination

Per the PROBLEM_STATEMENT: 

> "Rates set independently rather than referencing other venues"

> "Should be part of cross-venue strategy"

The arbitrage detector compares Blockradar rates to other venues, but there's no:
  - Auto-adjustment of Blockradar rates if DEX price diverges significantly
  - Inventory-aware rate setting (widen spread if running low on one side)

### 5. Missing API Endpoints

The Blockradar API calls are placeholders. Need to verify:
  - Actual API endpoint URLs
  - Authentication method (currently assumes Bearer token)
  - Request/response formats
  - Rate limits

### 6. No Funding/Withdrawal Automation

Given Blockradar holds funds on their platform:
  - No API to withdraw excess funds to treasury
  - No API to deposit when running low

---
  
## What Needs to Be Built
  ┌──────────┬───────────────────────────────┬─────────────────────────────────────────────────┐
  │ Priority │            Feature            │                   Description                   │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P1       │ Balance fetching              │ Get actual balances from Blockradar             │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P1       │ API verification              │ Confirm actual Blockradar API endpoints/auth    │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P2       │ Swap event tracking           │ Log individual swaps for P&L and audit          │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P2       │ Inventory alerts              │ Alert when one side is depleting                │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P3       │ Dynamic spread                │ Widen spread when inventory is imbalanced       │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P3       │ Cross-venue rate coordination │ Adjust rates based on DEX/CEX prices            │
  ├──────────┼───────────────────────────────┼─────────────────────────────────────────────────┤
  │ P4       │ Funding automation            │ Auto-request refills or withdrawals             │
  └──────────┴───────────────────────────────┴─────────────────────────────────────────────────┘
---
  
## Questions to Clarify

1. What's the actual Blockradar API?
    - The current https://api.blockradar.co/v1 is a placeholder
    - Need real endpoints and auth method
2. How do swaps flow?
    - Does user interact with Blockradar UI?
    - Does Blockradar call our wallet to execute swaps?
    - Or do we pre-fund a pool on their platform?