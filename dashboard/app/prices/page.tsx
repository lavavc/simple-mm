'use client';

import { useMemo, useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  formatNumber,
  normalizeToNgnUsd,
  spreadBps,
  isDex,
  VENUE_LABELS,
  VENUE_COLORS,
  sourceToVenue,
  normalizeSnapshotMid,
} from '@/lib/utils';
import { usePrices, useBlendedPrice, usePriceHistory } from '@/lib/hooks/useQueries';
import { RefreshCw, TrendingUp, AlertCircle, Circle, Activity } from 'lucide-react';
import { VenuePriceChart } from '@/components/charts/VenuePriceChart';
import {
  AreaChart,
  Area,
  ResponsiveContainer,
  Tooltip,
} from 'recharts';
import type { VenuePriceResponse, PriceSnapshot } from '@/types';

// ── Per-venue sparkline ─────────────────────────────────────────────────────

function buildSparkline(
  snapshots: PriceSnapshot[],
  venue: string,
): { time: string; value: number }[] {
  const bucketMs = 30_000; // 30s buckets
  const buckets = new Map<number, number>();

  for (const snap of snapshots) {
    if (sourceToVenue(snap.source) !== venue) continue;
    const mid = normalizeSnapshotMid(snap.source, Number(snap.mid));
    if (mid === null) continue;
    const bucket = Math.floor(snap.timestamp / bucketMs) * bucketMs;
    if (!buckets.has(bucket)) buckets.set(bucket, mid);
  }

  return Array.from(buckets.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([ts, value]) => ({
      time: new Date(ts).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }),
      value,
    }));
}

// ── Inline price card (per-venue) ──────────────────────────────────────────

