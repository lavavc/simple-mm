# Derived Research Artifacts

- `uni_base_pool_features.csv`: causal swap-level pool features for Base.
- `uni_bsc_pool_features.csv`: causal swap-level pool features for BSC.
- `uni_base_lp_ledger.csv`: paper-faithful LP lifecycle ledger for Base.
- `uni_bsc_lp_ledger.csv`: paper-faithful LP lifecycle ledger for BSC.
- `uni_base_tx_receipts.csv`: gas sidecar for Base LP lifecycle transactions.
- `uni_bsc_tx_receipts.csv`: gas sidecar for BSC LP lifecycle transactions.
- `native_token_usd_prices.csv`: optional local historical native gas-token
  price sidecar with `chain,timestamp_ms,native_token_usd,source`, produced by
  `research/scripts/build_native_token_price_sidecar.py`.
- `uni_base_lp_episode_features.csv`: Base paper LP episodes joined with receipt-backed gas fields.
- `uni_bsc_lp_episode_features.csv`: BSC paper LP episodes joined with receipt-backed gas fields.
