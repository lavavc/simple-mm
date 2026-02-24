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
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { usePoolMetricsHistory } from '@/lib/hooks/useQueries';
import type { PoolMetricPoint } from '@/types';

const TIME_WINDOWS = [
  { label: '1h',  minutes: 60 },
  { label: '6h',  minutes: 360 },
  { label: '24h', minutes: 1440 },
  { label: '7d',  minutes: 10080 },
] as const;

const COLORS = {
  aerodrome:   '#1976D2',
  pancakeswap: '#7B1FA2',
};

const NAMES = {
  aerodrome:   'Aerodrome',
  pancakeswap: 'PancakeSwap',
};

function fmtUsd(v: number) {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `$${(v / 1_000).toFixed(0)}K`;
  return `$${Math.round(v)}`;
}

function buildChartData(
  points: PoolMetricPoint[],
  minutes: number,
): { tvl: Record<string, number | string>[]; vol: Record<string, number | string>[] } {
  if (!points.length) return { tvl: [], vol: [] };

  const bucketMs =
    minutes <= 60   ? 60_000 :
    minutes <= 360  ? 300_000 :
    minutes <= 1440 ? 1_800_000 :
                      3_600_000;

  const tvlBuckets = new Map<number, Record<string, number>>();
  const volBuckets = new Map<number, Record<string, number>>();

  for (const p of points) {
    const bucket = Math.floor(p.timestamp / bucketMs) * bucketMs;
    if (p.pool_tvl_usd != null) {
      if (!tvlBuckets.has(bucket)) tvlBuckets.set(bucket, {});
      tvlBuckets.get(bucket)![p.venue] = p.pool_tvl_usd;
    }
    if (p.volume_24h_usd != null) {
      if (!volBuckets.has(bucket)) volBuckets.set(bucket, {});
      volBuckets.get(bucket)![p.venue] = p.volume_24h_usd;
    }
  }

  const fmt = (ts: number) =>
    minutes <= 1440
      ? new Date(ts).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
      : new Date(ts).toLocaleDateString('en-US', { weekday: 'short', hour: '2-digit' });

  const toRows = (map: Map<number, Record<string, number>>) =>
    Array.from(map.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([ts, vals]) => ({ time: fmt(ts), ...vals }));

  return { tvl: toRows(tvlBuckets), vol: toRows(volBuckets) };
}

function MetricChart({
  data,
  title,
}: {
  data: Record<string, number | string>[];
  title: string;
}) {
  const venues = Object.keys(COLORS) as (keyof typeof COLORS)[];
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground mb-2">{title}</p>
      {data.length === 0 ? (
        <div className="h-36 flex items-center justify-center text-xs text-muted-foreground">
          Waiting for data&hellip;
        </div>
      ) : (
        <div className="h-36">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data}>
              <XAxis dataKey="time" tick={{ fontSize: 9 }} tickLine={false} axisLine={false} />
              <YAxis
                tick={{ fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={fmtUsd}
                width={44}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--card))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px',
                  fontSize: '11px',
                }}
                formatter={(value: number, name: string) => [
                  fmtUsd(value),
                  NAMES[name as keyof typeof NAMES] || name,
                ]}
              />
              <Legend
                formatter={(v) => NAMES[v as keyof typeof NAMES] || v}
                wrapperStyle={{ fontSize: '10px' }}
              />
              {venues.map((venue) => (
                <Line
                  key={venue}
                  type="monotone"
                  dataKey={venue}
                  stroke={COLORS[venue]}
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

export function PoolMetricsChart() {
  const [minutes, setMinutes] = useState(1440);
  const { data: points } = usePoolMetricsHistory(minutes);
  const { tvl, vol } = useMemo(
    () => buildChartData(points ?? [], minutes),
    [points, minutes],
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">DEX Pool Metrics</CardTitle>
        <div className="flex gap-1">
          {TIME_WINDOWS.map(({ label, minutes: m }) => (
            <Button
              key={label}
              variant={minutes === m ? 'default' : 'ghost'}
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => setMinutes(m)}
            >
              {label}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <MetricChart data={tvl} title="Pool TVL (USD)" />
        <MetricChart data={vol} title="24h Volume (USD)" />
      </CardContent>
    </Card>
  );
}
