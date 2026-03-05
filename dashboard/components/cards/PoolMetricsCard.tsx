'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { usePoolMetrics } from '@/lib/hooks/useQueries';

const LABELS: Record<string, { name: string; chain: string }> = {
  'uni-base': { name: 'Uniswap Base', chain: 'Base' },
  'uni-bsc': { name: 'Uniswap BSC', chain: 'BSC' },
};

function fmtUsd(v: number | null) {
  if (v == null) return '—';
  return '$' + Math.round(v).toLocaleString();
}

export function PoolMetricsCard() {
  const { data } = usePoolMetrics();
  if (!data?.length) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">DEX Pool Metrics</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-6">
          {data.map((pool) => {
            const label = LABELS[pool.venue] ?? { name: pool.venue, chain: pool.chain };
            return (
              <div key={pool.venue} className="space-y-2">
                <p className="text-xs font-medium">
                  {label.name}{' '}
                  <span className="text-muted-foreground">({label.chain})</span>
                </p>
                <div className="space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">24h Volume</span>
                    <span>{fmtUsd(pool.volume_24h_usd)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Pool TVL</span>
                    <span>{fmtUsd(pool.pool_tvl_usd)}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
