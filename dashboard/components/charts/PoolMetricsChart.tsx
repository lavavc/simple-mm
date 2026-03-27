'use client';

import { useMemo, useState } from 'react';
import {
  ComposedChart,
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
  { label: '24h', minutes: 1440 },
  { label: '7d',  minutes: 10080 },
  { label: '30d', minutes: 43200 },
] as const;

const SERIES = {
  uni_base_tvl: { label: 'Uni Base Position', color: '#1976D2', axis: 'left'  },
  uni_bsc_tvl:  { label: 'Uni BSC Position',  color: '#7B1FA2', axis: 'left'  },
  uni_base_vol: { label: 'Uni Base 24hr Vol', color: '#42A5F5', axis: 'right' },
  uni_bsc_vol:  { label: 'Uni BSC 24hr Vol',  color: '#CE93D8', axis: 'right' },
} as const;

function fmtUsd(v: number) {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `$${(v / 1_000).toFixed(0)}K`;
  return `$${Math.round(v)}`;
}

function buildChartData(points: PoolMetricPoint[], minutes: number) {
  if (!points.length) return [];

  const bucketMs =
    minutes <= 1440  ? 1_800_000 :
    minutes <= 10080 ? 3_600_000 :
                       21_600_000;

  const buckets = new Map<number, Record<string, number>>();

  for (const p of points) {
    const bucket = Math.floor(p.timestamp / bucketMs) * bucketMs;
    if (!buckets.has(bucket)) buckets.set(bucket, {});
    const b = buckets.get(bucket)!;
    if (p.venue === 'uni-base') {
      if (p.pool_tvl_usd   != null) b.uni_base_tvl = p.pool_tvl_usd;
      if (p.volume_24h_usd != null) b.uni_base_vol = p.volume_24h_usd;
    } else if (p.venue === 'uni-bsc') {
      if (p.pool_tvl_usd   != null) b.uni_bsc_tvl = p.pool_tvl_usd;
      if (p.volume_24h_usd != null) b.uni_bsc_vol = p.volume_24h_usd;
    }
  }

  const fmt = (ts: number) =>
    minutes <= 1440
      ? new Date(ts).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
      : new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

  return Array.from(buckets.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([ts, vals]) => ({ time: fmt(ts), ...vals }));
}

export function PoolMetricsChart() {
  const [minutes, setMinutes] = useState(1440);
  const { data: points } = usePoolMetricsHistory(minutes);
  const chartData = useMemo(() => buildChartData(points ?? [], minutes), [points, minutes]);

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
      <CardContent>
        {chartData.length === 0 ? (
          <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
            Waiting for data&hellip;
          </div>
        ) : (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData}>
                <XAxis dataKey="time" tick={{ fontSize: 9 }} tickLine={false} axisLine={false} />
                <YAxis
                  yAxisId="left"
                  tick={{ fontSize: 9 }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={fmtUsd}
                  width={48}
                />
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  tick={{ fontSize: 9 }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={fmtUsd}
                  width={48}
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
                    SERIES[name as keyof typeof SERIES]?.label ?? name,
                  ]}
                />
                <Legend
                  verticalAlign="bottom"
                  wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }}
                  formatter={(name) => SERIES[name as keyof typeof SERIES]?.label ?? name}
                />
                {(Object.keys(SERIES) as (keyof typeof SERIES)[]).map((key) => (
                  <Line
                    key={key}
                    type="monotone"
                    dataKey={key}
                    yAxisId={SERIES[key].axis}
                    stroke={SERIES[key].color}
                    strokeWidth={2}
                    dot={false}
                    connectNulls
                  />
                ))}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
