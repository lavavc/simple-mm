'use client';

import { useMemo, useState } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
  CartesianGrid,
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  formatNumber,
  normalizeSnapshotMid,
  sourceToVenue,
  VENUE_LABELS,
  VENUE_COLORS,
} from '@/lib/utils';
import { usePriceHistory } from '@/lib/hooks/useQueries';
import type { BlendedPriceResponse, PriceSnapshot } from '@/types';

const TIME_WINDOWS = [
  { label: '5m', minutes: 5 },
  { label: '15m', minutes: 15 },
  { label: '1h', minutes: 60 },
  { label: '6h', minutes: 360 },
  { label: '24h', minutes: 1440 },
] as const;

interface VenuePriceChartProps {
  blended?: BlendedPriceResponse;
}

/** Group snapshots by time bucket and venue, normalized to NGN/USD. */
function buildChartData(
  snapshots: PriceSnapshot[],
  windowMinutes: number,
): { data: Record<string, number | string>[]; venues: string[] } {
  if (!snapshots.length) return { data: [], venues: [] };

  // Choose bucket size based on window
  const bucketMs =
    windowMinutes <= 5 ? 30_000 :       // 30s buckets
      windowMinutes <= 15 ? 60_000 :      // 1m buckets
        windowMinutes <= 60 ? 120_000 :     // 2m buckets
          windowMinutes <= 360 ? 600_000 :    // 10m buckets
            1_800_000;                           // 30m buckets

  const venueSet = new Set<string>();

  // Bucket: timestamp → venue → mid value
  const buckets = new Map<number, Record<string, number>>();

  for (const snap of snapshots) {
    const venue = sourceToVenue(snap.source);
    const mid = normalizeSnapshotMid(snap.source, Number(snap.mid));
    if (mid === null) continue;

    venueSet.add(venue);
    const bucket = Math.floor(snap.timestamp / bucketMs) * bucketMs;
    if (!buckets.has(bucket)) buckets.set(bucket, {});
    // Last-write-wins within a bucket (snapshots are DESC, so earlier writes win — reverse first)
    const b = buckets.get(bucket)!;
    if (!(venue in b)) b[venue] = mid;
  }

  // Sort by time ascending
  const sorted = Array.from(buckets.entries()).sort((a, b) => a[0] - b[0]);

  // Format for display
  const fmt = windowMinutes <= 60
    ? { hour: '2-digit' as const, minute: '2-digit' as const }
    : { hour: '2-digit' as const, minute: '2-digit' as const };

  const data = sorted.map(([ts, values]) => ({
    time: new Date(ts).toLocaleTimeString('en-US', fmt),
    ...values,
  }));

  return { data, venues: Array.from(venueSet) };
}

