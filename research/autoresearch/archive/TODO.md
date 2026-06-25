- For Dashboard:
    - Track LP position value for every change in sqrt price -> historical LP value time series for base and bsc positions
    - Global (c)NGN delta/inventory imbalance on mainpage

- [x] Change validation score to net return or ROI or ln(fees/transaction costs) + ROI? — H6: composite = net_return − max_dd + 0.001·clamp(ln(fees/tx_cost)) + fee/cost eligibility gate
- [ ] Change width and half-life to be normalized by swaps rather than time/blocks — filed as H11 in `research/autoresearch/archive/lp-backtester-research-log.md`
- [x] Calculate PBO! — CSCV tooling done (`--matrix-output` + research/scripts/compute_pbo.py); first numbers come with the extended-data re-run
- [x] 4.25% APR is benchmark (sGHO) — per-config APY now tracked (mean/median validation APY columns)
- [x] Combinatorial Purged Cross-Validation — CSCV over contiguous window blocks in research/backtester/pbo.py (purging beyond block contiguity not yet needed at current window sizes)
