'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { formatNumber, normalizeToNgnUsd, spreadBps, isDex, VENUE_LABELS } from '@/lib/utils';
import { Circle, TrendingUp, AlertCircle } from 'lucide-react';
import type { VenueStatus } from '@/types';

interface VenueCardProps {
  venue: VenueStatus;
}

export function VenueCard({ venue }: VenueCardProps) {
  const label = VENUE_LABELS[venue.name] || { name: venue.name, chain: 'Unknown', type: '?' };
  const isActive = venue.enabled && !venue.paused;
  const hasPrice = !!venue.price?.quote;
  const priceError = venue.price?.error;

  const normalized = venue.price ? normalizeToNgnUsd(venue.price) : null;
  // Don't show spread for DEX venues (AMM pools don't have order book spreads)
  const spread = normalized && !isDex(venue.name) ? spreadBps(normalized) : null;

  return (
    <Card className="hover:border-primary/50 transition-colors">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <div className="flex items-center gap-2">
          <CardTitle className="text-sm font-medium">{label.name}</CardTitle>
          <Badge variant="outline" className="text-xs">{label.type}</Badge>
        </div>
        <Circle
          className={`h-3 w-3 ${
            isActive ? 'fill-green-500 text-green-500' : 'fill-yellow-500 text-yellow-500'
          }`}
        />
      </CardHeader>
      <CardContent>
        {normalized ? (
          <div className="mb-3">
            <div className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-muted-foreground" />
              <span className="text-lg font-bold">{formatNumber(normalized.mid, 2)}</span>
              <span className="text-xs text-muted-foreground">NGN/USD</span>
            </div>
            {spread !== null && (
              <div className="mt-1">
                <Badge variant="outline" className="text-xs">{spread} bps spread</Badge>
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 mt-2 text-xs">
              <div>
                <span className="text-muted-foreground">Bid: </span>
                <span className="text-green-500">{formatNumber(normalized.bid, 2)}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Ask: </span>
                <span className="text-red-500">{formatNumber(normalized.ask, 2)}</span>
              </div>
            </div>
            {venue.price && venue.price.age_seconds > 0 && (
              <p className="text-xs text-muted-foreground mt-1">
                Updated {Math.round(venue.price.age_seconds)}s ago
              </p>
            )}
          </div>
        ) : hasPrice && venue.price?.pair === 'cNGN/NGN' ? (
          <div className="mb-3">
            <div className="flex items-center gap-2">
              <span className="text-lg font-bold">{formatNumber(venue.price.quote!.mid, 4)}</span>
              <span className="text-xs text-muted-foreground">cNGN/NGN</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">Peg rate (not USD convertible)</p>
          </div>
        ) : priceError ? (
          <div className="flex items-center gap-2 text-sm text-yellow-500 mb-3">
            <AlertCircle className="h-4 w-4" />
            <span>Price unavailable</span>
          </div>
        ) : (
          <div className="text-sm text-muted-foreground mb-3">No price data</div>
        )}

        {venue.name === 'blockradar' && venue.position?.rates && (
          <div className="mt-3 space-y-1">
            <h4 className="text-xs font-medium text-muted-foreground">Live Rates (cNGN/USD)</h4>
            {Object.entries(venue.position.rates).map(([key, rate]) => (
              <div key={key} className="flex justify-between text-xs">
                <span className="text-muted-foreground">{key.replace('_', '/')}</span>
                <span className="font-mono">{formatNumber(rate, 6)}</span>
              </div>
            ))}
          </div>
        )}

        {venue.position && (
          <div className="pt-2 border-t">
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              {Object.entries(venue.position.balances).filter(([, amount]) => Number(amount) > 0).map(([token, amount]) => (
                <div key={token} className="flex justify-between">
                  <span className="text-muted-foreground uppercase">{token}</span>
                  <span>{formatNumber(amount as number, token === 'cngn' ? 0 : 2)}</span>
                </div>
              ))}
            </div>
            {venue.position.lp_position && (
              <div className="mt-2">
                <Badge
                  variant={venue.position.lp_position.in_range ? 'success' : 'warning'}
                  className="text-xs"
                >
                  LP {venue.position.lp_position.in_range ? 'In Range' : 'Out of Range'}
                </Badge>
              </div>
            )}
            {(venue.position.volume_24h_usd != null || venue.position.pool_tvl_usd != null) && (
              <div className="mt-2 text-xs space-y-0.5">
                {venue.position.volume_24h_usd != null && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">24h Volume</span>
                    <span>${Math.round(venue.position.volume_24h_usd).toLocaleString()}</span>
                  </div>
                )}
                {venue.position.pool_tvl_usd != null && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Pool TVL</span>
                    <span>${Math.round(venue.position.pool_tvl_usd).toLocaleString()}</span>
                  </div>
                )}
                {venue.position.lp_position?.our_share_pct != null && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Our Share</span>
                    <span>{Number(venue.position.lp_position.our_share_pct).toFixed(2)}%</span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
