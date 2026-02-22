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
  Activity,
  BellRing
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
    <Card className={`relative overflow-hidden transition-all duration-300 rounded-sm border ${alert.acknowledged
      ? 'bg-white/[0.01] border-white/[0.02] opacity-50'
      : alert.severity === 'critical'
        ? 'bg-gradient-to-r from-red-500/10 to-transparent border-red-500/30 shadow-[0_0_15px_rgba(239,68,68,0.05)]'
        : alert.severity === 'warning'
          ? 'bg-gradient-to-r from-yellow-500/10 to-transparent border-yellow-500/30 shadow-[0_0_15px_rgba(234,179,8,0.05)]'
          : 'bg-white/[0.03] border-white/[0.05] hover:border-emerald-500/30 hover:shadow-[0_0_15px_rgba(16,185,129,0.05)]'
      }`}>
      <CardContent className="p-4 flex flex-col md:flex-row md:items-start gap-4">
        <Icon className={`h-5 w-5 md:mt-0.5 shrink-0 ${alert.acknowledged
          ? 'text-white/20'
          : alert.severity === 'critical'
            ? 'text-red-500'
            : alert.severity === 'warning'
              ? 'text-yellow-500'
              : 'text-emerald-500'
          }`} />
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <span className={`px-2 py-0.5 rounded-sm text-[8px] font-mono uppercase tracking-widest border ${alert.acknowledged
              ? 'bg-white/5 border-white/10 text-white/40'
              : alert.severity === 'critical'
                ? 'bg-red-500/10 border-red-500/20 text-red-500'
                : alert.severity === 'warning'
                  ? 'bg-yellow-500/10 border-yellow-500/20 text-yellow-500'
                  : 'bg-emerald-500/10 border-emerald-500/20 text-emerald-500'
              }`}>
              {severityLabels[alert.severity]}
            </span>
            <span className="bg-white/5 border border-white/10 px-2 py-0.5 rounded-sm text-[8px] font-mono uppercase tracking-widest text-white/50">
              {alert.category}
            </span>
            <span className="text-[9px] font-mono uppercase tracking-widest text-white/30">
              {formatTimestamp(alert.timestamp)}
            </span>
            <span className="text-[8px] font-mono uppercase tracking-widest text-white/20">
              ({formatRelativeTime(alert.timestamp)})
            </span>
          </div>
          <p className={`text-[11px] font-mono break-words leading-relaxed ${alert.acknowledged ? 'text-white/40' : 'text-white/70'}`}>
            {alert.message}
          </p>
        </div>
        <div className="shrink-0 flex flex-col justify-center mt-2 md:mt-0">
          {alert.acknowledged ? (
            <div className="flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-widest text-white/20 px-3 py-1.5 border border-white/5 bg-white/[0.01] rounded-sm">
              <CheckCheck className="h-3 w-3" />
              ACKNOWLEDGED
            </div>
          ) : (
            <button
              onClick={() => onAcknowledge(alert.id)}
              className="group flex items-center justify-center gap-2 px-3 py-1.5 bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 rounded-sm text-[9px] font-mono uppercase tracking-widest text-emerald-500 transition-colors w-full md:w-auto"
            >
              <Check className="h-3 w-3 group-hover:scale-110 transition-transform" />
              ACKNOWLEDGE
            </button>
          )}
        </div>
      </CardContent>
    </Card>
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

  return (
    <div className="relative flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 p-2 md:p-6 animate-in fade-in duration-500 font-sans space-y-6 overflow-hidden">

      {/* Subtle background grid pattern */}
      <div className="absolute inset-0 pointer-events-none opacity-[0.03] bg-[linear-gradient(rgba(255,255,255,1)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,1)_1px,transparent_1px)] bg-[size:40px_40px] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_70%)]" />

      {/* Top Status Bar */}
      <div className="flex items-center justify-between border-b border-white/[0.05] pb-3 z-10">
        <div className="flex items-center gap-3">
          <BellRing className="h-4 w-4 text-emerald-500" />
          <h1 className="text-xs font-bold tracking-widest uppercase text-white">Alerts <span className="text-white/40 font-mono ml-2 normal-case tracking-normal">System notifications and warnings</span></h1>
        </div>
        <div className="flex items-center gap-3">
          {isLoading ? (
            <div className="flex items-center gap-2 bg-emerald-500/10 border border-emerald-500/20 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-emerald-500/80">
              <div className="h-2 w-2 border-t-2 border-emerald-500 rounded-full animate-spin" />
              <span>Querying Logs...</span>
            </div>
          ) : unacknowledgedCount > 0 ? (
            <button onClick={handleAcknowledgeAll} className="flex items-center gap-2 bg-yellow-500/10 hover:bg-yellow-500/20 border border-yellow-500/30 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-yellow-500 transition-colors">
              <CheckCheck className="h-3 w-3" />
              ACKNOWLEDGE ALL ({unacknowledgedCount})
            </button>
          ) : (
            <div className="flex items-center gap-2 bg-emerald-500/10 border border-emerald-500/20 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-emerald-500/80">
              <Check className="h-3 w-3" />
              <span>System Nominal</span>
            </div>
          )}
        </div>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 z-10">
        <Card className="relative bg-gradient-to-br from-white/[0.02] to-transparent border-white/[0.05] backdrop-blur-md shadow-lg">
          <CardContent className="pt-6 pb-4">
            <div className="text-2xl font-mono font-bold text-white mb-1">{isLoading ? <div className="h-8 w-16 bg-white/10 rounded-sm animate-pulse" /> : alerts?.length || 0}</div>
            <div className="text-[9px] font-mono tracking-widest uppercase text-white/40">TOTAL ALERTS</div>
          </CardContent>
        </Card>
        <Card className="relative bg-gradient-to-br from-white/[0.02] to-transparent border-white/[0.05] backdrop-blur-md shadow-lg">
          <CardContent className="pt-6 pb-4">
            <div className="text-2xl font-mono font-bold text-yellow-500 mb-1">{isLoading ? <div className="h-8 w-16 bg-yellow-500/20 rounded-sm animate-pulse" /> : unacknowledgedCount}</div>
            <div className="text-[9px] font-mono tracking-widest uppercase text-white/40">UNACKNOWLEDGED</div>
          </CardContent>
        </Card>
        <Card className="relative bg-gradient-to-br from-white/[0.02] to-transparent border-white/[0.05] backdrop-blur-md shadow-lg">
          <CardContent className="pt-6 pb-4">
            <div className="text-2xl font-mono font-bold text-red-500 mb-1">{isLoading ? <div className="h-8 w-16 bg-red-500/20 rounded-sm animate-pulse" /> : criticalCount}</div>
            <div className="text-[9px] font-mono tracking-widest uppercase text-white/40">CRITICAL</div>
          </CardContent>
        </Card>
        <Card className="relative bg-gradient-to-br from-white/[0.02] to-transparent border-white/[0.05] backdrop-blur-md shadow-lg">
          <CardContent className="pt-6 pb-4">
            <div className="text-2xl font-mono font-bold text-emerald-500 mb-1">{isLoading ? <div className="h-8 w-16 bg-emerald-500/20 rounded-sm animate-pulse" /> : warningCount}</div>
            <div className="text-[9px] font-mono tracking-widest uppercase text-white/40">WARNINGS</div>
          </CardContent>
        </Card>
      </div>

      {/* Filters */}
      <Card className="bg-white/[0.02] border-white/[0.05] z-10 hidden md:block">
        <CardContent className="flex items-center gap-4 py-3">
          <div className="flex items-center gap-2 border-r border-white/10 pr-4">
            <Filter className="h-4 w-4 text-emerald-500/50" />
            <span className="text-[10px] font-mono font-bold tracking-widest uppercase text-white/50">FILTER</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {(
              [
                ['all', 'ALL'],
                ['unacknowledged', 'UNACKNOWLEDGED'],
                ['critical', 'CRITICAL'],
                ['warning', 'WARNING'],
                ['info', 'INFO'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                onClick={() => setFilter(value)}
                className={`flex items-center gap-2 px-3 py-1.5 text-[9px] font-mono tracking-widest uppercase rounded-sm transition-colors border ${filter === value
                  ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
                  : 'bg-white/5 border-transparent text-white/40 hover:bg-white/10 hover:text-white/60'
                  }`}
              >
                {label}
                {value === 'unacknowledged' && unacknowledgedCount > 0 && (
                  <span className="bg-yellow-500/20 text-yellow-500 px-1.5 py-0.5 rounded-[2px] text-[8px]">{unacknowledgedCount}</span>
                )}
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Alerts list */}
      <div className="space-y-3 z-10 pb-12">
        {isLoading ? (
          Array.from({ length: 5 }).map((_, i) => (
            <Card key={i} className="bg-white/[0.02] border border-white/[0.05] min-h-[90px] flex items-center p-4 rounded-sm">
              <div className="h-5 w-5 rounded-full bg-white/10 animate-pulse shrink-0" />
              <div className="ml-4 flex-1 space-y-2">
                <div className="flex gap-2">
                  <div className="h-4 w-16 bg-white/10 rounded-sm animate-pulse" />
                  <div className="h-4 w-20 bg-white/10 rounded-sm animate-pulse" />
                </div>
                <div className="h-3 w-1/2 bg-white/5 rounded-sm animate-pulse" />
              </div>
              <div className="h-8 w-28 bg-white/5 rounded-sm animate-pulse shrink-0 ml-auto hidden md:block" />
            </Card>
          ))
        ) : filteredAlerts && filteredAlerts.length > 0 ? (
          filteredAlerts.map((alert) => (
            <AlertItem
              key={alert.id}
              alert={alert}
              onAcknowledge={handleAcknowledge}
            />
          ))
        ) : (
          <Card className="bg-white/[0.02] border border-dashed border-white/10 rounded-sm">
            <CardContent className="py-12 text-center flex flex-col items-center justify-center">
              <span className="text-[10px] font-mono tracking-widest uppercase text-white/30">&gt; NO ACTIVE ALERTS DETECTED</span>
              <span className="text-[9px] font-mono tracking-widest uppercase text-white/20 mt-2">SYSTEM OPERATING WITHIN NOMINAL PARAMETERS</span>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
