'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { formatCurrency, formatBps, formatRelativeTime } from '@/lib/utils';
import { ArrowRightLeft, AlertTriangle } from 'lucide-react';
import type { ArbitrageStatus, ArbitrageOpportunity } from '@/types';

interface ArbitrageCardProps {
  status: ArbitrageStatus;
  opportunities: ArbitrageOpportunity[];
}

export function ArbitrageCard({ status, opportunities }: ArbitrageCardProps) {
  const recentOpportunities = opportunities.slice(0, 3);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">Arbitrage</CardTitle>
        <ArrowRightLeft className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-2 mb-3">
          <Badge variant={status.enabled ? 'success' : 'secondary'}>
            {status.enabled ? 'Enabled' : 'Disabled'}
          </Badge>
          <Badge variant="info">Detection Only</Badge>
          {status.circuit_breaker_active && (
            <Badge variant="destructive" className="flex items-center gap-1">
              <AlertTriangle className="h-3 w-3" />
              Circuit Breaker
            </Badge>
          )}
        </div>

        <div className="grid grid-cols-3 gap-2 text-center mb-4">
          <div>
            <div className="text-lg font-semibold">{status.opportunities_detected_24h}</div>
            <div className="text-xs text-muted-foreground">Detected (24h)</div>
          </div>
          <div>
            <div className="text-lg font-semibold">{status.opportunities_executed_24h}</div>
            <div className="text-xs text-muted-foreground">Executed</div>
          </div>
          <div>
            <div className="text-lg font-semibold">
              {formatCurrency(status.total_profit_24h_usd)}
            </div>
            <div className="text-xs text-muted-foreground">Profit</div>
          </div>
        </div>

        {status.last_scan_timestamp && (
          <p className="text-xs text-muted-foreground mb-3">
            Last scan: {formatRelativeTime(status.last_scan_timestamp)}
          </p>
        )}

        {recentOpportunities.length > 0 ? (
          <div className="space-y-2">
            <h4 className="text-xs font-medium text-muted-foreground">Recent Opportunities</h4>
            {recentOpportunities.map((opp) => (
              <div
                key={opp.id}
                className="flex items-center justify-between text-xs p-2 bg-secondary/50 rounded"
              >
                <span>
                  {opp.buy_venue} → {opp.sell_venue}
                </span>
                <span className="text-green-500">{formatBps(opp.net_spread_bps)}</span>
                <span>{formatCurrency(opp.expected_profit_usd)}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground text-center py-4">
            No opportunities detected
          </p>
        )}
      </CardContent>
    </Card>
  );
}
