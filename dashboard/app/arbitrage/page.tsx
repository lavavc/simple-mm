'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { formatCurrency, formatBps, formatRelativeTime, formatNumber } from '@/lib/utils';
import { useArbitrageStatus, useOpportunities, useTriggerScan } from '@/lib/hooks/useQueries';
import {
  RefreshCw,
  Power,
  Play,
  AlertTriangle,
  ArrowRight,
  RotateCcw,
} from 'lucide-react';

export default function ArbitragePage() {
  const { data: status, isLoading: statusLoading } = useArbitrageStatus();
  const { data: opportunities, isLoading: oppsLoading } = useOpportunities(50);
  const triggerScan = useTriggerScan();

  const token = process.env.NEXT_PUBLIC_API_TOKEN || '';

  const handleScan = () => {
    triggerScan.mutate(token);
  };

  if (statusLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Arbitrage</h1>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={handleScan}
            disabled={triggerScan.isPending}
          >
            {triggerScan.isPending ? (
              <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Play className="h-4 w-4 mr-2" />
            )}
            Scan Now
          </Button>
          <Button variant={status?.enabled ? 'destructive' : 'default'} disabled>
            <Power className="h-4 w-4 mr-2" />
            {status?.enabled ? 'Disable' : 'Enable'}
          </Button>
        </div>
      </div>

      {/* Status cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Mode
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2">
              <Badge variant={status?.enabled ? 'success' : 'secondary'}>
                {status?.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
              {status?.detection_only && <Badge variant="info">Detection Only</Badge>}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Last Scan
            </CardTitle>
          </CardHeader>
          <CardContent>
            <span className="text-lg font-semibold">
              {status?.last_scan_timestamp
                ? formatRelativeTime(status.last_scan_timestamp)
                : 'Never'}
            </span>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Circuit Breaker
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2">
              {status?.circuit_breaker_active ? (
                <>
                  <AlertTriangle className="h-4 w-4 text-red-500" />
                  <span className="text-red-500 font-medium">Active</span>
                  <Button variant="ghost" size="sm" className="ml-2" disabled>
                    <RotateCcw className="h-3 w-3 mr-1" />
                    Reset
                  </Button>
                </>
              ) : (
                <>
                  <span className="text-green-500 font-medium">Inactive</span>
                  <span className="text-muted-foreground text-sm">
                    ({status?.consecutive_failures || 0} failures)
                  </span>
                </>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Scan Interval
            </CardTitle>
          </CardHeader>
          <CardContent>
            <span className="text-lg font-semibold">
              {status?.params.scan_interval_seconds || 30}s
            </span>
          </CardContent>
        </Card>
      </div>

      {/* 24h Statistics */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">24h Statistics</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <div className="text-center p-4 bg-secondary/50 rounded-lg">
              <div className="text-2xl font-bold">
                {status?.opportunities_detected_24h || 0}
              </div>
              <div className="text-sm text-muted-foreground">Detected</div>
            </div>
            <div className="text-center p-4 bg-secondary/50 rounded-lg">
              <div className="text-2xl font-bold">
                {status?.opportunities_executed_24h || 0}
              </div>
              <div className="text-sm text-muted-foreground">Executed</div>
            </div>
            <div className="text-center p-4 bg-secondary/50 rounded-lg">
              <div className="text-2xl font-bold text-green-500">
                {formatCurrency(status?.total_profit_24h_usd || 0)}
              </div>
              <div className="text-sm text-muted-foreground">Profit</div>
            </div>
            <div className="text-center p-4 bg-secondary/50 rounded-lg">
              <div className="text-2xl font-bold">
                {formatCurrency(status?.daily_volume_usd || 0)}
              </div>
              <div className="text-sm text-muted-foreground">Volume</div>
            </div>
            <div className="text-center p-4 bg-secondary/50 rounded-lg">
              <div className="text-2xl font-bold">
                {formatCurrency(status?.inventory_imbalance_usd || 0)}
              </div>
              <div className="text-sm text-muted-foreground">Imbalance</div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Opportunities table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-medium">Detected Opportunities</CardTitle>
          <Badge variant="secondary">
            {opportunities?.length || 0} total
          </Badge>
        </CardHeader>
        <CardContent>
          {oppsLoading ? (
            <div className="flex items-center justify-center py-8">
              <RefreshCw className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : opportunities && opportunities.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b">
                    <th className="text-left py-2 font-medium">Time</th>
                    <th className="text-left py-2 font-medium">Route</th>
                    <th className="text-right py-2 font-medium">Buy Price</th>
                    <th className="text-right py-2 font-medium">Sell Price</th>
                    <th className="text-right py-2 font-medium">Gross</th>
                    <th className="text-right py-2 font-medium">Net</th>
                    <th className="text-right py-2 font-medium">Size</th>
                    <th className="text-right py-2 font-medium">Est. Profit</th>
                    <th className="text-center py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {opportunities.map((opp) => (
                    <tr key={opp.id} className="border-b hover:bg-secondary/30">
                      <td className="py-2 text-muted-foreground">
                        {formatRelativeTime(opp.timestamp)}
                      </td>
                      <td className="py-2">
                        <div className="flex items-center gap-1">
                          <Badge variant="outline" className="text-xs">
                            {opp.buy_venue}
                          </Badge>
                          <ArrowRight className="h-3 w-3 text-muted-foreground" />
                          <Badge variant="outline" className="text-xs">
                            {opp.sell_venue}
                          </Badge>
                        </div>
                      </td>
                      <td className="py-2 text-right font-mono">
                        {formatNumber(opp.buy_price, 6)}
                      </td>
                      <td className="py-2 text-right font-mono">
                        {formatNumber(opp.sell_price, 6)}
                      </td>
                      <td className="py-2 text-right">
                        {formatBps(opp.gross_spread_bps)}
                      </td>
                      <td className="py-2 text-right text-green-500">
                        {formatBps(opp.net_spread_bps)}
                      </td>
                      <td className="py-2 text-right">
                        {formatCurrency(opp.recommended_size_usd)}
                      </td>
                      <td className="py-2 text-right text-green-500 font-medium">
                        {formatCurrency(opp.expected_profit_usd)}
                      </td>
                      <td className="py-2 text-center">
                        <Badge
                          variant={
                            opp.status === 'completed'
                              ? 'success'
                              : opp.status === 'executing'
                              ? 'warning'
                              : opp.status === 'abandoned' || opp.status === 'expired'
                              ? 'destructive'
                              : 'secondary'
                          }
                        >
                          {opp.status}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-center text-muted-foreground py-8">
              No opportunities detected yet
            </p>
          )}
        </CardContent>
      </Card>

      {/* Parameters */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-medium">Parameters</CardTitle>
          <Button variant="ghost" size="sm" disabled>
            Edit
          </Button>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <span className="text-muted-foreground block">Min Spread</span>
              <span>{status?.params.min_spread_bps} bps</span>
            </div>
            <div>
              <span className="text-muted-foreground block">Min Net Profit</span>
              <span>{status?.params.min_net_profit_bps} bps</span>
            </div>
            <div>
              <span className="text-muted-foreground block">DEX Swap Fee</span>
              <span>{status?.params.dex_swap_fee_bps} bps</span>
            </div>
            <div>
              <span className="text-muted-foreground block">DEX Slippage</span>
              <span>{status?.params.dex_slippage_bps} bps</span>
            </div>
            <div>
              <span className="text-muted-foreground block">CEX Taker Fee</span>
              <span>{status?.params.cex_taker_fee_bps} bps</span>
            </div>
            <div>
              <span className="text-muted-foreground block">Max Single Trade</span>
              <span>{formatCurrency(status?.params.max_single_trade_usd || 0)}</span>
            </div>
            <div>
              <span className="text-muted-foreground block">Max Daily Volume</span>
              <span>{formatCurrency(status?.params.max_daily_volume_usd || 0)}</span>
            </div>
            <div>
              <span className="text-muted-foreground block">Max Imbalance</span>
              <span>{formatCurrency(status?.params.max_inventory_imbalance_usd || 0)}</span>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
