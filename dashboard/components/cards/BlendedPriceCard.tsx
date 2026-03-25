'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { formatNumber } from '@/lib/utils';
import { Activity, TrendingUp, Shield, AlertTriangle } from 'lucide-react';
import type { BlendedPriceResponse } from '@/types';

interface BlendedPriceCardProps {
  blended: BlendedPriceResponse;
}

export function BlendedPriceCard({ blended }: BlendedPriceCardProps) {
  const confidencePct = Math.round(blended.confidence * 100);
  const confidenceColor =
    confidencePct >= 80 ? 'text-green-500' : confidencePct >= 50 ? 'text-yellow-500' : 'text-red-500';
  const degraded = blended.num_sources < blended.total_venues;
  const dexVolumes = blended.dex_volume_24h_usd ?? {};

  const formatCompactUsd = (value: number | null | undefined) => {
    if (value == null) return '—';
    if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
    if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
    return `$${formatNumber(value, 0)}`;
  };

  // VWAP as NGN/USD for display (invert cNGN/USD)
  const ngnPerUsd = blended.vwap > 0 ? 1 / blended.vwap : 0;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">Blended Price</CardTitle>
        <Activity className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        {/* Main VWAP */}
        <div className="flex items-center gap-2 mb-3">
          <TrendingUp className="h-4 w-4 text-muted-foreground" />
          <span className="text-2xl font-bold">
            {formatNumber(ngnPerUsd, 2)}
          </span>
          <span className="text-sm text-muted-foreground">NGN/USD</span>
        </div>

        {/* TWAP values */}
        <div className="grid grid-cols-2 gap-2 text-sm mb-3">
          <div>
            <span className="text-muted-foreground">TWAP 5m: </span>
            <span>{blended.twap_5m > 0 ? formatNumber(1 / blended.twap_5m, 2) : '—'}</span>
          </div>
          <div>
            <span className="text-muted-foreground">TWAP 1h: </span>
            <span>{blended.twap_1h > 0 ? formatNumber(1 / blended.twap_1h, 2) : '—'}</span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground mb-3">
          <div>
            <span>UNI Base 24h Vol: </span>
            <span className="text-foreground">{formatCompactUsd(dexVolumes['uni-base'])}</span>
          </div>
          <div>
            <span>UNI BSC 24h Vol: </span>
            <span className="text-foreground">{formatCompactUsd(dexVolumes['uni-bsc'])}</span>
          </div>
        </div>

        {/* Confidence and sources */}
        <div className="flex items-center gap-2 pt-2 border-t">
          <Shield className={`h-3 w-3 ${confidenceColor}`} />
          <Badge variant="outline" className="text-xs">
            {confidencePct}% confidence
          </Badge>
          <span className={`text-xs ${degraded ? 'text-yellow-500' : 'text-muted-foreground'}`}>
            {blended.num_sources} of {blended.total_venues} sources
          </span>
          {degraded && <AlertTriangle className="h-3 w-3 text-yellow-500" />}
        </div>
      </CardContent>
    </Card>
  );
}
