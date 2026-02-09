'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  formatNumber,
  normalizeToNgnUsd,
  spreadBps,
  isDex,
  VENUE_LABELS,
} from '@/lib/utils';
import { usePrices, useBlendedPrice } from '@/lib/hooks/useQueries';
import { RefreshCw, TrendingUp, AlertCircle, Circle, Activity } from 'lucide-react';
import { VenuePriceChart } from '@/components/charts/VenuePriceChart';
import type { VenuePriceResponse } from '@/types';

// ── Inline price card (per-venue) ──────────────────────────────────────────

function PriceCard({ price }: { price: VenuePriceResponse }) {
  const label = VENUE_LABELS[price.venue] || { name: price.venue, chain: 'Unknown', type: '?' };
  const hasPrice = !!price.quote;
  const normalized = normalizeToNgnUsd(price);
  // Don't show spread for DEX venues (AMM pools don't have order book spreads)
  const spread = normalized && !isDex(price.venue) ? spreadBps(normalized) : null;

  return (
    <Card className="hover:border-primary/50 transition-colors">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="text-sm font-medium">{label.name}</CardTitle>
          <Badge variant="outline" className="text-xs">{label.type}</Badge>
        </div>
        <Circle
          className={`h-3 w-3 ${hasPrice ? 'fill-green-500 text-green-500' : 'fill-yellow-500 text-yellow-500'}`}
        />
      </CardHeader>
      <CardContent>
        {normalized ? (
          <div>
            <div className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-muted-foreground" />
              <span className="text-2xl font-bold">{formatNumber(normalized.mid, 2)}</span>
              <span className="text-sm text-muted-foreground">NGN/USD</span>
            </div>
            {spread !== null && (
              <div className="mt-1">
                <Badge variant="outline" className="text-xs">{spread} bps spread</Badge>
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 mt-3 text-sm">
              <div>
                <span className="text-muted-foreground">Bid: </span>
                <span className="text-green-500">{formatNumber(normalized.bid, 2)}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Ask: </span>
                <span className="text-red-500">{formatNumber(normalized.ask, 2)}</span>
              </div>
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              Updated {Math.round(price.age_seconds)}s ago
            </p>
          </div>
        ) : hasPrice && price.pair === 'cNGN/NGN' ? (
          <div>
            <span className="text-2xl font-bold">{formatNumber(price.quote!.mid, 4)}</span>
            <span className="text-sm text-muted-foreground ml-2">cNGN/NGN</span>
            <p className="text-xs text-muted-foreground mt-2">Peg rate (not USD convertible)</p>
          </div>
        ) : price.error ? (
          <div className="flex items-center gap-2 text-sm text-yellow-500">
            <AlertCircle className="h-4 w-4" />
            <span>{price.error}</span>
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">No price data</div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function PricesPage() {
  const { data: prices, isLoading } = usePrices();
  const { data: blended } = useBlendedPrice();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const normalizedPrices = prices
    ?.map((p) => ({ venue: p.venue, normalized: normalizeToNgnUsd(p) }))
    .filter((p) => p.normalized !== null) ?? [];

  const mids = normalizedPrices.map((p) => p.normalized!.mid);
  const crossVenueSpreadBps =
    mids.length >= 2
      ? Math.round(((Math.max(...mids) - Math.min(...mids)) / Math.min(...mids)) * 10000)
      : null;

  const vwapNgn = blended && blended.vwap > 0 ? 1 / blended.vwap : null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Price Feed</h1>
        {crossVenueSpreadBps !== null && (
          <Badge
            variant={crossVenueSpreadBps > 150 ? 'warning' : crossVenueSpreadBps > 100 ? 'outline' : 'success'}
            className="text-sm"
          >
            Cross-venue spread: {crossVenueSpreadBps} bps
          </Badge>
        )}
      </div>

      {/* Blended price summary */}
      {blended && blended.vwap > 0 && (
        <Card>
          <CardContent className="py-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Activity className="h-5 w-5 text-blue-500" />
                <div>
                  <div className="text-sm text-muted-foreground">Blended VWAP</div>
                  <div className="text-xl font-bold">
                    {formatNumber(1 / blended.vwap, 2)}{' '}
                    <span className="text-sm font-normal text-muted-foreground">NGN/USD</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-4 text-sm">
                <div>
                  <span className="text-muted-foreground">TWAP 5m: </span>
                  <span>{blended.twap_5m > 0 ? formatNumber(1 / blended.twap_5m, 2) : '—'}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">TWAP 1h: </span>
                  <span>{blended.twap_1h > 0 ? formatNumber(1 / blended.twap_1h, 2) : '—'}</span>
                </div>
                <Badge variant="outline" className="text-xs">
                  {Math.round(blended.confidence * 100)}% confidence · {blended.num_sources} sources
                </Badge>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Per-venue price cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {prices?.map((price) => <PriceCard key={price.venue} price={price} />)}
      </div>

      {/* Multi-venue price comparison chart */}
      <VenuePriceChart blended={blended} />

      {/* Comparison table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Price Comparison Table</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2 font-medium">Venue</th>
                  <th className="text-right py-2 font-medium">NGN/USD</th>
                  <th className="text-right py-2 font-medium">Bid</th>
                  <th className="text-right py-2 font-medium">Ask</th>
                  <th className="text-right py-2 font-medium">Spread</th>
                  <th className="text-right py-2 font-medium">vs VWAP</th>
                  <th className="text-right py-2 font-medium">vs Cheapest</th>
                </tr>
              </thead>
              <tbody>
                {normalizedPrices.map((p) => {
                  const label = VENUE_LABELS[p.venue] || { name: p.venue };
                  const minMid = Math.min(...mids);
                  const diffFromMin = Math.round(((p.normalized!.mid - minMid) / minMid) * 10000);
                  // Don't show spread for DEX venues (AMM pools don't have order book spreads)
                  const spread = isDex(p.venue) ? null : spreadBps(p.normalized!);
                  const diffFromVwap = vwapNgn
                    ? Math.round(((p.normalized!.mid - vwapNgn) / vwapNgn) * 10000)
                    : null;

                  return (
                    <tr key={p.venue} className="border-b last:border-0">
                      <td className="py-2">{label.name}</td>
                      <td className="text-right font-medium">{formatNumber(p.normalized!.mid, 2)}</td>
                      <td className="text-right text-green-500">{formatNumber(p.normalized!.bid, 2)}</td>
                      <td className="text-right text-red-500">{formatNumber(p.normalized!.ask, 2)}</td>
                      <td className="text-right">{spread !== null ? `${spread} bps` : '—'}</td>
                      <td className="text-right">
                        {diffFromVwap !== null ? (
                          <span className={diffFromVwap > 0 ? 'text-red-400' : diffFromVwap < 0 ? 'text-green-400' : ''}>
                            {diffFromVwap >= 0 ? '+' : ''}{diffFromVwap} bps
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="text-right">
                        <Badge
                          variant={diffFromMin === 0 ? 'success' : diffFromMin > 100 ? 'warning' : 'outline'}
                          className="text-xs"
                        >
                          {diffFromMin === 0 ? 'Cheapest' : `+${diffFromMin} bps`}
                        </Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted-foreground mt-4">
            All prices normalized to NGN per 1 USD. Lower = cheaper to buy USD.
            Arbitrage opportunities exist when cross-venue spread exceeds fees (~100-150 bps).
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
