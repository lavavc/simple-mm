'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatCurrency, formatNumber } from '@/lib/utils';
import { Wallet } from 'lucide-react';
import type { GlobalPosition } from '@/types';

interface GlobalPositionCardProps {
  position: GlobalPosition;
}

export function GlobalPositionCard({ position }: GlobalPositionCardProps) {
  const deltaPercent = position.delta_ratio * 100;
  const targetPercent = position.target_delta * 100;
  const deviation = Math.abs(deltaPercent - targetPercent);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">Global Position</CardTitle>
        <Wallet className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">
          {formatCurrency(position.total_usd_value)}
        </div>
        <p className="text-xs text-muted-foreground mb-3">Total Value</p>

        <div className="space-y-2">
          <div className="flex justify-between text-sm">
            <span className="text-muted-foreground">Delta Ratio</span>
            <span className={deviation > 5 ? 'text-yellow-500' : 'text-green-500'}>
              {formatNumber(deltaPercent, 1)}% (target: {formatNumber(targetPercent, 0)}%)
            </span>
          </div>

          {/* Delta bar visualization */}
          <div className="h-2 bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full bg-primary transition-all"
              style={{ width: `${Math.min(deltaPercent, 100)}%` }}
            />
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs mt-3">
            <div>
              <span className="text-muted-foreground block">cNGN</span>
              <span>{formatNumber(position.total_cngn, 0)}</span>
            </div>
            <div>
              <span className="text-muted-foreground block">USD</span>
              <span>{formatNumber(position.total_usdc + position.total_usdt, 2)}</span>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
