'use client';

import { useEffect, useRef, useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000/ws';
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

/**
 * Maps engine event types to the React Query cache keys they should invalidate.
 * When a WebSocket event arrives, every listed key is refetched.
 */
const EVENT_TO_KEYS: Record<string, string[][]> = {
  venue_prices: [['prices'], ['blendedPrice'], ['normalizedPrices'], ['priceHistory'], ['status']],
  positions: [['status']],
  portfolio_delta: [['globalPosition']],
  alert: [['alerts']],
  refill_alert: [['alerts']],
  system: [['status'], ['health']],
  account_balances: [['accountBalances']],
  arbitrage_opportunity: [['opportunities'], ['arbitrageStatus']],
  arbitrage_completed: [['opportunities'], ['arbitrageStatus']],
  action: [], // logged only — no cache to invalidate
};

/**
 * Connects to the engine's WebSocket and keeps React Query caches fresh.
 *
 * Call once near the app root (e.g. in Providers). When an event arrives
 * the hook invalidates the matching query keys so components re-render
 * with live data without any polling interval.
 */
export function useEventStream() {
  const qc = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      retryRef.current = 0;
    };

    ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        const keys = EVENT_TO_KEYS[event.type];
        if (keys) {
          for (const key of keys) {
            qc.invalidateQueries({ queryKey: key });
          }
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (!mountedRef.current) return;
      // Exponential backoff with jitter
      const delay = Math.min(
        RECONNECT_BASE_MS * 2 ** retryRef.current + Math.random() * 500,
        RECONNECT_MAX_MS,
      );
      retryRef.current++;
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [qc]);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
    };
  }, [connect]);
}
