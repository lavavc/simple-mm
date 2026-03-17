import type {
  PriceSnapshot,
  VenuePriceResponse,
  SystemStatus,
  GlobalPosition,
  Position,
  ArbitrageStatus,
  ArbitrageOpportunity,
  AccountInfo,
  AccountBalance,
  Alert,
  HealthCheck,
  BlendedPriceResponse,
  NormalizedPriceResponse,
  PoolMetrics,
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

function authHeaders(token?: string): HeadersInit {
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

export const api = {
  // Status
  getStatus: (): Promise<SystemStatus> =>
    fetchJson(`${API_BASE}/status`),

  getHealth: (): Promise<HealthCheck> =>
    fetchJson(`${API_BASE}/health`),

  // Prices (per-venue)
  getPrices: (): Promise<VenuePriceResponse[]> =>
    fetchJson(`${API_BASE}/prices`),

  getVenuePrice: (venue: string): Promise<VenuePriceResponse> =>
    fetchJson(`${API_BASE}/prices/${venue}`),

  refreshPrices: (): Promise<VenuePriceResponse[]> =>
    fetchJson(`${API_BASE}/prices/refresh`, { method: 'POST' }),

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

  // Trading control
  // Venue control
  pauseVenue: (venue: string, token: string): Promise<{ venue: string; paused: boolean }> =>
    fetchJson(`${API_BASE}/venues/${venue}/pause`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  resumeVenue: (venue: string, token: string): Promise<{ venue: string; paused: boolean }> =>
    fetchJson(`${API_BASE}/venues/${venue}/resume`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  syncVenue: (venue: string, token: string): Promise<{ status: string; venue: string }> =>
    fetchJson(`${API_BASE}/venues/${venue}/sync`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

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

  getPortfolioValuation: (): Promise<any> =>
    fetchJson(`${API_BASE}/arbitrage/liquidation`),

  enableArbitrage: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/enable`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  disableArbitrage: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/disable`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  enableExecuteCexDex: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/execute-cex-dex/enable`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  disableExecuteCexDex: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/execute-cex-dex/disable`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  enableExecuteDexDex: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/execute-dex-dex/enable`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  disableExecuteDexDex: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/execute-dex-dex/disable`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  triggerScan: (token: string): Promise<{
    status: string;
    opportunities_found: number;
    opportunities: ArbitrageOpportunity[];
  }> =>
    fetchJson(`${API_BASE}/arbitrage/scan`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  resetCircuitBreaker: (token: string): Promise<{ status: string }> =>
    fetchJson(`${API_BASE}/arbitrage/reset-circuit-breaker`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

  // Accounts
  getAccounts: (): Promise<AccountInfo[]> =>
    fetchJson(`${API_BASE}/accounts`),

  getAccountBalances: (): Promise<AccountBalance[]> =>
    fetchJson(`${API_BASE}/accounts/balances`),

  getAccountBalance: (role: string): Promise<AccountBalance> =>
    fetchJson(`${API_BASE}/accounts/${role}/balance`),

  updateThresholds: (
    role: string,
    thresholds: { min_balance_eth?: number; min_balance_tokens?: Record<string, number> },
    token: string
  ): Promise<{ status: string; role: string }> =>
    fetchJson(`${API_BASE}/accounts/${role}/thresholds`, {
      method: 'PUT',
      headers: {
        ...authHeaders(token),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(thresholds),
    }),

  // Pool metrics (DexScreener)
  getPoolMetrics: (): Promise<PoolMetrics[]> =>
    fetchJson(`${API_BASE}/pool-metrics`),

  getPoolMetricsHistory: (minutes: number): Promise<PoolMetricPoint[]> =>
    fetchJson(`${API_BASE}/pool-metrics/history?minutes=${minutes}`),

  // Alerts
  getAlerts: (limit = 20): Promise<Alert[]> =>
    fetchJson(`${API_BASE}/alerts?limit=${limit}`),

  acknowledgeAlert: (id: number, token: string): Promise<{ status: string; alert_id: number }> =>
    fetchJson(`${API_BASE}/alerts/${id}/acknowledge`, {
      method: 'POST',
      headers: authHeaders(token),
    }),

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