function PriceCard({
  price,
  sparklineData,
}: {
  price: VenuePriceResponse;
  sparklineData: { time: string; value: number }[];
}) {
  const label = VENUE_LABELS[price.venue] || { name: price.venue, chain: 'Unknown', type: '?' };
  const hasPrice = !!price.quote;
  const normalized = normalizeToNgnUsd(price);
  const spread = normalized && !isDex(price.venue) ? spreadBps(normalized) : null;
  const color = VENUE_COLORS[price.venue] || '#10B981';

  // Absolute timestamp when this venue's price was produced.
  // Anchored per card using its own age_seconds — so each card is independent.
  const priceTs = useRef(Date.now() - Math.round(price.age_seconds) * 1000);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    priceTs.current = Date.now() - Math.round(price.age_seconds) * 1000;
  }, [price.age_seconds]);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const totalAge = Math.round((now - priceTs.current) / 1000);
  const ageLabel = (() => {
    if (totalAge < 5) return '● LIVE';
    if (totalAge < 60) return `${totalAge}s ago`;
    const m = Math.floor(totalAge / 60);
    const s = totalAge % 60;
    return `${m}m ${s}s ago`;
  })();

  const sparkMin = sparklineData.length
    ? Math.min(...sparklineData.map((d) => d.value))
    : 0;
  const sparkMax = sparklineData.length
    ? Math.max(...sparklineData.map((d) => d.value))
    : 0;
  const sparkDelta = sparklineData.length >= 2
    ? sparklineData[sparklineData.length - 1].value - sparklineData[0].value
    : 0;

  return (
    <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05]">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="text-[10px] font-mono tracking-widest uppercase text-white/80">{label.name}</CardTitle>
          <Badge variant="outline" className="text-[8px] bg-white/[0.05] border-white/10 text-white/60 font-mono">{label.type}</Badge>
        </div>
        <Circle
          className={`h-2 w-2 ${hasPrice ? 'fill-emerald-500 text-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]' : 'fill-yellow-500 text-yellow-500 shadow-[0_0_8px_rgba(234,179,8,0.8)]'}`}
        />
      </CardHeader>
      <CardContent>
        {normalized ? (
          <div>
            <div className="flex items-center gap-2 mb-2">
              <TrendingUp className="h-4 w-4 text-emerald-500/50" />
              <span className="text-xl font-bold font-mono tracking-tight text-white">{formatNumber(normalized.mid, 2)}</span>
              <span className="text-[10px] text-white/40 uppercase tracking-widest font-mono">NGN/USD</span>
            </div>
            {spread !== null && (
              <div className="mt-1 mb-3">
                <Badge variant="outline" className="text-[9px] bg-emerald-500/10 border-emerald-500/30 text-emerald-400 font-mono tracking-wider">{spread} BPS SPREAD</Badge>
              </div>
            )}

            {/* Sparkline */}
            {sparklineData.length > 1 && (
              <div className="mt-3 mb-1">
                <div className="flex items-center justify-between text-[8px] font-mono text-white/30 uppercase tracking-widest mb-1">
                  <span>15M PRICE TRAIL</span>
                  <span className={sparkDelta >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                    {sparkDelta >= 0 ? '+' : ''}{formatNumber(sparkDelta, 1)}
                  </span>
                </div>
                <div className="h-[52px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={sparklineData} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id={`spark-${price.venue}`} x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor={color} stopOpacity={0.25} />
                          <stop offset="95%" stopColor={color} stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <Tooltip
                        contentStyle={{
                          backgroundColor: '#0B0E14',
                          border: '1px solid rgba(255,255,255,0.1)',
                          borderRadius: '2px',
                          fontFamily: 'ui-monospace, monospace',
                          fontSize: '9px',
                          textTransform: 'uppercase',
                          letterSpacing: '0.05em',
                          padding: '4px 8px',
                        }}
                        itemStyle={{ color: 'rgba(255,255,255,0.8)' }}
                        labelStyle={{ color: 'rgba(255,255,255,0.3)', marginBottom: '2px' }}
                        formatter={(v: number) => [formatNumber(v, 2), 'NGN/USD']}
                      />
                      <Area
                        type="stepAfter"
                        dataKey="value"
                        stroke={color}
                        strokeWidth={1.5}
                        fill={`url(#spark-${price.venue})`}
                        dot={false}
                        activeDot={{ r: 3, fill: '#0B0E14', stroke: color, strokeWidth: 1.5 }}
                        connectNulls
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
                <div className="flex justify-between text-[8px] font-mono text-white/20 mt-0.5">
                  <span>{formatNumber(sparkMin, 1)}</span>
                  <span>{formatNumber(sparkMax, 1)}</span>
                </div>
              </div>
            )}

            <div className="grid grid-cols-2 gap-2 mt-3 text-[10px] font-mono border-t border-white/[0.05] pt-3">
              <div>
                <div className="text-white/30 uppercase tracking-widest mb-1 text-[8px]">BID</div>
                <div className="text-emerald-400">{formatNumber(normalized.bid, 2)}</div>
              </div>
              <div>
                <div className="text-white/30 uppercase tracking-widest mb-1 text-[8px]">ASK</div>
                <div className="text-red-400">{formatNumber(normalized.ask, 2)}</div>
              </div>
            </div>
            <p className="text-[8px] text-white/30 tracking-widest font-mono mt-4 text-right tabular-nums">
              {ageLabel}
            </p>
          </div>
        ) : hasPrice && price.pair === 'cNGN/NGN' ? (
          <div>
            <span className="text-xl font-bold font-mono text-white">{formatNumber(price.quote!.mid, 4)}</span>
            <span className="text-[10px] text-white/40 uppercase tracking-widest font-mono ml-2">cNGN/NGN</span>
            <p className="text-[9px] text-white/50 font-mono mt-2 bg-white/5 p-2 rounded-sm border border-white/10">Fixed Peg Rate</p>
          </div>
        ) : price.error ? (
          <div className="flex items-center gap-2 text-[10px] font-mono text-yellow-500/80 bg-yellow-500/10 p-2 rounded-sm border border-yellow-500/20">
            <AlertCircle className="h-3 w-3" />
            <span>{price.error}</span>
          </div>
        ) : (
          <div className="text-[10px] font-mono text-white/30 uppercase tracking-widest italic py-4 text-center border border-dashed border-white/10 rounded-sm">Offline</div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function PricesPage() {
  const { data: prices, isLoading } = usePrices();
  const { data: blended } = useBlendedPrice();
  const { data: snapshots } = usePriceHistory(15);

  const normalizedPrices = prices
    ?.map((p) => ({ venue: p.venue, normalized: normalizeToNgnUsd(p) }))
    .filter((p) => p.normalized !== null) ?? [];

  const mids = normalizedPrices.map((p) => p.normalized!.mid);
  const crossVenueSpreadBps =
    mids.length >= 2
      ? Math.round(((Math.max(...mids) - Math.min(...mids)) / Math.min(...mids)) * 10000)
      : null;

  const vwapNgn = blended && blended.vwap > 0 ? 1 / blended.vwap : null;

  // Build per-venue sparklines from the shared history fetch
  const sparklines = useMemo(() => {
    const all = snapshots ?? [];
    const venues = prices?.map((p) => p.venue) ?? [];
    return Object.fromEntries(venues.map((v) => [v, buildSparkline(all, v)]));
  }, [snapshots, prices]);

  return (
    <div className="flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 p-2 md:p-4 animate-in fade-in duration-500 font-sans space-y-6">

      {/* Top Status Bar */}
      <div className="flex items-center justify-between border-b border-white/[0.05] pb-3">
        <div className="flex items-center gap-3">
          <Activity className="h-4 w-4 text-emerald-500" />
          <h1 className="text-xs font-bold tracking-widest uppercase text-white">Price Feed</h1>
        </div>
        <div className="flex items-center gap-3">
          {isLoading ? (
            <div className="flex items-center gap-2 bg-yellow-500/10 border border-yellow-500/20 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-yellow-500/80">
              <div className="h-2 w-2 border-t-2 border-yellow-500 rounded-full animate-spin" />
              <span>Syncing......</span>
            </div>
          ) : crossVenueSpreadBps !== null ? (
            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono ${crossVenueSpreadBps > 150 ? 'bg-yellow-500/10 border border-yellow-500/20 text-yellow-500/80' : 'bg-emerald-500/10 border border-emerald-500/20 text-emerald-500/80'}`}>
              <div className={`h-2 w-2 rounded-full ${crossVenueSpreadBps > 150 ? 'bg-yellow-500 animate-pulse' : 'bg-emerald-500 animate-ping'}`} />
              CROSS-VENUE SPREAD: {crossVenueSpreadBps} BPS
            </div>
          ) : (
            <div className="flex items-center gap-3 bg-white/[0.02] border border-white/5 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-white/60">
              <span>Scanner:</span>
              <span className="flex h-2 w-2 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span className="text-emerald-400">Zero-Latency Socket Active</span>
            </div>
          )}
        </div>
      </div>

      {/* Blended price summary */}
      {(isLoading || (blended && blended.vwap > 0)) && (
        <Card className="bg-emerald-500/5 border-l-2 border-l-emerald-500/50 border-r-0 border-t-0 border-b-0 shadow-inner rounded-sm">
          <CardContent className="py-4">
            <div className="flex flex-col md:flex-row items-center justify-between gap-4">
              <div className="flex items-center gap-4">
                <div className="flex items-center justify-center h-10 w-10 bg-emerald-500/10 rounded-full border border-emerald-500/20 shadow-[0_0_15px_rgba(16,185,129,0.2)]">
                  <Activity className="h-5 w-5 text-emerald-400" />
                </div>
                <div>
                  <div className="text-[10px] text-emerald-400/50 font-mono tracking-widest uppercase mb-1">AGGREGATED VWAP</div>
                  <div className="text-2xl font-bold font-mono text-white tracking-tight">
                    {isLoading || !blended ? (
                      <div className="flex items-baseline gap-2">
                        <div className="h-7 w-24 bg-emerald-500/20 rounded-sm animate-pulse" />
                        <span className="text-[12px] font-normal text-white/40 tracking-widest">NGN/USD</span>
                      </div>
                    ) : (
                      <>
                        {formatNumber(1 / blended.vwap, 2)}{' '}
                        <span className="text-[12px] font-normal text-white/40 tracking-widest">NGN/USD</span>
                      </>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-6 text-[10px] font-mono">
                <div className="flex flex-col items-end">
                  <span className="text-white/30 tracking-widest uppercase mb-1">TWAP 5M</span>
                  <span className="text-white/80">{isLoading || !blended ? <div className="h-3 w-12 bg-white/10 rounded-sm animate-pulse" /> : (blended.twap_5m > 0 ? formatNumber(1 / blended.twap_5m, 2) : '—')}</span>
                </div>
                <div className="flex flex-col items-end">
                  <span className="text-white/30 tracking-widest uppercase mb-1">TWAP 1H</span>
                  <span className="text-white/80">{isLoading || !blended ? <div className="h-3 w-12 bg-white/10 rounded-sm animate-pulse" /> : (blended.twap_1h > 0 ? formatNumber(1 / blended.twap_1h, 2) : '—')}</span>
                </div>
                <div className="flex flex-col items-end bg-emerald-500/10 px-3 py-1.5 rounded-sm border border-emerald-500/20">
                  <span className="text-emerald-400/50 tracking-widest uppercase mb-1">{isLoading || !blended ? <div className="h-3 w-16 bg-emerald-500/20 rounded-sm animate-pulse mb-0.5" /> : `${blended.num_sources} SOURCES`}</span>
                  <span className="text-emerald-400 font-bold tracking-wider">{isLoading || !blended ? <div className="h-3 w-10 bg-emerald-400/20 rounded-sm animate-pulse mt-0.5" /> : `${Math.round(blended.confidence * 100)}% CONF`}</span>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Per-venue price cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        {isLoading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <Card key={i} className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05]">
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <div className="flex items-center gap-2">
                  <div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse" />
                  <div className="h-3 w-8 bg-white/5 rounded-sm animate-pulse" />
                </div>
                <div className="h-2 w-2 rounded-full bg-white/10 animate-pulse" />
              </CardHeader>
              <CardContent>
                <div>
                  <div className="flex items-center gap-2 mb-2">
                    <Activity className="h-4 w-4 text-emerald-500/20" />
                    <div className="h-6 w-24 bg-white/10 rounded-sm animate-pulse" />
                    <div className="h-3 w-10 bg-white/5 rounded-sm animate-pulse ml-1" />
                  </div>
                  <div className="mt-1 mb-3">
                    <div className="h-4 w-20 bg-emerald-500/10 rounded-sm animate-pulse" />
                  </div>
                  <div className="grid grid-cols-2 gap-2 mt-3 border-t border-white/[0.05] pt-3">
                    <div>
                      <div className="h-2 w-6 bg-white/5 rounded-sm mb-2" />
                      <div className="h-3 w-12 bg-emerald-400/20 rounded-sm animate-pulse" />
                    </div>
                    <div>
                      <div className="h-2 w-6 bg-white/5 rounded-sm mb-2" />
                      <div className="h-3 w-12 bg-red-400/20 rounded-sm animate-pulse" />
                    </div>
                  </div>
                  <div className="flex justify-end mt-4">
                    <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                  </div>
                </div>
              </CardContent>
            </Card>
          ))
        ) : (
          prices?.map((price) => (
            <PriceCard
              key={price.venue}
              price={price}
              sparklineData={sparklines[price.venue] ?? []}
            />
          ))
        )}
      </div>

      {/* Multi-venue price comparison chart */}
      <VenuePriceChart blended={blended} />

      {/* Comparison table */}
      <Card className="bg-white/[0.02] border-white/[0.05]">
        <CardHeader className="border-b border-white/[0.05] pb-3">
          <CardTitle className="text-[10px] font-mono font-bold tracking-widest uppercase text-white/50">Price Matrix</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="border-b border-white/[0.05] text-white/30 tracking-widest uppercase">
                  <th className="text-left py-3 font-medium">Venue</th>
                  <th className="text-right py-3 font-medium">NGN/USD</th>
                  <th className="text-right py-3 font-medium">Bid</th>
                  <th className="text-right py-3 font-medium">Ask</th>
                  <th className="text-right py-3 font-medium">Spread</th>
                  <th className="text-right py-3 font-medium">vs VWAP</th>
                  <th className="text-right py-3 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="text-white/80">
                {isLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <tr key={i} className="border-b border-white/[0.02] last:border-0 hover:bg-white/[0.02] transition-colors">
                      <td className="py-4 items-center flex gap-2">
                        <div className="h-3 w-20 bg-white/10 rounded-sm animate-pulse" />
                      </td>
                      <td className="text-right py-4">
                        <div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse ml-auto" />
                      </td>
                      <td className="text-right py-4">
                        <div className="h-3 w-16 bg-emerald-400/20 rounded-sm animate-pulse ml-auto" />
                      </td>
                      <td className="text-right py-4">
                        <div className="h-3 w-16 bg-red-400/20 rounded-sm animate-pulse ml-auto" />
                      </td>
                      <td className="text-right py-4">
                        <div className="h-3 w-12 bg-white/10 rounded-sm animate-pulse ml-auto" />
                      </td>
                      <td className="text-right py-4">
                        <div className="h-3 w-12 bg-white/5 rounded-sm animate-pulse ml-auto" />
                      </td>
                      <td className="text-right py-4">
                        <div className="h-5 w-24 bg-white/10 rounded-sm animate-pulse ml-auto" />
                      </td>
                    </tr>
                  ))
                ) : (
                  normalizedPrices.map((p) => {
                    const label = VENUE_LABELS[p.venue] || { name: p.venue };
                    const minMid = Math.min(...mids);
                    const diffFromMin = Math.round(((p.normalized!.mid - minMid) / minMid) * 10000);
                    const spread = isDex(p.venue) ? null : spreadBps(p.normalized!);
                    const diffFromVwap = vwapNgn
                      ? Math.round(((p.normalized!.mid - vwapNgn) / vwapNgn) * 10000)
                      : null;

                    return (
                      <tr key={p.venue} className="border-b border-white/[0.02] last:border-0 hover:bg-white/[0.02] transition-colors">
                        <td className="py-3 items-center flex gap-2">
                          {label.name}
                          {isDex(p.venue) && <span className="bg-white/10 text-white/50 px-1.5 py-0.5 rounded-sm text-[8px]">DEX</span>}
                        </td>
                        <td className="text-right font-medium text-white">{formatNumber(p.normalized!.mid, 2)}</td>
                        <td className="text-right text-emerald-400">{formatNumber(p.normalized!.bid, 2)}</td>
                        <td className="text-right text-red-400">{formatNumber(p.normalized!.ask, 2)}</td>
                        <td className="text-right text-white/50">{spread !== null ? `${spread} BPS` : '—'}</td>
                        <td className="text-right">
                          {diffFromVwap !== null ? (
                            <span className={`${diffFromVwap > 0 ? 'text-red-400/80' : diffFromVwap < 0 ? 'text-emerald-400/80' : 'text-white/50'}`}>
                              {diffFromVwap >= 0 ? '+' : ''}{diffFromVwap}
                            </span>
                          ) : (
                            <span className="text-white/30">—</span>
                          )}
                        </td>
                        <td className="text-right">
                          {diffFromMin === 0 ? (
                            <span className="text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-2 py-1 rounded-sm text-[8px] uppercase tracking-wider">Cheapest Floor</span>
                          ) : (
                            <span className={`text-white/60 bg-white/5 border border-white/10 px-2 py-1 rounded-sm text-[8px] uppercase tracking-wider ${diffFromMin > 100 ? 'text-yellow-500/80 bg-yellow-500/10 border-yellow-500/20' : ''}`}>
                              +{diffFromMin} BPS Premium
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
          <p className="text-[9px] text-white/20 mt-4 font-mono tracking-wide">
            &gt; All prices normalized to NGN per 1 USD. Lower baseline indicates cheaper USD acquisition floor.<br />
            &gt; Automated arbitrage vectors activate when cross-venue spreads exceed combined liquidity fragmentation constraints (~110-150 BPS).
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