export function VenuePriceChart({ blended }: VenuePriceChartProps) {
  const [windowMinutes, setWindowMinutes] = useState(60);
  const { data: snapshots } = usePriceHistory(windowMinutes);

  const { data: chartData, venues: activeVenues } = useMemo(
    () => buildChartData(snapshots ?? [], windowMinutes),
    [snapshots, windowMinutes],
  );

  // Y-axis range
  const priceRange = useMemo(() => {
    const all = chartData.flatMap((pt) =>
      activeVenues.map((v) => pt[v] as number).filter((n) => n && isFinite(n)),
    );
    if (all.length === 0) return { min: 1400, max: 1500 };
    const min = Math.min(...all);
    const max = Math.max(...all);
    const pad = (max - min) * 0.15 || 10;
    return { min: Math.floor(min - pad), max: Math.ceil(max + pad) };
  }, [chartData, activeVenues]);

  // VWAP reference line
  const vwapNgn = useMemo(
    () => (blended && blended.vwap > 0 ? 1 / blended.vwap : null),
    [blended],
  );

  // Cross-venue spread from latest point
  const spread = useMemo(() => {
    if (chartData.length === 0) return null;
    const last = chartData[chartData.length - 1];
    const vals = activeVenues.map((v) => last[v] as number).filter((n) => n && isFinite(n));
    if (vals.length < 2) return null;
    const min = Math.min(...vals);
    return Math.round(((Math.max(...vals) - min) / min) * 10000);
  }, [chartData, activeVenues]);

  if (activeVenues.length === 0) {
    return (
      <Card className="col-span-full bg-white/[0.02] border-white/[0.05]">
        <CardHeader className="border-b border-white/[0.05] pb-3">
          <CardTitle className="text-[10px] font-mono font-bold tracking-widest uppercase text-white/50">Cross-Venue Price Trajectory</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-80 flex items-center justify-center text-[10px] font-mono uppercase tracking-widest text-white/30 animate-pulse">
            WAITING FOR ORACLE HISTORY...
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="col-span-full bg-white/[0.02] border-white/[0.05]">
      <CardHeader className="flex flex-row items-center justify-between border-b border-white/[0.05] pb-3 mb-4">
        <CardTitle className="text-[10px] font-mono font-bold tracking-widest uppercase text-white/50">
          Cross-Venue Price Trajectory (NGN/USD)
        </CardTitle>
        <div className="flex items-center gap-4">
          {spread !== null && (
            <div className="flex items-center gap-2 text-[9px] font-mono uppercase tracking-widest">
              <span className="text-white/30">SPREAD:</span>
              <span className={spread > 100 ? 'text-yellow-500/80' : 'text-emerald-400'}>
                {spread} BPS
              </span>
            </div>
          )}
          <div className="flex gap-1 bg-black/20 p-1 rounded-sm border border-white/[0.02]">
            {TIME_WINDOWS.map(({ label, minutes }) => (
              <button
                key={label}
                onClick={() => setWindowMinutes(minutes)}
                className={`px-3 py-1 text-[9px] font-mono tracking-widest uppercase rounded-sm transition-colors ${windowMinutes === minutes
                    ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 shadow-[0_0_10px_rgba(16,185,129,0.1)]'
                    : 'text-white/30 hover:text-white/60 hover:bg-white/5 border border-transparent'
                  }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="h-80">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.02)" vertical={false} />
              <XAxis
                dataKey="time"
                tick={{ fill: 'rgba(255,255,255,0.3)', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                dy={10}
              />
              <YAxis
                domain={[priceRange.min, priceRange.max]}
                tick={{ fill: 'rgba(255,255,255,0.3)', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v) => formatNumber(v, 0)}
                width={50}
                dx={-10}
              />
              <Tooltip
                content={({ active, payload, label }) => {
                  if (!active || !payload?.length) return null;
                  const sorted = [...payload].sort((a, b) => (b.value as number) - (a.value as number));
                  return (
                    <div style={{ backgroundColor: '#0B0E14', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '2px', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', fontSize: '10px', textTransform: 'uppercase', letterSpacing: '0.05em', boxShadow: '0 4px 20px rgba(0,0,0,0.5)', padding: '8px 12px' }}>
                      <div style={{ color: 'rgba(255,255,255,0.4)', marginBottom: '8px' }}>{label}</div>
                      {sorted.map((entry) => (
                        <div key={entry.dataKey as string} style={{ display: 'flex', justifyContent: 'space-between', gap: '24px', color: entry.color, marginBottom: '2px' }}>
                          <span>{VENUE_LABELS[entry.dataKey as string]?.name || entry.dataKey}</span>
                          <span>{formatNumber(entry.value as number, 2)}</span>
                        </div>
                      ))}
                    </div>
                  );
                }}
              />
              <Legend
                formatter={(value) => <span style={{ color: 'rgba(255,255,255,0.5)', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', fontSize: '9px', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{VENUE_LABELS[value]?.name || value}</span>}
                wrapperStyle={{ paddingTop: '20px' }}
              />
              {vwapNgn && isFinite(vwapNgn) && (
                <ReferenceLine
                  y={vwapNgn}
                  stroke="rgba(16,185,129,0.3)"
                  strokeDasharray="4 4"
                  strokeWidth={1}
                  label={{
                    value: `VWAP ${formatNumber(vwapNgn, 1)}`,
                    position: 'insideTopRight',
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                    fontSize: 9,
                    fill: 'rgba(16,185,129,0.8)',
                  }}
                />
              )}
              {activeVenues.map((venue) => (
                <Line
                  key={venue}
                  type="stepAfter"
                  dataKey={venue}
                  stroke={VENUE_COLORS[venue] || '#888'}
                  strokeWidth={1.5}
                  dot={false}
                  activeDot={{ r: 4, fill: '#0B0E14', stroke: VENUE_COLORS[venue] || '#888', strokeWidth: 2 }}
                  connectNulls
                  name={venue}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="flex justify-between items-center text-[9px] text-white/20 mt-6 font-mono tracking-wide border-t border-white/[0.02] pt-3">
          <span>&gt; LIVE HIGH-FREQUENCY ORACLE TRAJECTORY (TICK: ~30S)</span>
          <span>&gt; OPTIMAL VECTOR: SPREAD &gt; 110 BPS TO OVERCOME FRAGMENTATION FEES</span>
        </div>
      </CardContent>
    </Card>
  );
}
