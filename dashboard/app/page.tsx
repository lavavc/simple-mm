'use client';

import { StatusCard } from '@/components/cards/StatusCard';
import { GlobalPositionCard } from '@/components/cards/GlobalPositionCard';
import { BlendedPriceCard } from '@/components/cards/BlendedPriceCard';
import { VenueCard } from '@/components/cards/VenueCard';
import { ArbitrageCard } from '@/components/cards/ArbitrageCard';
import { AlertsList } from '@/components/cards/AlertsList';
import { VenuePriceChart } from '@/components/charts/VenuePriceChart';
import { Button } from '@/components/ui/button';
import {
  useStatus,
  useHealth,
  useGlobalPosition,
  useBlendedPrice,
  useArbitrageStatus,
  useOpportunities,
  useAlerts,
  usePauseTrading,
  useResumeTrading,
  useAcknowledgeAlert,
} from '@/lib/hooks/useQueries';
import { Pause, Play, RefreshCw } from 'lucide-react';

export default function DashboardPage() {
  const { data: status, isLoading: statusLoading } = useStatus();
  const { data: health } = useHealth();
  const { data: globalPosition } = useGlobalPosition();
  const { data: blendedPrice } = useBlendedPrice();
  const { data: arbStatus } = useArbitrageStatus();
  const { data: opportunities } = useOpportunities(10);
  const { data: alerts } = useAlerts(10);

  const pauseTrading = usePauseTrading();
  const resumeTrading = useResumeTrading();
  const acknowledgeAlert = useAcknowledgeAlert();

  const token = process.env.NEXT_PUBLIC_API_TOKEN || '';

  const handleToggleTrading = () => {
    if (status?.trading_enabled) {
      pauseTrading.mutate(token);
    } else {
      resumeTrading.mutate(token);
    }
  };

  const handleAcknowledgeAlert = (id: number) => {
    acknowledgeAlert.mutate({ id, token });
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
      {/* Header with trading control */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <Button
          variant={status?.trading_enabled ? 'destructive' : 'default'}
          onClick={handleToggleTrading}
          disabled={pauseTrading.isPending || resumeTrading.isPending}
        >
          {status?.trading_enabled ? (
            <>
              <Pause className="h-4 w-4 mr-2" />
              Pause Trading
            </>
          ) : (
            <>
              <Play className="h-4 w-4 mr-2" />
              Resume Trading
            </>
          )}
        </Button>
      </div>

      {/* Top row: Status, Blended Price, Position */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {status && (
          <StatusCard
            tradingEnabled={status.trading_enabled}
            uptime={status.uptime}
            arbitrageEnabled={health?.arbitrage_enabled ?? false}
          />
        )}
        {blendedPrice && <BlendedPriceCard blended={blendedPrice} />}
        {globalPosition && <GlobalPositionCard position={globalPosition} />}
      </div>

      {/* Venues Grid */}
      {status?.venues && status.venues.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-3">Venues</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {status.venues.map((venue) => (
              <VenueCard key={venue.name} venue={venue} />
            ))}
          </div>
        </div>
      )}

      {/* Venue Price Comparison Chart */}
      <VenuePriceChart blended={blendedPrice} />

      {/* Bottom row: Arbitrage and Alerts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {arbStatus && opportunities && (
          <ArbitrageCard status={arbStatus} opportunities={opportunities} />
        )}
        {alerts && (
          <AlertsList alerts={alerts} onAcknowledge={handleAcknowledgeAlert} />
        )}
      </div>
    </div>
  );
}
