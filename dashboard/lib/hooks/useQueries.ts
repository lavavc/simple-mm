'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

// ── Queries ─────────────────────────────────────────────────────────────────
// No refetchInterval — all updates are pushed via WebSocket (see useEventStream).
// History queries use a short staleTime so they refresh on window focus.

export const LAST_EVENT_PACKET_QUERY_KEY = ['eventStreamLastPacket'] as const;

// 5 venues × 2 ticks/min = ~10 rows/min, capped at 5000
function priceHistoryLimit(windowMinutes: number | undefined): number {
  if (!windowMinutes || !isFinite(windowMinutes)) return 5000;
  return Math.min(Math.ceil(windowMinutes * 5 * 2), 5000);
}

export function usePriceHistory(windowMinutes?: number) {
  const fromTs = windowMinutes && isFinite(windowMinutes)
    ? Date.now() - windowMinutes * 60 * 1000
    : undefined;
  return useQuery({
    queryKey: ['priceHistory', windowMinutes ?? 'all'],
    queryFn: () => api.getPriceHistory({ from_ts: fromTs, limit: priceHistoryLimit(windowMinutes) }),
    refetchInterval: 30_000,
    staleTime: 25_000,
  });
}

export function useLastEventPacket() {
  return useQuery<number | null>({
    queryKey: LAST_EVENT_PACKET_QUERY_KEY,
    queryFn: async () => null,
    staleTime: Infinity,
    gcTime: Infinity,
  });
}

export function useStatus() {
  return useQuery({
    queryKey: ['status'],
    queryFn: api.getStatus,
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function useVenueOrders(venue: string, enabled = true) {
  return useQuery({
    queryKey: ['venueOrders', venue],
    queryFn: () => api.getVenueOrders(venue),
    enabled: enabled && Boolean(venue),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function useQuidaxDepth() {
  return useQuery({
    queryKey: ['quidaxDepth'],
    queryFn: api.getQuidaxDepth,
    staleTime: Infinity,
  });
}

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: api.getHealth });
}

export function usePrices() {
  return useQuery({ queryKey: ['prices'], queryFn: api.getPrices });
}

export function useBlendedPrice() {
  return useQuery({
    queryKey: ['blendedPrice'],
    queryFn: api.getBlendedPrice,
    staleTime: 30_000,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function useNormalizedPrices() {
  return useQuery({ queryKey: ['normalizedPrices'], queryFn: api.getNormalizedPrices });
}

export function usePortfolioExposure() {
  return useQuery({
    queryKey: ['portfolioExposure'],
    queryFn: api.getPortfolioExposure,
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function useArbitrageStatus() {
  return useQuery({ queryKey: ['arbitrageStatus'], queryFn: api.getArbitrageStatus });
}

export function useOpportunities(limit = 50) {
  return useQuery({
    queryKey: ['opportunities', limit],
    queryFn: () => api.getOpportunities({ limit }),
  });
}

export function useArbHistory(limit = 30, pipeline?: 'cex_dex' | 'dex_dex') {
  return useQuery({
    queryKey: ['arbHistory', pipeline, limit],
    queryFn: () => api.getArbHistory({ limit, pipeline }),
  });
}

export function useAccountBalances() {
  return useQuery({ queryKey: ['accountBalances'], queryFn: api.getAccountBalances });
}

export function useAlerts(limit = 20) {
  return useQuery({
    queryKey: ['alerts', limit],
    queryFn: () => api.getAlerts(limit),
  });
}

export function usePortfolioValuation() {
  return useQuery({
    queryKey: ['portfolioValuation'],
    queryFn: api.getPortfolioValuation,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}

export function usePoolMetricsHistory(minutes: number) {
  return useQuery({
    queryKey: ['poolMetricsHistory', minutes],
    queryFn: () => api.getPoolMetricsHistory(minutes),
    refetchInterval: 60_000,
  });
}
