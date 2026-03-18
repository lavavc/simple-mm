---
title: Problem Statement
order: 1
---

# CNGN Trading Operations: Problem Statement

## Overview

CNGN manages market-making positions across four venues to provide liquidity for the Nigerian Naira stablecoin. The goal is delta-neutral operation (~50/50 USD/NGN exposure) across a ~$800k portfolio while capturing 10-30 bps per transaction through cross-venue arbitrage.

---

## Positions & Venues

| Venue | Type | Pairs | Mechanism |
|-------|------|-------|-----------|
| **Uniswap (Base)** | DEX (Base) | cNGN/USDC | Concentrated LP with min/max range |
| **Uniswap (BSC)** | DEX (BSC) | cNGN/USDT | Concentrated LP with min/max range |
| **Quidax** | CEX | cNGN/USDT | Limit order book (market maker account) |
| **Blockradar** | Wallet System | cNGN/USDT, cNGN/USDC | Fixed swap rates (sole market maker) |

**Future pairs:** CNGN/ZARP, CNGN/IDRX (via USD intermediary)

---

## Venue-Specific Operations

### DEXs (Aerodrome & PancakeSwap) — *Biggest Operational Headache*

**Current Process:**
- Provide two-sided liquidity in concentrated LP positions
- Set min/max price range (~150 NGN wide, e.g., 1400-1550)
- Range set based on historical 5-6 month price bounds, not mathematical model
- Use separate wallet for market-making trades to adjust pool price
- When price exits range or pool imbalances: collapse position, reset range, rebalance

**Pain Points:**
1. **Constant manual intervention** — 2-5 adjustments daily during volatility
2. **Rebalancing coordination** — Must move funds between wallets when one side depletes
3. **Multi-sig friction** — 2-of-3 Safe wallets require coordinating multiple signers
4. **No price reference** — Manually checking external rates before adjusting
5. **Range management** — Wide ranges for convenience expose them to arbitrage/drain risk; tight ranges require constant updates
6. **Operational hours** — Volatility events require 2-3am interventions

### CEXes (Quidax for now, VALR later)

**Current Process:**
- Set ~40 limit orders across buy and sell sides
- Price ladder: current rate ±1, ±2, ±3... up to ±20 NGN
- 5% of allocated liquidity per price point
- Adjust 4-5 times daily during volatile periods

**Pain Points:**
1. **Manual order placement** — All orders set by hand via UI
2. **Frequent repricing** — Must cancel and replace orders as market moves
3. **Strategy iteration** — Still A/B testing optimal spread/depth configuration
4. **API unused** — Quidax has API but no programmatic integration yet

### Wallets (Blockradar, maybe other B2C providers later)

**Current Process:**
- Set fixed swap rate for each direction (CNGN→USDT, USDT→CNGN, etc.)
- CNGN is sole liquidity provider for swaps
- Earns ~15 bps each direction (~30 bps round-trip)

**Pain Points:**
1. **Rate isolation** — Rates set independently rather than referencing other venues
2. **Manual updates** — Price changes require manual intervention
3. **No arbitrage integration** — Should be part of cross-venue strategy

### CNGN ↔ NGN (On/Off Ramp)

**Status:** Well-optimized. API integration exists. Not a priority for automation.

---

## Global Challenges

### 1. No Clean USDT/NGN Price Feed

**The Problem:**
There is no reliable, clean source for the USDT/NGN rate.

- **Bybit P2P:** Primary reference, but first 5-10 orders are typically fraud
- **Filtering required:** Must check transaction count, reviews, completion rate
- **Azza dashboard:** Previously provided filtered rates but stopped working
- **CBN rate:** Not representative of actual market

**Impact:** Every pricing decision across all venues starts with an unreliable input.

### 2. No Global Position Dashboard

**The Problem:**
Portfolio state tracked in Google Sheets. No real-time view of:
- Total exposure by currency (CNGN, USDT, USDC, NGN)
- Position sizes per venue
- Delta neutrality status
- P&L by venue

**Impact:** Cannot quickly assess if rebalancing is needed or measure strategy performance.

### 3. Cross-Venue Arbitrage is Manual

**The Problem:**
Price discrepancies between venues (DEX vs CEX vs Blockradar) create arbitrage opportunities that require:
- Manually spotting the discrepancy
- Coordinating multi-sig transactions
- Executing across multiple interfaces

**Impact:** Arbitrage profits left on table; response time measured in minutes/hours, not seconds.

### 4. Rebalancing Friction

**The Problem:**
When liquidity depletes on one venue (e.g., all CNGN sold on Aerodrome):
1. Identify the imbalance
2. Determine source wallet with excess
3. Coordinate 2-of-3 multisig signatures
4. Execute transfer
5. Redeploy liquidity

**Impact:** Slow rebalancing during volatility = positions go stale, arbitraged against.

---

## Security Considerations

**Current State:**
- 2-of-3 Safe multisig wallets
- Keys in paper wallets (desk operations separate from reserve HSM)
- No automated execution (all manual)

**Desired State:**
- Hot wallets with limited funds (~10 trades worth)
- Automated execution for routine operations
- Monitoring shifts from "watch prices" to "watch funding levels"
- Reserve multisig only touched for periodic hot wallet refills

**Key Constraints:**
- Cannot expose large amounts to hot wallet risk
- Must maintain audit trail for compliance
- Multi-sig approval still required for significant fund movements

---

## Priority Summary

| Priority | Problem | Impact |
|----------|---------|--------|
| **P0** | Clean USDT/NGN price feed | Foundation for all pricing decisions |
| **P1** | Global position dashboard | Cannot measure or manage what you can't see |
| **P2** | DEX position management | 2-5x daily manual intervention, 24/7 |
| **P2** | Quidax order automation | 4-5x daily manual order updates |
| **P2** | Cross-venue rate synchronization | Blockradar rates isolated from strategy |
| **P3** | Automated cross-venue arbitrage | Revenue optimization opportunity |

---

## Success Criteria

A solution should:

1. **Reduce manual intervention** from 5+ times/day to 1-2 monitoring checks
2. **Provide unified price reference** that all venue operations use
3. **Show global position state** in real-time across all venues
4. **Automate routine rebalancing** within pre-approved risk limits
5. **Maintain security** through limited hot wallet exposure and audit trails
6. **Be extensible** to new venues (Valr, Busha, future ZARP pairs) and new pairs