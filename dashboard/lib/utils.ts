import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import type { VenuePriceResponse } from '@/types';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ── Formatting ──────────────────────────────────────────────────────────────

export function formatNumber(value: number, decimals = 2): string {
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

export function formatCurrency(value: number, currency = 'USD'): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(value);
}

export function formatBps(bps: number): string {
  return `${bps >= 0 ? '+' : ''}${bps} bps`;
}

export function formatTimestamp(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function formatRelativeTime(timestamp: number): string {
  const seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function formatUptime(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

export function formatAddress(address: string, chars = 6): string {
  if (address.length <= chars * 2 + 2) return address;
  return `${address.slice(0, chars + 2)}...${address.slice(-chars)}`;
}

export function getSeverityColor(severity: 'info' | 'warning' | 'critical'): string {
  switch (severity) {
    case 'critical':
      return 'text-red-500 bg-red-500/10';
    case 'warning':
      return 'text-yellow-500 bg-yellow-500/10';
    case 'info':
      return 'text-blue-500 bg-blue-500/10';
  }
}

// ── Venue metadata ──────────────────────────────────────────────────────────

export interface VenueLabel {
  name: string;
  chain: string;
  type: string;
}

export const VENUE_LABELS: Record<string, VenueLabel> = {
  aerodrome: { name: 'Aerodrome', chain: 'Base', type: 'DEX' },
  quidax: { name: 'Quidax', chain: 'CEX', type: 'CEX' },
  blockradar: { name: 'Blockradar', chain: 'Base', type: 'B2C' },
  pancakeswap: { name: 'PancakeSwap', chain: 'BSC', type: 'DEX' },
  bybit: { name: 'Bybit P2P', chain: 'P2P', type: 'P2P' },
};

export const VENUE_COLORS: Record<string, string> = {
  bybit: '#F7931A',
  quidax: '#2E7D32',
  aerodrome: '#1976D2',
  pancakeswap: '#7B1FA2',
  blockradar: '#455A64',
};

// ── Source → venue mapping ──────────────────────────────────────────────────
// price_snapshots.source uses raw names; map to canonical venue names + pairs

interface SourceInfo {
  venue: string;
  pair: string;
}

const SOURCE_MAP: Record<string, SourceInfo> = {
  bybit_p2p: { venue: 'bybit', pair: 'USDT/NGN' },
  quidax: { venue: 'quidax', pair: 'cNGN/USDT' },
  aerodrome_pool: { venue: 'aerodrome', pair: 'cNGN/USDC' },
  pancakeswap_pool: { venue: 'pancakeswap', pair: 'cNGN/USDT' },
  blockradar: { venue: 'blockradar', pair: 'cNGN/USDC' },
};

/** Map a price_snapshots source name to a venue name. */
export function sourceToVenue(source: string): string {
  return SOURCE_MAP[source]?.venue ?? source;
}

/** Map a price_snapshots source name to its trading pair. */
export function sourceToPair(source: string): string {
  return SOURCE_MAP[source]?.pair ?? '';
}

/** Normalize a raw snapshot mid price to NGN/USD using its source pair. */
export function normalizeSnapshotMid(source: string, mid: number): number | null {
  const pair = SOURCE_MAP[source]?.pair;
  if (!pair || !mid || !isFinite(mid)) return null;

  if (pair === 'USDT/NGN') return mid;
  if (pair === 'cNGN/USDC' || pair === 'cNGN/USDT') {
    const val = 1 / mid;
    return isFinite(val) ? val : null;
  }
  return null; // cNGN/NGN not USD-convertible
}

// ── Price normalization ─────────────────────────────────────────────────────

/** Normalize any venue price to NGN/USD (how many NGN per 1 USD).
 *  Returns null if the price can't be normalized or values are zero/invalid.
 *  Note: API returns Decimal values as strings, so we must coerce to Number. */
export function normalizeToNgnUsd(
  price: VenuePriceResponse,
): { bid: number; ask: number; mid: number } | null {
  if (!price.quote) return null;
  const bid = Number(price.quote.bid);
  const ask = Number(price.quote.ask);
  const mid = Number(price.quote.mid);

  if (!isFinite(mid) || mid <= 0) return null;

  // USDT/NGN: already in NGN per USD format
  if (price.pair === 'USDT/NGN') {
    return { bid, ask, mid };
  }

  // cNGN/USDC or cNGN/USDT: invert (0.0007 USD/cNGN → ~1430 NGN/USD)
  if (price.pair === 'cNGN/USDC' || price.pair === 'cNGN/USDT') {
    if (!bid || !ask) return null;
    const result = { bid: 1 / ask, ask: 1 / bid, mid: 1 / mid };
    return isFinite(result.mid) ? result : null;
  }

  // cNGN/NGN: peg rate, not convertible to USD
  return null;
}

/** Compute spread in basis points from normalized bid/ask/mid. */
export function spreadBps(n: { bid: number; ask: number; mid: number }): number {
  return Math.round(((n.ask - n.bid) / n.mid) * 10000);
}

/** Check if a venue is a DEX (AMM pools don't have order book spreads). */
export function isDex(venueName: string): boolean {
  return VENUE_LABELS[venueName]?.type === 'DEX';
}
