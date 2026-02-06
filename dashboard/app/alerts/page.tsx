'use client';

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { formatRelativeTime, formatTimestamp, getSeverityColor } from '@/lib/utils';
import { useAlerts, useAcknowledgeAlert } from '@/lib/hooks/useQueries';
import {
  RefreshCw,
  Check,
  CheckCheck,
  AlertCircle,
  AlertTriangle,
  Info,
  Filter,
} from 'lucide-react';
import type { Alert } from '@/types';

type FilterType = 'all' | 'unacknowledged' | 'critical' | 'warning' | 'info';

const severityIcons = {
  critical: AlertCircle,
  warning: AlertTriangle,
  info: Info,
};

const severityLabels = {
  critical: 'Critical',
  warning: 'Warning',
  info: 'Info',
};

function AlertItem({
  alert,
  onAcknowledge,
}: {
  alert: Alert;
  onAcknowledge: (id: number) => void;
}) {
  const Icon = severityIcons[alert.severity];

  return (
    <div
      className={`flex items-start gap-4 p-4 rounded-lg border ${
        alert.acknowledged ? 'opacity-60 bg-secondary/30' : 'bg-card'
      } ${getSeverityColor(alert.severity)}`}
    >
      <Icon className="h-5 w-5 mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <Badge
            variant={
              alert.severity === 'critical'
                ? 'destructive'
                : alert.severity === 'warning'
                ? 'warning'
                : 'info'
            }
          >
            {severityLabels[alert.severity]}
          </Badge>
          <Badge variant="outline">{alert.category}</Badge>
          <span className="text-xs text-muted-foreground">
            {formatTimestamp(alert.timestamp)}
          </span>
          <span className="text-xs text-muted-foreground">
            ({formatRelativeTime(alert.timestamp)})
          </span>
        </div>
        <p className="text-sm break-words">{alert.message}</p>
      </div>
      <div className="shrink-0">
        {alert.acknowledged ? (
          <Button variant="ghost" size="sm" disabled>
            <CheckCheck className="h-4 w-4 text-green-500" />
          </Button>
        ) : (
          <Button variant="outline" size="sm" onClick={() => onAcknowledge(alert.id)}>
            <Check className="h-4 w-4 mr-1" />
            Acknowledge
          </Button>
        )}
      </div>
    </div>
  );
}

export default function AlertsPage() {
  const [filter, setFilter] = useState<FilterType>('all');
  const { data: alerts, isLoading } = useAlerts(100);
  const acknowledgeAlert = useAcknowledgeAlert();

  const token = process.env.NEXT_PUBLIC_API_TOKEN || '';

  const handleAcknowledge = (id: number) => {
    acknowledgeAlert.mutate({ id, token });
  };

  const handleAcknowledgeAll = () => {
    const unacknowledged = alerts?.filter((a) => !a.acknowledged) || [];
    unacknowledged.forEach((alert) => {
      acknowledgeAlert.mutate({ id: alert.id, token });
    });
  };

  // Apply filters
  const filteredAlerts = alerts?.filter((alert) => {
    switch (filter) {
      case 'unacknowledged':
        return !alert.acknowledged;
      case 'critical':
        return alert.severity === 'critical';
      case 'warning':
        return alert.severity === 'warning';
      case 'info':
        return alert.severity === 'info';
      default:
        return true;
    }
  });

  const unacknowledgedCount = alerts?.filter((a) => !a.acknowledged).length || 0;
  const criticalCount = alerts?.filter((a) => a.severity === 'critical').length || 0;
  const warningCount = alerts?.filter((a) => a.severity === 'warning').length || 0;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Alerts</h1>
          <p className="text-muted-foreground">
            System notifications and warnings
          </p>
        </div>
        {unacknowledgedCount > 0 && (
          <Button variant="outline" onClick={handleAcknowledgeAll}>
            <CheckCheck className="h-4 w-4 mr-2" />
            Acknowledge All ({unacknowledgedCount})
          </Button>
        )}
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card>
          <CardContent className="pt-6">
            <div className="text-2xl font-bold">{alerts?.length || 0}</div>
            <p className="text-xs text-muted-foreground">Total Alerts</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-2xl font-bold text-yellow-500">
              {unacknowledgedCount}
            </div>
            <p className="text-xs text-muted-foreground">Unacknowledged</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-2xl font-bold text-red-500">{criticalCount}</div>
            <p className="text-xs text-muted-foreground">Critical</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-2xl font-bold text-yellow-500">{warningCount}</div>
            <p className="text-xs text-muted-foreground">Warnings</p>
          </CardContent>
        </Card>
      </div>

      {/* Filters */}
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center gap-2">
            <Filter className="h-4 w-4 text-muted-foreground" />
            <CardTitle className="text-sm font-medium">Filter</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            {(
              [
                ['all', 'All'],
                ['unacknowledged', 'Unacknowledged'],
                ['critical', 'Critical'],
                ['warning', 'Warning'],
                ['info', 'Info'],
              ] as const
            ).map(([value, label]) => (
              <Button
                key={value}
                variant={filter === value ? 'default' : 'outline'}
                size="sm"
                onClick={() => setFilter(value)}
              >
                {label}
                {value === 'unacknowledged' && unacknowledgedCount > 0 && (
                  <Badge variant="secondary" className="ml-1">
                    {unacknowledgedCount}
                  </Badge>
                )}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Alerts list */}
      <div className="space-y-3">
        {filteredAlerts && filteredAlerts.length > 0 ? (
          filteredAlerts.map((alert) => (
            <AlertItem
              key={alert.id}
              alert={alert}
              onAcknowledge={handleAcknowledge}
            />
          ))
        ) : (
          <Card>
            <CardContent className="py-8 text-center text-muted-foreground">
              {filter === 'all'
                ? 'No alerts'
                : `No ${filter === 'unacknowledged' ? 'unacknowledged' : filter} alerts`}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
