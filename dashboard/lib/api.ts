import type {
  PriceSnapshot,
  VenuePriceResponse,
  SystemStatus,
  GlobalPosition,
  Position,
  ArbitrageStatus,
  ArbitrageOpportunity,
  ArbitrageHistoryItem,
  AccountInfo,
  AccountBalance,
  Alert,
  HealthCheck,
  BlendedPriceResponse,
  NormalizedPriceResponse,
  PoolMetricPoint,
} from '@/types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '/api';

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

export const api = {
  // Status
  getStatus: (): Promise<SystemStatus> =>
    fetchJson(`${API_BASE}/status`),

  getHealth: (): Promise<HealthCheck> =>
    fetchJson(`${API_BASE}/health`),

  // Prices
  getPrices: (): Promise<VenuePriceResponse[]> =>
    fetchJson(`${API_BASE}/prices`),

  getVenuePrice: (venue: string): Promise<VenuePriceResponse> =>
    fetchJson(`${API_BASE}/prices/${venue}`),

  getBlendedPrice: (): Promise<BlendedPriceResponse> =>
    fetchJson(`${API_BASE}/prices/blended`),

  getNormalizedPrices: (): Promise<NormalizedPriceResponse[]> =>
    fetchJson(`${API_BASE}/prices/normalized`),

  getPriceHistory: (params?: {
    from_ts?: number;
    to_ts?: number;
    limit?: number;
  }): Promise<PriceSnapshot[]> => {
    const searchParams = new URLSearchParams();
    if (params?.from_ts) searchParams.set('from_ts', String(params.from_ts));
    if (params?.to_ts) searchParams.set('to_ts', String(params.to_ts));
    if (params?.limit) searchParams.set('limit', String(params.limit));
    const query = searchParams.toString();
    return fetchJson(`${API_BASE}/price/history${query ? `?${query}` : ''}`);
  },

  // Positions
  getPositions: (): Promise<Position[]> =>
    fetchJson(`${API_BASE}/positions`),

  getGlobalPosition: (): Promise<GlobalPosition> =>
    fetchJson(`${API_BASE}/positions/global`),

  getVenuePosition: (venue: string): Promise<Position> =>
    fetchJson(`${API_BASE}/positions/${venue}`),

  // Arbitrage
  getArbitrageStatus: (): Promise<ArbitrageStatus> =>
    fetchJson(`${API_BASE}/arbitrage/status`),

  getOpportunities: (params?: {
    status?: string;
    from_ts?: number;
    to_ts?: number;
    limit?: number;
  }): Promise<ArbitrageOpportunity[]> => {
    const searchParams = new URLSearchParams();
    if (params?.status) searchParams.set('status', params.status);
    if (params?.from_ts) searchParams.set('from_ts', String(params.from_ts));
    if (params?.to_ts) searchParams.set('to_ts', String(params.to_ts));
    if (params?.limit) searchParams.set('limit', String(params.limit));
    const query = searchParams.toString();
    return fetchJson(`${API_BASE}/arbitrage/opportunities${query ? `?${query}` : ''}`);
  },

  getArbHistory: (params?: {
    pipeline?: string;
    from_ts?: number;
    to_ts?: number;
    limit?: number;
  }): Promise<ArbitrageHistoryItem[]> => {
    const searchParams = new URLSearchParams();
    if (params?.pipeline) searchParams.set('pipeline', params.pipeline);
    if (params?.from_ts) searchParams.set('from_ts', String(params.from_ts));
    if (params?.to_ts) searchParams.set('to_ts', String(params.to_ts));
    if (params?.limit) searchParams.set('limit', String(params.limit));
    const query = searchParams.toString();
    return fetchJson(`${API_BASE}/arbitrage/history${query ? `?${query}` : ''}`);
  },

  getPortfolioValuation: (): Promise<any> =>
    fetchJson(`${API_BASE}/arbitrage/liquidation`),

  // Accounts
  getAccounts: (): Promise<AccountInfo[]> =>
    fetchJson(`${API_BASE}/accounts`),

  getAccountBalances: (): Promise<AccountBalance[]> =>
    fetchJson(`${API_BASE}/accounts/balances`),

  getAccountBalance: (role: string): Promise<AccountBalance> =>
    fetchJson(`${API_BASE}/accounts/${role}/balance`),

  // Pool metrics history
  getPoolMetricsHistory: (minutes: number): Promise<PoolMetricPoint[]> =>
    fetchJson(`${API_BASE}/pool-metrics/history?minutes=${minutes}`),

  // Alerts
  getAlerts: (limit = 20): Promise<Alert[]> =>
    fetchJson(`${API_BASE}/alerts?limit=${limit}`),

  acknowledgeAlert: (id: number): Promise<{ status: string; alert_id: number }> =>
    fetchJson(`${API_BASE}/alerts/${id}/acknowledge`, { method: 'POST' }),

  // Actions
  getActions: (params?: {
    venue?: string;
    action_type?: string;
    limit?: number;
  }): Promise<unknown[]> => {
    const searchParams = new URLSearchParams();
    if (params?.venue) searchParams.set('venue', params.venue);
    if (params?.action_type) searchParams.set('action_type', params.action_type);
    if (params?.limit) searchParams.set('limit', String(params.limit));
    const query = searchParams.toString();
    return fetchJson(`${API_BASE}/actions${query ? `?${query}` : ''}`);
  },
};
