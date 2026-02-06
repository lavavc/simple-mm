'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { formatUptime } from '@/lib/utils';
import { Activity, Pause, Play } from 'lucide-react';

interface StatusCardProps {
  tradingEnabled: boolean;
  uptime: number;
  arbitrageEnabled: boolean;
}

export function StatusCard({ tradingEnabled, uptime, arbitrageEnabled }: StatusCardProps) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">System Status</CardTitle>
        <Activity className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-2 mb-2">
          {tradingEnabled ? (
            <>
              <Play className="h-4 w-4 text-green-500" />
              <span className="text-green-500 font-medium">Running</span>
            </>
          ) : (
            <>
              <Pause className="h-4 w-4 text-yellow-500" />
              <span className="text-yellow-500 font-medium">Paused</span>
            </>
          )}
        </div>
        <p className="text-xs text-muted-foreground mb-2">
          Uptime: {formatUptime(uptime)}
        </p>
        <div className="flex gap-2">
          <Badge variant={tradingEnabled ? 'success' : 'warning'}>
            Trading {tradingEnabled ? 'ON' : 'OFF'}
          </Badge>
          <Badge variant={arbitrageEnabled ? 'info' : 'secondary'}>
            Arb {arbitrageEnabled ? 'ON' : 'OFF'}
          </Badge>
        </div>
      </CardContent>
    </Card>
  );
}
