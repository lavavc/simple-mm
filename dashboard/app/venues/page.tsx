'use client';

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { formatNumber } from '@/lib/utils';
import { useStatus } from '@/lib/hooks/useQueries';
import { RefreshCw, Pause, Play, RotateCcw, Circle } from 'lucide-react';
import type { VenueStatus } from '@/types';

const venueInfo: Record<
  string,
  { name: string; chain: string; chainId: number; type: string; description: string }
> = {
  aerodrome: {
    name: 'Aerodrome',
    chain: 'Base',
    chainId: 8453,
    type: 'DEX',
    description: 'Concentrated liquidity AMM on Base. Primary DEX for cNGN/USDC pair.',
  },
  quidax: {
    name: 'Quidax',
    chain: 'CEX',
    chainId: 0,
    type: 'CEX',
    description: 'Nigerian crypto exchange. Order ladder management for cNGN/USDT.',
  },
  pancakeswap: {
    name: 'PancakeSwap',
    chain: 'BSC',
    chainId: 56,
    type: 'DEX',
    description: 'Concentrated liquidity AMM on BSC. Primary DEX for cNGN/USDT pair.',
  },
  blockradar: {
    name: 'Blockradar',
    chain: 'Base',
    chainId: 8453,
    type: 'Wallet',
    description: 'B2C wallet integration. Rate setting and liquidity management.',
  },
};

function VenueDetail({ venue }: { venue: VenueStatus }) {
  const info = venueInfo[venue.name] || {
    name: venue.name,
    chain: 'Unknown',
    chainId: 0,
    type: 'Unknown',
    description: '',
  };
  const isActive = venue.enabled && !venue.paused;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <CardTitle>{info.name}</CardTitle>
            <Badge variant="outline">{info.type}</Badge>
            <Badge variant="secondary">{info.chain}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">{info.description}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" disabled>
            <RotateCcw className="h-4 w-4 mr-1" />
            Sync
          </Button>
          <Button variant={venue.paused ? 'default' : 'outline'} size="sm" disabled>
            {venue.paused ? (
              <>
                <Play className="h-4 w-4 mr-1" />
                Resume
              </>
            ) : (
              <>
                <Pause className="h-4 w-4 mr-1" />
                Pause
              </>
            )}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Status */}
          <div>
            <h4 className="text-sm font-medium mb-3">Status</h4>
            <div className="flex items-center gap-2 mb-4">
              <Circle
                className={`h-3 w-3 ${
                  isActive ? 'fill-green-500 text-green-500' : 'fill-yellow-500 text-yellow-500'
                }`}
              />
              <span className={isActive ? 'text-green-500' : 'text-yellow-500'}>
                {isActive ? 'Active' : 'Paused'}
              </span>
            </div>

            {/* LP Position for DEX */}
            {venue.position?.lp_position && (
              <div className="space-y-2 p-3 bg-secondary/50 rounded-lg">
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">LP Token ID</span>
                  <span className="font-mono text-sm">
                    #{venue.position.lp_position.token_id}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">Liquidity</span>
                  <span className="font-mono text-sm">
                    {formatNumber(Number(venue.position.lp_position.liquidity), 0)}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">Range</span>
                  <span className="font-mono text-sm">
                    {formatNumber(venue.position.lp_position.range_min, 6)} -{' '}
                    {formatNumber(venue.position.lp_position.range_max, 6)}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">Status</span>
                  <Badge
                    variant={venue.position.lp_position.in_range ? 'success' : 'warning'}
                  >
                    {venue.position.lp_position.in_range ? 'In Range' : 'Out of Range'}
                  </Badge>
                </div>
              </div>
            )}
          </div>

          {/* Balances */}
          <div>
            <h4 className="text-sm font-medium mb-3">Balances</h4>
            {venue.position?.balances ? (
              <div className="space-y-2">
                {Object.entries(venue.position.balances).map(([token, amount]) => (
                  <div
                    key={token}
                    className="flex items-center justify-between p-2 bg-secondary/50 rounded"
                  >
                    <span className="uppercase font-medium">{token}</span>
                    <span className="font-mono">
                      {formatNumber(amount as number, token === 'cngn' ? 0 : 2)}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No balance data available</p>
            )}
          </div>
        </div>

        {/* Parameters section */}
        {venue.params && (
          <div className="mt-6 pt-4 border-t">
            <div className="flex items-center justify-between mb-3">
              <h4 className="text-sm font-medium">Parameters</h4>
              <Button variant="ghost" size="sm" disabled>
                Edit
              </Button>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              {(venue.name === 'aerodrome' || venue.name === 'pancakeswap') && (
                <>
                  <div>
                    <span className="text-muted-foreground block">SD Multiplier</span>
                    <span>{String(venue.params.sd_multiplier)}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">Rebalance Threshold</span>
                    <span>{String(venue.params.rebalance_threshold_percent)}%</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">Max Slippage</span>
                    <span>{String(venue.params.max_slippage_percent)}%</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">Downside Skew</span>
                    <span>{String(venue.params.downside_skew)}</span>
                  </div>
                </>
              )}
              {venue.name === 'quidax' && (
                <>
                  <div>
                    <span className="text-muted-foreground block">Ladder Enabled</span>
                    <span>{venue.params.ladder_enabled ? 'Yes' : 'No'}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">Order Size cNGN</span>
                    <span>{String(venue.params.order_size_cngn)}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">Order Size USDT</span>
                    <span>{String(venue.params.order_size_usdt)}</span>
                  </div>
                </>
              )}
              {venue.name === 'blockradar' && (
                <div>
                  <span className="text-muted-foreground block">Spread</span>
                  <span>{String(venue.params.spread_bps)} bps</span>
                </div>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function VenuesPage() {
  const { data: status, isLoading } = useStatus();
  const [selectedVenue, setSelectedVenue] = useState<string | null>(null);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const venues = status?.venues || [];
  const displayedVenue = selectedVenue
    ? venues.find((v) => v.name === selectedVenue)
    : venues[0];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Venues</h1>

      {/* Venue tabs */}
      <div className="flex gap-2 border-b pb-2">
        {venues.map((venue) => {
          const info = venueInfo[venue.name];
          const isActive = venue.enabled && !venue.paused;
          const isSelected = (selectedVenue || venues[0]?.name) === venue.name;

          return (
            <Button
              key={venue.name}
              variant={isSelected ? 'secondary' : 'ghost'}
              onClick={() => setSelectedVenue(venue.name)}
              className="gap-2"
            >
              <Circle
                className={`h-2 w-2 ${
                  isActive ? 'fill-green-500 text-green-500' : 'fill-yellow-500 text-yellow-500'
                }`}
              />
              {info?.name || venue.name}
            </Button>
          );
        })}
      </div>

      {/* Selected venue detail */}
      {displayedVenue && <VenueDetail venue={displayedVenue} />}

      {venues.length === 0 && (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            No venues configured
          </CardContent>
        </Card>
      )}
    </div>
  );
}
