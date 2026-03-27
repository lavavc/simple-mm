'use client';

import { useMemo, useState } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import { Button } from '@/components/ui/button';
import { TrendingUp } from 'lucide-react';
import { usePoolMetricsHistory } from '@/lib/hooks/useQueries';
import type { PoolMetricPoint } from '@/types';

const TIME_WINDOWS = [
  { label: '24h', minutes: 1440 },
  { label: '7d',  minutes: 10080 },
  { label: '30d', minutes: 43200 },
] as const;

function fmtUsd(v: number) {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `$${(v / 1_000).toFixed(0)}K`;
  return `$${Math.round(v)}`;
}

function buildChartData(points: PoolMetricPoint[], venue: string, minutes: number) {
  const filtered = points.filter(p => p.venue === venue && p.position_value_usd != null);
  if (!filtered.length) return [];

  const bucketMs =
    minutes <= 1440  ? 1_800_000 :
    minutes <= 10080 ? 3_600_000 :
                       21_600_000;

  const buckets = new Map<number, number>();
  for (const p of filtered) {
    const bucket = Math.floor(p.timestamp / bucketMs) * bucketMs;
    buckets.set(bucket, p.position_value_usd!);
  }

  const fmt = (ts: number) =>
    minutes <= 1440
      ? new Date(ts).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
      : new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

  return Array.from(buckets.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([ts, value]) => ({ time: fmt(ts), value }));
}

export function PoolMetricsChart({ venue }: { venue: string }) {
  const [minutes, setMinutes] = useState(1440);
  const { data: points } = usePoolMetricsHistory(minutes);
  const chartData = useMemo(
    () => buildChartData(points ?? [], venue, minutes),
    [points, venue, minutes],
  );

  return (
    <div className="bg-[#12161C] border border-white/[0.05] rounded-sm">
      <div className="p-3 border-b border-white/[0.02] flex items-center justify-between">
        <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center gap-2">
          <TrendingUp className="h-4 w-4 text-white/40" />
          LP POSITION VALUE
        </div>
        <div className="flex gap-1">
          {TIME_WINDOWS.map(({ label, minutes: m }) => (
            <Button
              key={label}
              variant={minutes === m ? 'default' : 'ghost'}
              size="sm"
              className={`h-5 px-2 text-[10px] font-mono uppercase tracking-widest rounded-sm ${
                minutes === m
                  ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/30'
                  : 'text-white/30 hover:text-white/60 hover:bg-white/[0.03]'
              }`}
              onClick={() => setMinutes(m)}
            >
              {label}
            </Button>
          ))}
        </div>
      </div>

      <div className="p-4">
        {chartData.length === 0 ? (
          <div className="h-32 flex items-center justify-center">
            <div className="text-[11px] font-mono text-white/20 uppercase tracking-widest">
              No position history
            </div>
          </div>
        ) : (
          <div className="h-32">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="rgba(255,255,255,0.03)" vertical={false} />
                <XAxis
                  dataKey="time"
                  tick={{ fontSize: 9, fill: 'rgba(255,255,255,0.25)', fontFamily: 'monospace' }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  tick={{ fontSize: 9, fill: 'rgba(255,255,255,0.25)', fontFamily: 'monospace' }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={fmtUsd}
                  width={44}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#0d1117',
                    border: '1px solid rgba(255,255,255,0.06)',
                    borderRadius: '2px',
                    fontSize: '11px',
                    fontFamily: 'monospace',
                    color: 'rgba(255,255,255,0.7)',
                  }}
                  formatter={(v: number) => [fmtUsd(v), 'Position Value']}
                  labelStyle={{ color: 'rgba(255,255,255,0.3)', fontSize: '10px' }}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="#34d399"
                  strokeWidth={1.5}
                  dot={false}
                  connectNulls
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  );
}
