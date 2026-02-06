'use client';

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';

// ── Queries ─────────────────────────────────────────────────────────────────
// No refetchInterval — all updates are pushed via WebSocket (see useEventStream).
// History queries use a short staleTime so they refresh on window focus.

export function usePriceHistory(windowMinutes = 60) {
  const fromTs = Date.now() - windowMinutes * 60 * 1000;
  // Enough points for a smooth chart: ~2 per minute per venue × 5 venues
  const limit = Math.min(windowMinutes * 10, 1000);
  return useQuery({
    queryKey: ['priceHistory', windowMinutes],
    queryFn: () => api.getPriceHistory({ from_ts: fromTs, limit }),
    // Re-fetch every 30s so the chart accumulates new points
    refetchInterval: 30_000,
  });
}

export function useStatus() {
  return useQuery({ queryKey: ['status'], queryFn: api.getStatus });
}

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: api.getHealth });
}

export function usePrices() {
  return useQuery({ queryKey: ['prices'], queryFn: api.getPrices });
}

export function useBlendedPrice() {
  return useQuery({ queryKey: ['blendedPrice'], queryFn: api.getBlendedPrice });
}

export function useNormalizedPrices() {
  return useQuery({ queryKey: ['normalizedPrices'], queryFn: api.getNormalizedPrices });
}

export function useGlobalPosition() {
  return useQuery({ queryKey: ['globalPosition'], queryFn: api.getGlobalPosition });
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

export function useAccountBalances() {
  return useQuery({ queryKey: ['accountBalances'], queryFn: api.getAccountBalances });
}

export function useAlerts(limit = 20) {
  return useQuery({
    queryKey: ['alerts', limit],
    queryFn: () => api.getAlerts(limit),
  });
}

// ── Mutations ───────────────────────────────────────────────────────────────

export function usePauseTrading() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => api.pauseTrading(token),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status'] });
      qc.invalidateQueries({ queryKey: ['health'] });
    },
  });
}

export function useResumeTrading() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => api.resumeTrading(token),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status'] });
      qc.invalidateQueries({ queryKey: ['health'] });
    },
  });
}

export function useTriggerScan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => api.triggerScan(token),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['opportunities'] });
      qc.invalidateQueries({ queryKey: ['arbitrageStatus'] });
    },
  });
}

export function useAcknowledgeAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, token }: { id: number; token: string }) =>
      api.acknowledgeAlert(id, token),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alerts'] });
    },
  });
}
