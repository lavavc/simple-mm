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
      <Card className="col-span-full">
        <CardHeader>
          <CardTitle className="text-sm font-medium">Venue Price Comparison</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-48 flex items-center justify-center text-muted-foreground">
            Waiting for price history&hellip;
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="col-span-full">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">
          Venue Price Comparison (NGN/USD)
        </CardTitle>
        <div className="flex items-center gap-2">
          {spread !== null && (
            <span className="text-xs text-muted-foreground mr-2">
              Spread:{' '}
              <span className={spread > 100 ? 'text-yellow-500' : 'text-green-500'}>
                {spread} bps
              </span>
            </span>
          )}
          <div className="flex gap-1">
            {TIME_WINDOWS.map(({ label, minutes }) => (
              <Button
                key={label}
                variant={windowMinutes === minutes ? 'default' : 'ghost'}
                size="sm"
                className="h-6 px-2 text-xs"
                onClick={() => setWindowMinutes(minutes)}
              >
                {label}
              </Button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <XAxis dataKey="time" tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
              <YAxis
                domain={[priceRange.min, priceRange.max]}
                tick={{ fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v) => formatNumber(v, 0)}
                width={50}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--card))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px',
                }}
                labelStyle={{ color: 'hsl(var(--foreground))' }}
                formatter={(value: number, name: string) => [
                  formatNumber(value, 2),
                  VENUE_LABELS[name]?.name || name,
                ]}
              />
              <Legend
                formatter={(value) => VENUE_LABELS[value]?.name || value}
                wrapperStyle={{ fontSize: '12px' }}
              />
              {vwapNgn && isFinite(vwapNgn) && (
                <ReferenceLine
                  y={vwapNgn}
                  stroke="#fff"
                  strokeDasharray="4 4"
                  strokeWidth={1}
                  label={{
                    value: `VWAP ${formatNumber(vwapNgn, 1)}`,
                    position: 'right',
                    fontSize: 10,
                    fill: '#888',
                  }}
                />
              )}
              {activeVenues.map((venue) => (
                <Line
                  key={venue}
                  type="monotone"
                  dataKey={venue}
                  stroke={VENUE_COLORS[venue] || '#888'}
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                  name={venue}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
        <p className="text-xs text-muted-foreground mt-2 text-center">
          Live price history. Tighter lines = better arbitrage execution. Spread &gt;150 bps indicates opportunity.
        </p>
      </CardContent>
    </Card>
  );
}
