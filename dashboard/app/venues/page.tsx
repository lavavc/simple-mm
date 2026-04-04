'use client';

import React, { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { formatNumber } from '@/lib/utils';
import { useStatus, usePortfolioValuation } from '@/lib/hooks/useQueries';
import { Play, Pause, RotateCcw, Database, Settings, Activity as ActivityIcon, Wallet, Zap, Server, Network, ShieldCheck, Gauge, ExternalLink } from 'lucide-react';
import type { VenueStatus } from '@/types';
import { PoolMetricsChart } from '@/components/charts/PoolMetricsChart';

const venueInfo: Record<
  string,
  { name: string; chain: string; chainId: number; type: string; description: string }
> = {
  'uni-base': {
    name: 'Uniswap Base',
    chain: 'Base',
    chainId: 8453,
    type: 'DEX',
    description: 'Uniswap V4 pool on Base. Primary DEX for cNGN/USDC pair.',
  },
  quidax: {
    name: 'Quidax',
    chain: 'CEX',
    chainId: 0,
    type: 'CEX',
    description: 'Nigerian crypto exchange. Order ladder management for cNGN/USDT.',
  },
  'uni-bsc': {
    name: 'Uniswap BSC',
    chain: 'BSC',
    chainId: 56,
    type: 'DEX',
    description: 'Uniswap V4 pool on BSC. Primary DEX for cNGN/USDT pair.',
  },
  blockradar: {
    name: 'Blockradar',
    chain: 'Base',
    chainId: 8453,
    type: 'Wallet',
    description: 'B2C wallet integration. Rate setting and liquidity management.',
  }
};

function VenueDetail({ venue, isSyncing }: { venue: VenueStatus; isSyncing: boolean }) {
  const info = venueInfo[venue.name] || {
    name: venue.name,
    chain: 'Unknown',
    chainId: 0,
    type: 'Unknown',
    description: '',
  };
  const isActive = venue.enabled && !venue.paused;

  // Fake telemetry
  const [latency, setLatency] = useState(12);
  const [blockHeight, setBlockHeight] = useState(18349200);

  useEffect(() => {
    const interval = setInterval(() => {
      setLatency(10 + Math.floor(Math.random() * 8));
      setBlockHeight(prev => prev + (Math.random() > 0.7 ? 1 : 0));
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  const { data: valuationData } = usePortfolioValuation();

  // Map venue -> wallet roles that belong to it
  const VENUE_ROLES: Record<string, string[]> = {
    quidax: ['quidax-exchange', 'quidax-lp', 'quidax-trade-fund'],
    'uni-bsc': ['uni-bsc-trade', 'uni-bsc-lp'],
    'uni-base': ['uni-base-trade', 'uni-base-lp'],
  };
  const roles = VENUE_ROLES[venue.name] || [];

  // Sum value_usd for cNGN across all roles belonging to this venue
  const cNGNValueUSD = valuationData?.venues
    ? roles.reduce((total, role) => {
        const cngn = valuationData.venues[role]?.['cNGN'] ?? valuationData.venues[role]?.['cngn'];
        return total + (Number(cngn?.value_usd) || 0);
      }, 0)
    : 0;
  // Live spot from venue's own price, as fallback
  const spotPrice = Number(venue.price?.quote?.mid) || 0.00066;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 animate-in fade-in duration-300">
      {/* LEFT COLUMN: Controls & Status */}
      <div className="lg:col-span-1 space-y-4">
        {/* Identity & Controls */}
        <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
          <CardHeader className="p-3 border-b border-white/[0.02]">
            <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center justify-between">
              <div className="flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-emerald-500/70" />
                TARGET VENUE
              </div>
              <span className={`flex h-1.5 w-1.5 relative ${isActive ? 'opacity-100' : 'opacity-50'}`}>
                {isActive && <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>}
                <span className={`relative inline-flex rounded-full h-1.5 w-1.5 ${isActive ? 'bg-emerald-500' : 'bg-yellow-500'}`}></span>
              </span>
            </div>
          </CardHeader>
          <CardContent className="p-4 space-y-4">
            <div>
              <div className="text-xl font-mono text-white tracking-widest uppercase mb-1">{info.name}</div>
              <div className="text-[11px] text-white/60 font-mono tracking-wide mb-3 leading-relaxed">{info.description}</div>
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-mono px-2 py-0.5 rounded-sm border bg-blue-500/10 border-blue-500/30 text-blue-400 tracking-widest uppercase">{info.type}</span>
                <span className="text-[10px] font-mono px-2 py-0.5 rounded-sm border bg-purple-500/10 border-purple-500/30 text-purple-400 tracking-widest uppercase">{info.chain}</span>
              </div>
            </div>

            <div className="h-px w-full bg-white/[0.05]"></div>
          </CardContent>
        </Card>

        {/* System Telemetry */}
        <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
          <CardHeader className="p-3 border-b border-white/[0.02]">
            <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center gap-2">
              <Server className="h-4 w-4" />
              NODE TELEMETRY
            </div>
          </CardHeader>
          <CardContent className="p-3 space-y-3">
            <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02] text-[10px] font-mono">
              <div className="flex items-center gap-2 text-white/60"><ActivityIcon className="h-3.5 w-3.5" /> RPC Latency</div>
              <div className="text-emerald-400 text-xs">{isSyncing ? '--' : `${latency}ms`}</div>
            </div>
            <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02] text-[10px] font-mono">
              <div className="flex items-center gap-2 text-white/60"><Database className="h-3.5 w-3.5" /> Chain ID</div>
              <div className="text-white/80 text-xs">{info.chainId}</div>
            </div>
            <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02] text-[10px] font-mono">
              <div className="flex items-center gap-2 text-white/60"><Network className="h-3.5 w-3.5" /> Block Height</div>
              <div className="text-blue-400 text-xs">{isSyncing ? '--' : formatNumber(blockHeight, 0).replace(/,/g, '')}</div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* RIGHT COLUMNS: Content */}
      <div className="lg:col-span-3 space-y-4">

        {/* Top Row: Liquidity & Balances */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Balances */}
          <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
            <CardHeader className="p-3 border-b border-white/[0.05]">
              <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Wallet className="h-4 w-4 text-white/60" />
                  VENUE INVENTORY
                </div>
                {isSyncing && <div className="h-1.5 w-1.5 bg-white/20 rounded-full animate-ping" />}
              </div>
            </CardHeader>
            <CardContent className={`p-0 transition-opacity duration-300 ${isSyncing ? 'opacity-30' : ''}`}>
              {venue.position?.balances ? (
                <div className="p-4 space-y-4">
                  <div className="grid grid-cols-2 gap-4">
                    {Object.entries(venue.position.balances).map(([token, amount]) => {
                      const isCngn = token.toLowerCase() === 'cngn';

                      let usdValue = Number(amount) || 0;
                      if (isCngn) {
                        // Use exact slippage-adjusted liquidation if available, else live spot
                        usdValue = cNGNValueUSD > 0
                          ? cNGNValueUSD
                          : (Number(amount) || 0) * spotPrice;
                      }

                      return (
                        <div key={token} className="bg-black/40 border border-white/[0.02] rounded-sm p-3.5">
                          <div className="flex justify-between items-start mb-2">
                            <span className={`text-[11px] uppercase tracking-widest font-bold ${isCngn ? 'text-emerald-500' : 'text-blue-500'}`}>{token}</span>
                            <span className="text-sm font-mono text-white">{formatNumber(Number(amount) || 0, isCngn ? 0 : 2)}</span>
                          </div>

                          {isCngn && cNGNValueUSD > 0 ? (
                            <div className="border-t border-white/[0.05] pt-2">
                              <div className="text-[9px] text-white/30 font-mono uppercase tracking-widest mb-1">cNGN val</div>
                              <div className="text-[10px] font-mono text-amber-400/90">${formatNumber(cNGNValueUSD, 2)}</div>
                            </div>
                          ) : (
                            <div className="text-[10px] text-white/50 font-mono text-right border-t border-white/[0.05] pt-2">
                              ≈ ${formatNumber(usdValue, 2)}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  
                  {/* Total Venue Liquid Value */}
                  <div className="mt-4 pt-3 border-t border-white/[0.05] flex justify-between items-center">
                    <span className="text-[10px] text-white/40 font-mono uppercase tracking-widest">Total Liquid Value</span>
                    <span className="text-sm font-mono text-white/90 font-bold">
                      ${formatNumber(
                        Object.entries(venue.position.balances).reduce((acc, [t, a]) => {
                          const isC = t.toLowerCase() === 'cngn';
                          let v = Number(a) || 0;
                          if (isC) {
                            v = cNGNValueUSD > 0 ? cNGNValueUSD : (Number(a) || 0) * spotPrice;
                          }
                          return acc + v;
                        }, 0), 2
                      )}
                    </span>
                  </div>
                </div>
              ) : (
                <div className="py-12 flex flex-col items-center justify-center text-center">
                  {isSyncing ? (
                    <div className="flex flex-col items-center space-y-3">
                      <div className="h-5 w-5 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin" />
                      <div className="text-[11px] text-emerald-500/70 uppercase tracking-widest font-mono animate-pulse">Establishing Connection...</div>
                      <div className="text-[11px] font-mono text-white/40">Fetching Balances</div>
                    </div>
                  ) : (
                    <>
                      <Database className="h-6 w-6 text-white/20 mb-3" />
                      <div className="text-[11px] font-mono text-white/40 uppercase tracking-widest">Telemetry Unavailable</div>
                    </>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          {venue.position?.lp_position ? (
            <Card className={`bg-[#12161C] border rounded-sm shadow-none transition-colors duration-500 ${venue.position.lp_position.in_range ? 'border-emerald-500/30' : 'border-yellow-500/30'}`}>
              <CardHeader className={`p-3 border-b flex flex-row items-center justify-between ${venue.position.lp_position.in_range ? 'border-emerald-500/10 bg-emerald-500/[0.02]' : 'border-yellow-500/10 bg-yellow-500/[0.02]'}`}>
                <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center gap-2">
                  <Gauge className={`h-4 w-4 ${venue.position.lp_position.in_range ? 'text-emerald-400' : 'text-yellow-400'}`} />
                  LIQUIDITY SENSOR
                </div>
                <div className={`text-[10px] uppercase tracking-widest font-mono px-2 py-0.5 rounded-sm border ${venue.position.lp_position.in_range ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400' : 'bg-yellow-500/10 border-yellow-500/20 text-yellow-400'}`}>
                  {venue.position.lp_position.in_range ? 'IN RANGE' : 'OUT RANGE'}
                </div>
              </CardHeader>
              <CardContent className="p-4 space-y-4">
                <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02]">
                  <div className="text-[10px] text-white/50 uppercase tracking-widest">
                    {venue.position.lp_position.position_count > 1 ? 'Vector Position IDs' : 'Vector Position ID'}
                  </div>
                  <div className="text-sm font-mono text-white">
                    {venue.position.lp_position.token_id
                      ? `#${venue.position.lp_position.token_id}`
                      : `${venue.position.lp_position.position_count} positions`}
                  </div>
                </div>

                <div className="bg-black/40 p-3.5 rounded-sm border border-white/[0.02] space-y-4">
                  <div className="flex justify-between items-end">
                    <div className="text-[10px] text-white/50 uppercase tracking-widest">Active Liquidity Volume</div>
                    <div className="text-lg font-mono text-emerald-400">{formatNumber(Number(venue.position.lp_position.liquidity), 0)}</div>
                  </div>

                  {/* Visual Range Bar Mock */}
                  <div className="space-y-1.5">
                    <div className="flex justify-between text-[10px] font-mono text-white/50">
                      <span>MIN {formatNumber(venue.position.lp_position.range_min, 6)}</span>
                      <span>MAX {formatNumber(venue.position.lp_position.range_max, 6)}</span>
                    </div>
                    <div className="h-2 w-full bg-black rounded-full overflow-hidden border border-white/[0.05] relative">
                      {venue.position.lp_position.in_range ? (
                        <div className="absolute top-0 bottom-0 left-[20%] right-[20%] bg-emerald-500/50 rounded-full">
                          <div className="absolute top-0 bottom-0 left-[45%] w-1.5 bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,1)]"></div>
                        </div>
                      ) : (
                        <div className="absolute top-0 bottom-0 left-[20%] right-[20%] bg-white/10 rounded-full">
                          <div className="absolute top-0 bottom-0 left-[5%] w-1.5 bg-yellow-400 shadow-[0_0_8px_rgba(250,204,21,1)]"></div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          ) : (
            <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
              <CardHeader className="p-3 border-b border-white/[0.02]">
                <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center gap-2">
                  <Gauge className="h-4 w-4" /> VENUE SPOT PRICE
                </div>
              </CardHeader>
              <CardContent className="py-12 flex flex-col items-center justify-center text-center">
                {isSyncing ? (
                  <div className="flex flex-col items-center space-y-3">
                    <div className="h-5 w-5 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin" />
                    <div className="text-[11px] text-emerald-500/70 uppercase tracking-widest font-mono animate-pulse">Establishing Connection...</div>
                    <div className="text-[11px] font-mono text-white/40">Fetching Sensors</div>
                  </div>
                ) : (
                  <>
                    <div className="text-3xl font-mono text-white tracking-tight mb-2">
                      ${venue.price?.quote?.mid ? Number(venue.price.quote.mid).toFixed(7) : '0.0000000'}
                    </div>
                    <div className="flex items-center gap-2 mb-4">
                      <span className="text-[10px] text-emerald-500/70 uppercase tracking-widest font-mono border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 rounded-sm">MID PRICE</span>
                      <span className="text-[10px] text-white/40 uppercase tracking-widest font-mono">{venue.price?.pair || 'cNGN/USD'}</span>
                    </div>

                    {venue.name === 'quidax' && venue.price?.quote && (
                      <div className="flex gap-6 mb-4">
                        <div className="text-center">
                          <div className="text-[8px] text-white/30 font-mono uppercase tracking-widest mb-1">BID</div>
                          <div className="text-sm font-mono text-emerald-400">${Number(venue.price.quote.bid).toFixed(7)}</div>
                        </div>
                        <div className="text-center">
                          <div className="text-[8px] text-white/30 font-mono uppercase tracking-widest mb-1">SPREAD</div>
                          <div className="text-sm font-mono text-white/60">
                            {venue.price.quote.bid && venue.price.quote.ask
                              ? `${((Number(venue.price.quote.ask) - Number(venue.price.quote.bid)) / Number(venue.price.quote.bid) * 10000).toFixed(0)} bps`
                              : '—'}
                          </div>
                        </div>
                        <div className="text-center">
                          <div className="text-[8px] text-white/30 font-mono uppercase tracking-widest mb-1">ASK</div>
                          <div className="text-sm font-mono text-red-400">${Number(venue.price.quote.ask).toFixed(7)}</div>
                        </div>
                      </div>
                    )}

                    <div className="h-px w-24 bg-white/10 mb-4" />

                    <div className="text-[10px] text-white/30 font-mono uppercase tracking-widest mb-1">NGN per USD</div>
                    <div className="text-2xl font-mono text-emerald-400 tracking-tight mb-1">
                      {venue.price?.quote?.mid ? Math.round(1 / Number(venue.price.quote.mid)).toLocaleString() : '—'}
                    </div>
                    <div className="text-[10px] text-white/30 font-mono uppercase tracking-widest mb-4">NGN per $1 USD</div>
                    {['uni-base', 'uni-bsc'].includes(venue.name) && (
                      <a
                        href={venue.name === 'uni-base'
                          ? 'https://app.uniswap.org/explore/pools/base/0x84fa97768196067f0e5aa157709039a3897e219cba3002d9ad38bf44e300fe93'
                          : 'https://app.uniswap.org/explore/pools/bnb/0x2268f03a28f37f16cd3610dc669536f8c815d9d4cb2906feeeba9150fb2d8596'}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[10px] font-mono text-emerald-500/70 uppercase tracking-widest hover:text-emerald-400 flex items-center gap-1"
                      >
                        Pool data <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                  </>
                )}
              </CardContent>
            </Card>
          )}
        </div>


        {/* LP Position Value History (DEX venues only) */}
        {['uni-base', 'uni-bsc'].includes(venue.name) && (
          <PoolMetricsChart venue={venue.name} />
        )}

        {/* Bottom Row: Parameters (Detailed List) */}
        <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
          <CardHeader className="p-3 border-b border-white/[0.02]">
            <div className="flex items-center justify-between">
              <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center gap-2">
                <Settings className="h-4 w-4 text-white/60" /> PROTOCOL PARAMETERS & CONSTRAINTS
              </div>
              <Button disabled variant="outline" className="h-7 bg-transparent border-white/[0.05] text-[10px] font-mono uppercase tracking-widest text-emerald-500/70 hover:text-emerald-400 hover:border-emerald-500/30 hover:bg-emerald-500/10">Modify Constraints</Button>
            </div>
          </CardHeader>
          <CardContent className="p-0">
            <div className="divide-y divide-white/[0.02]">
              {['uni-base', 'uni-bsc'].includes(venue.name) && (
                <>
                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Pricing Model SD Multiplier</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Width of liquidity curve in standard deviations</div>
                    </div>
                    <div className="text-sm font-mono text-emerald-400 bg-emerald-500/10 px-3 py-1 rounded-sm border border-emerald-500/20">1.50x</div>
                  </div>
                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Max Capital Allocation</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Percentage of total inventory authorized for deployment</div>
                    </div>
                    <div className="text-sm font-mono text-blue-400 bg-blue-500/10 px-3 py-1 rounded-sm border border-blue-500/20">80.0%</div>
                  </div>
                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Rebalance Threshold Delta</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Imbalance trigger required to automatically reposition liquidity</div>
                    </div>
                    <div className="text-sm font-mono text-yellow-400 bg-yellow-500/10 px-3 py-1 rounded-sm border border-yellow-500/20">5.0%</div>
                  </div>
                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Max Execution Slippage</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Hard cap on cross-pool slippage tolerance</div>
                    </div>
                    <div className="text-sm font-mono text-red-400 bg-red-500/10 px-3 py-1 rounded-sm border border-red-500/20">1.0%</div>

                  </div>
                </>
              )}
              {venue.name === 'quidax' && (
                <>

                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Order Ladder Tiers</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Number of active orders distributed around mid-price</div>
                    </div>
                    <div className="text-sm font-mono text-emerald-400 bg-emerald-500/10 px-3 py-1 rounded-sm border border-emerald-500/20">10</div>
                  </div>
                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Execution Tick Increment</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Price step between ladder tiers</div>
                    </div>
                    <div className="text-sm font-mono text-white/80 bg-white/5 px-3 py-1 rounded-sm border border-white/10">0.000001</div>
                  </div>
                  <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                    <div>
                      <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">Liquidity Density (per Lvl)</div>
                      <div className="text-[10px] font-mono text-white/50 mt-1">Capital assigned to each generated order tier</div>
                    </div>
                    <div className="text-sm font-mono text-blue-400 bg-blue-500/10 px-3 py-1 rounded-sm border border-blue-500/20">5.0%</div>

                  </div>
                </>
              )}
              {venue.name === 'blockradar' && (

                <div className="p-3.5 flex items-center justify-between hover:bg-white/[0.01] transition-colors">
                  <div>
                    <div className="text-[11px] font-mono text-white/90 uppercase tracking-widest">System Operation Spread</div>
                    <div className="text-[10px] font-mono text-white/50 mt-1">Base buffer for internal liquidity operations</div>
                  </div>
                  <div className="text-sm font-mono text-purple-400 bg-purple-500/10 px-3 py-1 rounded-sm border border-purple-500/20">15 BPS</div>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>

  );
}

export default function VenuesPage() {
  const { data: status, isLoading } = useStatus();
  const [selectedVenue, setSelectedVenue] = useState<string | null>(null);

  const venues = status?.venues || [];
  const isSyncing = isLoading;

  const displayedVenue = selectedVenue
    ? venues.find((v) => v.name === selectedVenue)
    : venues[0];

  return (
    <div className="flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 p-2 md:p-4 animate-in fade-in duration-500 font-sans">
      {/* Top Status Bar */}
      <div className="flex items-center justify-between border-b border-white/[0.05] pb-3 mb-4">
        <div className="flex items-center gap-3">
          <Zap className={`h-4 w-4 ${isSyncing ? 'text-emerald-500/30' : 'text-emerald-500'}`} />
          <h1 className="text-xs font-bold tracking-widest uppercase text-white">Venues</h1>
        </div>
        <div className="flex items-center gap-3">
          {isSyncing ? (
            <div className="flex items-center gap-2 bg-yellow-500/10 border border-yellow-500/20 px-3 py-1.5 rounded-sm text-[11px] uppercase tracking-widest font-mono text-yellow-500">
              <div className="h-2 w-2 border-t-2 border-yellow-500 rounded-full animate-spin" />
              <span>Syncing......</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 bg-white/[0.02] border border-white/5 px-3 py-1.5 rounded-sm text-[11px] uppercase tracking-widest font-mono text-white/70">
              <span className="flex h-2 w-2 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span className="text-emerald-400">Active</span>
            </div>
          )}
        </div>
      </div>

      <div className="w-full">
        {/* Venue tabs */}
        <div className="flex flex-wrap gap-2 mb-4">
          {isSyncing && venues.length === 0 ? (
            Array.from({ length: 4 }).map((_, i) => (
              <div key={`tab-skel-${i}`} className="flex items-center gap-2 px-3 py-2 rounded-sm bg-black/20 border border-white/[0.02] animate-pulse">
                <div className="h-1.5 w-1.5 rounded-full bg-white/10" />
                <div className="h-2 w-16 bg-white/10 rounded-sm" />
              </div>
            ))
          ) : (
            venues.map((venue) => {
              const info = venueInfo[venue.name];
              const isActive = venue.enabled && !venue.paused;
              const isSelected = (selectedVenue || venues[0]?.name) === venue.name;

              return (
                <button
                  key={venue.name}
                  onClick={() => setSelectedVenue(venue.name)}
                  className={`flex items-center gap-2 px-3 py-2 rounded-sm text-[11px] uppercase tracking-widest font-mono transition-colors border ${isSelected
                    ? 'bg-white/[0.05] border-white/10 text-white'
                    : 'bg-black/20 border-white/[0.02] text-white/50 hover:text-white/80 hover:bg-white/[0.04]'
                    }`}
                >
                  <div className={`h-1.5 w-1.5 rounded-full ${isActive ? 'bg-emerald-500 shadow-[0_0_5px_rgba(16,185,129,0.5)]' : 'bg-yellow-500 shadow-[0_0_5px_rgba(234,179,8,0.5)]'}`} />
                  {info?.name || venue.name}
                </button>
              );
            })
          )}
        </div>

        {/* Selected venue detail */}
        {displayedVenue ? (
          <VenueDetail venue={displayedVenue} isSyncing={isSyncing} />
        ) : isSyncing ? (
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 animate-in fade-in duration-500">
            {/* LEFT COLUMN: SKELETON */}
            <div className="lg:col-span-1 space-y-4">
              <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
                <CardHeader className="p-3 border-b border-white/[0.02] flex flex-row items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="h-4 w-4 bg-white/10 rounded-sm animate-pulse" />
                    <div className="h-3 w-24 bg-white/10 rounded-sm animate-pulse" />
                  </div>
                  <div className="h-1.5 w-1.5 rounded-full bg-white/10 animate-pulse" />
                </CardHeader>
                <CardContent className="p-4 space-y-4">
                  <div>
                    <div className="h-6 w-32 bg-white/10 rounded-sm animate-pulse mb-3" />
                    <div className="space-y-2 mb-4">
                      <div className="h-2 w-full bg-white/5 rounded-sm animate-pulse" />
                      <div className="h-2 w-4/5 bg-white/5 rounded-sm animate-pulse" />
                    </div>
                    <div className="flex gap-2">
                      <div className="h-5 w-16 bg-blue-500/20 rounded-sm animate-pulse" />
                      <div className="h-5 w-16 bg-purple-500/20 rounded-sm animate-pulse" />
                    </div>
                  </div>
                  <div className="h-px w-full bg-white/[0.05]"></div>
                  <div className="flex flex-col gap-2">
                    <div className="h-9 w-full bg-white/5 rounded-sm animate-pulse" />
                    <div className="h-9 w-full bg-white/5 rounded-sm animate-pulse" />
                  </div>
                </CardContent>
              </Card>

              <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
                <CardHeader className="p-3 border-b border-white/[0.02]">
                  <div className="flex items-center gap-2">
                    <div className="h-4 w-4 bg-white/10 rounded-sm animate-pulse" />
                    <div className="h-3 w-24 bg-white/10 rounded-sm animate-pulse" />
                  </div>
                </CardHeader>
                <CardContent className="p-3 space-y-3">
                  <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02]">
                    <div className="h-3 w-24 bg-white/5 rounded-sm animate-pulse" />
                    <div className="h-3 w-8 bg-emerald-400/20 rounded-sm animate-pulse" />
                  </div>
                  <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02]">
                    <div className="h-3 w-20 bg-white/5 rounded-sm animate-pulse" />
                    <div className="h-3 w-12 bg-white/10 rounded-sm animate-pulse" />
                  </div>
                  <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02]">
                    <div className="h-3 w-28 bg-white/5 rounded-sm animate-pulse" />
                    <div className="h-3 w-16 bg-blue-400/20 rounded-sm animate-pulse" />
                  </div>
                </CardContent>
              </Card>
            </div>

            {/* RIGHT COLUMNS: SKELETON */}
            <div className="lg:col-span-3 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
                  <CardHeader className="p-3 border-b border-white/[0.05] flex flex-row items-center justify-between">
                    <div className="flex items-center gap-2">
                      <div className="h-4 w-4 bg-white/10 rounded-sm animate-pulse" />
                      <div className="h-3 w-32 bg-white/10 rounded-sm animate-pulse" />
                    </div>
                    <div className="h-1.5 w-1.5 rounded-full bg-white/20 animate-pulse" />
                  </CardHeader>
                  <CardContent className="p-4">
                    <div className="grid grid-cols-2 gap-4">
                      <div className="bg-black/40 border border-white/[0.02] rounded-sm p-3.5">
                        <div className="flex justify-between items-start mb-2">
                          <div className="h-3 w-12 bg-emerald-500/20 rounded-sm animate-pulse" />
                          <div className="h-4 w-16 bg-white/10 rounded-sm animate-pulse" />
                        </div>
                        <div className="flex justify-end border-t border-white/[0.05] pt-2 mt-2">
                          <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                        </div>
                      </div>
                      <div className="bg-black/40 border border-white/[0.02] rounded-sm p-3.5">
                        <div className="flex justify-between items-start mb-2">
                          <div className="h-3 w-12 bg-blue-500/20 rounded-sm animate-pulse" />
                          <div className="h-4 w-16 bg-white/10 rounded-sm animate-pulse" />
                        </div>
                        <div className="flex justify-end border-t border-white/[0.05] pt-2 mt-2">
                          <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>

                <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
                  <CardHeader className="p-3 border-b border-white/[0.05] flex flex-row items-center justify-between">
                    <div className="flex items-center gap-2">
                      <div className="h-4 w-4 bg-white/10 rounded-sm animate-pulse" />
                      <div className="h-3 w-32 bg-white/10 rounded-sm animate-pulse" />
                    </div>
                    <div className="h-5 w-16 bg-white/5 rounded-sm animate-pulse" />
                  </CardHeader>
                  <CardContent className="p-4 space-y-4">
                    <div className="flex justify-between items-center bg-black/40 p-2.5 rounded-sm border border-white/[0.02]">
                      <div className="h-3 w-32 bg-white/5 rounded-sm animate-pulse" />
                      <div className="h-4 w-12 bg-white/10 rounded-sm animate-pulse" />
                    </div>
                    <div className="bg-black/40 p-3.5 rounded-sm border border-white/[0.02] space-y-4">
                      <div className="flex justify-between items-end">
                        <div className="h-3 w-36 bg-white/5 rounded-sm animate-pulse" />
                        <div className="h-5 w-24 bg-emerald-400/20 rounded-sm animate-pulse" />
                      </div>
                      <div className="space-y-1.5 mt-3">
                        <div className="flex justify-between">
                          <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                          <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                        </div>
                        <div className="h-2 w-full bg-white/5 rounded-full animate-pulse" />
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </div>

              <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
                <CardHeader className="p-3 border-b border-white/[0.02] flex flex-row items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="h-4 w-4 bg-white/10 rounded-sm animate-pulse" />
                    <div className="h-3 w-48 bg-white/10 rounded-sm animate-pulse" />
                  </div>
                  <div className="h-6 w-24 bg-white/5 rounded-sm animate-pulse" />
                </CardHeader>
                <CardContent className="p-0">
                  <div className="divide-y divide-white/[0.02]">
                    {Array.from({ length: 4 }).map((_, i) => (
                      <div key={i} className="p-3.5 flex items-center justify-between">
                        <div className="flex flex-col gap-2 w-1/2">
                          <div className="h-3 w-48 bg-white/10 rounded-sm animate-pulse" />
                          <div className="h-2 w-64 bg-white/5 rounded-sm animate-pulse" />
                        </div>
                        <div className="h-6 w-16 bg-emerald-500/20 rounded-sm animate-pulse" />
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            </div>
          </div>
        ) : (
          <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none">
            <CardContent className="py-12 flex flex-col items-center justify-center text-center">
              <Database className="h-8 w-8 text-white/10 mb-3" />
              <div className="text-[12px] font-mono text-white/40 uppercase tracking-widest">NO ASSET VENUES CONFIGURED</div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
