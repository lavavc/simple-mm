'use client';

import { StatusCard } from '@/components/cards/StatusCard';
import { GlobalPositionCard } from '@/components/cards/GlobalPositionCard';
import { BlendedPriceCard } from '@/components/cards/BlendedPriceCard';
import { VenueCard } from '@/components/cards/VenueCard';
import { ArbitrageCard } from '@/components/cards/ArbitrageCard';
import { AlertsList } from '@/components/cards/AlertsList';
import { PoolMetricsChart } from '@/components/charts/PoolMetricsChart';
import { VenuePriceChart } from '@/components/charts/VenuePriceChart';
import {
  useStatus,
  useHealth,
  useGlobalPosition,
  useBlendedPrice,
  useArbitrageStatus,
  useOpportunities,
  useAlerts,
  useAcknowledgeAlert,
} from '@/lib/hooks/useQueries';
import { RefreshCw } from 'lucide-react';

export default function DashboardPage() {
  const { data: status, isLoading: statusLoading } = useStatus();
  const { data: health } = useHealth();
  const { data: globalPosition } = useGlobalPosition();
  const { data: blendedPrice } = useBlendedPrice();
  const { data: arbStatus } = useArbitrageStatus();
  const { data: opportunities } = useOpportunities(10);
  const { data: alerts } = useAlerts(10);

  const acknowledgeAlert = useAcknowledgeAlert();

  const token = process.env.NEXT_PUBLIC_API_TOKEN || '';

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
      <h1 className="text-2xl font-bold">Dashboard</h1>

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

      {/* DEX Pool Metrics */}
      <PoolMetricsChart />

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
