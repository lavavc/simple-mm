'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { formatRelativeTime, getSeverityColor } from '@/lib/utils';
import { Bell, Check, AlertCircle, AlertTriangle, Info } from 'lucide-react';
import type { Alert } from '@/types';

interface AlertsListProps {
  alerts: Alert[];
  onAcknowledge?: (id: number) => void;
}

const severityIcons = {
  critical: AlertCircle,
  warning: AlertTriangle,
  info: Info,
};

export function AlertsList({ alerts, onAcknowledge }: AlertsListProps) {
  const visible = alerts.filter((a) => !a.acknowledged);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="text-sm font-medium">Alerts</CardTitle>
          {visible.length > 0 && (
            <Badge variant="destructive">{visible.length}</Badge>
          )}
        </div>
        <Bell className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        {visible.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-4">
            No alerts
          </p>
        ) : (
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {visible.map((alert) => {
              const Icon = severityIcons[alert.severity];
              return (
                <div
                  key={alert.id}
                  className={`flex items-start gap-2 p-2 rounded text-sm ${getSeverityColor(alert.severity)}`}
                >
                  <Icon className="h-4 w-4 mt-0.5 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="text-xs">
                        {alert.category}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {formatRelativeTime(alert.timestamp)}
                      </span>
                    </div>
                    <p className="mt-1 break-words">{alert.message}</p>
                  </div>
                  {onAcknowledge && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-6 w-6 shrink-0"
                      onClick={() => onAcknowledge(alert.id)}
                    >
                      <Check className="h-3 w-3" />
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
