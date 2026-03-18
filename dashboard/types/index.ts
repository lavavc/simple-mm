// Types matching engine/api/schemas.py

export interface PriceQuote {
  source: string;
  timestamp: number;
  bid: number;
  ask: number;
  mid: number;
}

export interface LPPosition {
  token_id: string;
  liquidity: string;
  range_min: number;
  range_max: number;
  in_range: boolean;
  our_share_pct?: number;
}

export interface Position {
  venue: string;
  pair: string;
  timestamp: number;
  balances: Record<string, number>;
  lp_position?: LPPosition;
  open_orders?: Record<string, unknown>;
  pool_tvl_usd?: number;
  volume_24h_usd?: number;
  rates?: Record<string, number>;  // per-route cNGN/USD rates (blockradar only)
}

export interface VenuePriceResponse {
  venue: string;
  pair: string;
  quote?: PriceQuote;
  error?: string;
  age_seconds: number;
}

export interface VenueStatus {
  name: string;
  enabled: boolean;
  paused: boolean;
  last_action?: number;
  position?: Position;
  price?: VenuePriceResponse;
  params?: Record<string, unknown>;
}

export interface SystemStatus {
  trading_enabled: boolean;
  uptime: number;
  last_price_update?: number;
  venues: VenueStatus[];
}

export interface GlobalPosition {
  total_cngn: number;
  total_usdt: number;
  total_usdc: number;
  total_usd_value: number;
  delta_ratio: number;
  target_delta: number;
}

export interface Alert {
  id: number;
  timestamp: number;
  severity: 'info' | 'warning' | 'critical';
  category: string;
  message: string;
  acknowledged: boolean;
}

export interface ArbitrageParams {
  min_net_profit_bps: number;
  dex_swap_fee_bps: number;
  dex_slippage_bps: number;
  cex_taker_fee_bps: number;
  max_single_trade_usd: number;
  max_daily_volume_usd: number;
  max_inventory_imbalance_usd: number;
  scan_interval_seconds: number;
  max_consecutive_failures: number;
  max_daily_loss_usd: number;
}

export interface ArbitrageOpportunity {
  id: string;
  timestamp: number;
  buy_venue: string;
  sell_venue: string;
  buy_price: number;
  sell_price: number;
  gross_spread_bps: number;
  net_spread_bps: number;
  recommended_size_usd: number;
  expected_profit_usd: number;
  status: 'detected' | 'executing' | 'completed' | 'abandoned' | 'expired';
  actual_profit_usd?: number;
  reason?: string;
}

export interface ArbitrageStatus {
  enabled: boolean;
  execute_cex_dex: boolean;
  execute_dex_dex: boolean;
  detection_only?: boolean;
  last_scan_timestamp?: number;
  opportunities_detected_24h: number;
  opportunities_executed_24h: number;
  total_profit_24h_usd: number;
  daily_volume_usd: number;
  inventory_imbalance_usd: number;
  circuit_breaker_active: boolean;
  consecutive_failures: number;
  params: ArbitrageParams;
  low_inventory_venues: string[];
}

// Order Book Types
export interface OrderBookLevel {
  price: string;
  amount: string;
}

export interface OrderBookDepth {
  venue: string;
  pair: string;
  timestamp: number;
  bids: OrderBookLevel[];
  asks: OrderBookLevel[];
}

export interface AccountInfo {
  role: string;
  address: string;
  derivation_path: string;
  chain_id: number;
  tokens: string[];
}

export interface AccountBalance {
  role: string;
  address: string;
  chain_id: number;
  native_balance: number;
  native_symbol: string;
  token_balances: Record<string, number>;
  needs_refill: boolean;
  refill_reasons: string[];
}

export interface HealthCheck {
  status: string;
  timestamp: number;
  trading_enabled: boolean;
  arbitrage_enabled: boolean;
}

export interface BlendedPriceResponse {
  vwap: number;
  twap_5m: number;
  twap_1h: number;
  reference_price_ngn: number;
  venue_prices: Record<string, number>;
  timestamp: number;
  num_sources: number;
  total_venues: number;
  confidence: number;
}

export interface NormalizedPriceResponse {
  venue: string;
  cngn_usd: number;
  basis: string;
  raw_mid: number;
  timestamp: number;
}

export interface PoolMetrics {
  venue: string;
  chain: string;
  pool_tvl_usd: number | null;
  volume_24h_usd: number | null;
}

export interface PoolMetricPoint {
  timestamp: number;
  venue: string;
  pool_tvl_usd: number | null;
  volume_24h_usd: number | null;
}

/** Row from the price_snapshots table (returned by GET /api/price/history). */
export interface PriceSnapshot {
  id: number;
  timestamp: number;
  source: string;
  bid: number;
  ask: number;
  mid: number;
  metadata?: string | null;
}
