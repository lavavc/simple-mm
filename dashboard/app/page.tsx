'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { formatNumber, formatCurrency, formatUptime } from '@/lib/utils';
import { useStatus, useGlobalPosition, useBlendedPrice } from '@/lib/hooks/useQueries';
import { VenuePriceChart } from '@/components/charts/VenuePriceChart';
import { Activity, Zap, Wallet, AlertTriangle, ArrowRight, TrendingUp, Cpu } from 'lucide-react';

interface CurvePoint {
  size: number;
  cngn_uni_bsc: number;
  cngn_uni_base: number;
  profit: number;
  min_acceptable_usd: number;
}

interface DexArbData {
  timestamp: number;
  prices: {
    'uni-bsc': number;
    'uni-base': number;
  };
  stats: {
    uni_bsc_liquidity_cngn_raw: string;
    uni_base_liquidity_cngn_raw: string;
  };
  curve: CurvePoint[];
  optimal_arb: {
    direction: string;
    optimal_size_usd: number;
    expected_profit_usd: number;
    cngn_transferred: number;
    expected_usd_out: number;
    net_spread_bps: number;
    slippage_tolerance_bps?: number;
    uni_bsc_fee_bps?: number;
    uni_base_fee_bps?: number;
    gas_usd?: number;
  };
}

export default function DashboardPage() {
  const { data: status, isLoading: statusLoading } = useStatus();
  const { data: globalPosition, isLoading: positionLoading } = useGlobalPosition();
  const { data: blendedPrice, isLoading: blendedLoading } = useBlendedPrice();

  const { data: curveData } = useQuery<DexArbData | null>({
    queryKey: ['dex_arb_curve'],
    queryFn: () => null,
    staleTime: Infinity,
  });

  const [now, setNow] = React.useState(Date.now());
  React.useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(interval);
  }, []);

  const isSyncing = statusLoading || positionLoading || blendedLoading || !curveData;
  const timeSinceLastPacket = curveData?.timestamp ? Math.max(0, (now - curveData.timestamp) / 1000).toFixed(1) : "0.0";

  const resolvedCurveData = curveData || {
    timestamp: 0,
    prices: { 'uni-bsc': 0, 'uni-base': 0 },
    stats: { uni_bsc_liquidity_cngn_raw: "0", uni_base_liquidity_cngn_raw: "0" },
    curve: [],
    optimal_arb: {
      direction: "_____",
      optimal_size_usd: 0,
      expected_profit_usd: 0,
      cngn_transferred: 0,
      expected_usd_out: 0,
      net_spread_bps: 0
    }
  };

  return (
    <div className="flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 p-2 md:p-4 animate-in fade-in duration-500 font-sans">
      {/* Top Status Bar */}
      <div className="flex items-center justify-between border-b border-white/[0.05] pb-3 mb-4">
        <div className="flex items-center gap-3">
          <Activity className={`h-4 w-4 ${isSyncing ? 'text-emerald-500/30' : 'text-emerald-500'}`} />
          <h1 className="text-xs font-bold tracking-widest uppercase text-white">Main Dashboard <span className="text-white/40 font-mono ml-2 normal-case tracking-normal">System overview and active operations</span></h1>
        </div>
        <div className="flex items-center gap-3">
          {!isSyncing && curveData?.timestamp && (
            <div className="text-[11px] font-mono text-white/50 tracking-widest uppercase mr-2 flex flex-col items-end">
              <span className="text-[9px] text-white/40 mb-0.5">LAST PACKET</span>
              <span className={parseFloat(timeSinceLastPacket) > 5 ? "text-yellow-500/90" : "text-white/90"}>{timeSinceLastPacket}s ago</span>
            </div>
          )}
          {isSyncing ? (
            <div className="flex items-center gap-2 bg-yellow-500/10 border border-yellow-500/20 px-3 py-1.5 rounded-sm text-[11px] uppercase tracking-widest font-mono text-yellow-500/90">
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

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 mb-4">
        {/* System Status */}
        <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none relative overflow-hidden flex flex-col">
          <div className={`absolute -right-10 -top-10 w-32 h-32 rounded-full blur-3xl opacity-10 transition-colors duration-1000 ${status?.trading_enabled ? 'bg-emerald-500' : 'bg-yellow-500'}`} />
          <CardHeader className="p-3 border-b border-white/[0.02] z-10">
            <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center justify-between">
              <span className="flex items-center gap-2"><Cpu className={`h-4 w-4 ${status?.trading_enabled ? 'text-emerald-500' : 'text-yellow-500'}`} /> System Health</span>
              <div className="flex items-center gap-1.5 bg-black/40 px-2 py-0.5 rounded-sm border border-white/5">
                <div className={`h-1.5 w-1.5 rounded-full ${status?.trading_enabled ? 'bg-emerald-500 shadow-[0_0_5px_rgba(16,185,129,0.5)]' : 'bg-yellow-500 animate-pulse'}`} />
                <span className="text-[9px] font-mono tracking-widest text-white/50">{status?.trading_enabled ? 'ONLINE' : 'PAUSED'}</span>
              </div>
            </div>
          </CardHeader>
          <CardContent className={`p-4 flex-1 flex flex-col justify-between z-10 ${isSyncing ? 'opacity-30' : ''}`}>
            {isSyncing ? (
              <div className="space-y-4 pt-1">
                <div className="flex items-center gap-3">
                  <div className="h-8 w-8 bg-white/10 rounded-sm animate-pulse" />
                  <div className="h-5 w-24 bg-white/5 rounded-sm animate-pulse" />
                </div>
                <div className="flex justify-between border-t border-white/[0.05] pt-3">
                  <div className="h-3 w-16 bg-white/5 rounded-sm animate-pulse" />
                  <div className="h-3 w-16 bg-white/5 rounded-sm animate-pulse" />
                </div>
              </div>
            ) : (
              <>
                <div className="flex items-center gap-3 mb-4">
                  <div className={`p-2.5 rounded-sm border ${status?.trading_enabled ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-500' : 'bg-yellow-500/10 border-yellow-500/20 text-yellow-500'}`}>
                    <Activity className="h-5 w-5" />
                  </div>
                  <div>
                    <div className="text-xl font-bold font-mono tracking-tight text-white leading-tight">
                      {status?.trading_enabled ? 'ARMED' : 'PAUSED'}
                    </div>
                    <div className="text-[9px] text-white/40 font-mono tracking-widest uppercase mt-0.5">
                      CORE ENGINE STATUS
                    </div>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 text-[10px] font-mono border-t border-white/[0.05] pt-3 mt-auto">
                  <div className="flex flex-col">
                    <span className="text-white/30 uppercase tracking-widest mb-1">UPTIME</span>
                    <span className="text-white/80">{formatUptime(status?.uptime || 0)}</span>
                  </div>
                  <div className="flex flex-col text-right">
                    <span className="text-white/30 uppercase tracking-widest mb-1">VENUES ONLINE</span>
                    <span className="text-white/80">{status?.venues?.filter((v: any) => v.enabled && !v.paused).length || 0} <span className="text-white/40">/ {status?.venues?.length || 0}</span></span>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>


        {/* Global Portfolio */}
        <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none flex flex-col">
          <CardHeader className="p-3 border-b border-white/[0.02]">
            <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center justify-between">
              <span className="flex items-center gap-2"><Wallet className="h-4 w-4" /> Global Portfolio</span>
              {isSyncing && <div className="h-1.5 w-1.5 bg-white/20 rounded-full animate-ping" />}
            </div>
          </CardHeader>
          <CardContent className={`p-4 flex-1 flex flex-col justify-between ${isSyncing ? 'opacity-30' : ''}`}>
            {isSyncing ? (
              <div className="space-y-4 pt-1">
                <div className="flex items-center gap-3">
                  <div className="h-8 w-8 bg-white/10 rounded-sm animate-pulse" />
                  <div className="h-5 w-24 bg-white/5 rounded-sm animate-pulse" />
                </div>
                <div className="flex justify-between border-t border-white/[0.05] pt-3">
                  <div className="h-3 w-16 bg-white/5 rounded-sm animate-pulse" />
                  <div className="h-3 w-16 bg-white/5 rounded-sm animate-pulse" />
                </div>
              </div>
            ) : (() => {
              const stableUsd = Number(globalPosition?.total_usdc ?? 0) + Number(globalPosition?.total_usdt ?? 0);
              const total = Number(globalPosition?.total_usd_value ?? 0);
              const cngnPct = Number(globalPosition?.delta_ratio ?? 0) * 100;
              const stablePct = 100 - cngnPct;
              return (
                <>
                  <div className="flex items-center gap-3 mb-4">
                    <div className="p-2.5 rounded-sm bg-emerald-500/10 border border-emerald-500/20 text-emerald-500">
                      <Wallet className="h-5 w-5" />
                    </div>
                    <div>
                      <div className="text-xl font-bold font-mono tracking-tight text-white leading-tight">
                        {formatCurrency(total)}
                      </div>
                      <div className="text-[9px] text-white/40 font-mono tracking-widest uppercase mt-0.5">
                        TOTAL VALUE
                      </div>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-[10px] font-mono border-t border-white/[0.05] pt-3 mt-auto">
                    <div className="flex flex-col">
                      <span className="text-white/30 uppercase tracking-widest mb-1">cNGN</span>
                      <span className="text-emerald-400">{formatNumber(globalPosition?.total_cngn || 0, 0)}</span>
                      <span className="text-white/30 mt-0.5">{cngnPct.toFixed(1)}%</span>
                    </div>
                    <div className="flex flex-col text-right">
                      <span className="text-white/30 uppercase tracking-widest mb-1">USDC/USDT</span>
                      <span className="text-blue-400">{formatNumber(stableUsd, 2)}</span>
                      <span className="text-white/30 mt-0.5">{stablePct.toFixed(1)}%</span>
                    </div>
                  </div>
                </>
              );
            })()}
          </CardContent>
        </Card>

        {/* Blended Oracle Price */}
        <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none relative overflow-hidden flex flex-col">
          <div className="absolute -left-10 -top-10 w-32 h-32 rounded-full blur-3xl opacity-10 bg-blue-400 pointer-events-none" />
          <CardHeader className="p-3 border-b border-white/[0.02] z-10">
            <div className="text-[11px] text-white/50 uppercase tracking-widest font-bold flex items-center justify-between">
              <span className="flex items-center gap-2"><TrendingUp className="h-4 w-4 text-blue-400" /> Blended Oracle</span>
              <div className="flex items-center gap-1.5 bg-black/40 px-2 py-0.5 rounded-sm border border-white/5">
                <div className="h-1.5 w-1.5 rounded-full bg-blue-400 shadow-[0_0_5px_rgba(96,165,250,0.6)] animate-pulse" />
                <span className="text-[9px] font-mono tracking-widest text-white/50">LIVE</span>
              </div>
            </div>
          </CardHeader>
          <CardContent className={`p-4 flex-1 flex flex-col justify-between z-10 ${isSyncing ? 'opacity-30' : ''}`}>
            {isSyncing ? (
              <div className="space-y-4 pt-1">
                <div className="flex items-center gap-3">
                  <div className="h-8 w-8 bg-white/10 rounded-sm animate-pulse" />
                  <div className="h-5 w-24 bg-white/5 rounded-sm animate-pulse" />
                </div>
                <div className="flex justify-between border-t border-white/[0.05] pt-3">
                  <div className="h-3 w-16 bg-white/5 rounded-sm animate-pulse" />
                  <div className="h-3 w-16 bg-white/5 rounded-sm animate-pulse" />
                </div>
              </div>
            ) : (
              <>
                <div className="flex items-center gap-3 mb-4">
                  <div className="p-2.5 rounded-sm border bg-blue-500/10 border-blue-500/20 text-blue-400">
                    <TrendingUp className="h-5 w-5" />
                  </div>
                  <div>
                    <div className="text-xl font-bold font-mono tracking-tight text-white leading-tight">
                      ${Number(blendedPrice?.vwap || 0).toFixed(6)}
                    </div>
                    <div className="text-lg font-bold font-mono tracking-tight text-blue-300 leading-tight">
                      ₦{formatNumber(blendedPrice?.vwap ? 1 / blendedPrice.vwap : 0, 2)}
                    </div>
                    <div className="text-[9px] text-white/40 font-mono tracking-widest uppercase mt-0.5">
                      cNGN/USD
                    </div>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 text-[10px] font-mono border-t border-white/[0.05] pt-3 mt-auto">
                  <div className="flex flex-col">
                    <span className="text-white/30 uppercase tracking-widest mb-1">TWAP 5m</span>
                    <span className="text-white/80">₦{formatNumber(blendedPrice?.twap_5m ? 1 / blendedPrice.twap_5m : 0, 2)}</span>
                  </div>
                  <div className="flex flex-col text-right">
                    <span className="text-white/30 uppercase tracking-widest mb-1">SOURCES</span>
                    <span className="text-white/80">
                      <span className={`${(blendedPrice?.confidence ?? 0) >= 0.8 ? 'text-emerald-400' : 'text-yellow-500'}`}>
                        {Math.round((blendedPrice?.confidence ?? 0) * 100)}% conf
                      </span>
                      <span className="text-white/30 mx-1">·</span>
                      {blendedPrice?.num_sources ?? 0} of {blendedPrice?.total_venues ?? 0}
                    </span>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>

        {/* Dex Arb Active Component */}
        <Card className={`bg-[#12161C] border rounded-sm shadow-none transition-colors duration-500 ${resolvedCurveData.optimal_arb.expected_profit_usd > 0 ? 'border-emerald-500/30' : 'border-white/[0.05]'}`}>
          <CardHeader className={`p-3 border-b ${resolvedCurveData.optimal_arb.expected_profit_usd > 0 ? 'border-emerald-500/10 bg-emerald-500/[0.02]' : 'border-white/[0.02]'}`}>
            <div className="text-[11px] text-white/60 uppercase tracking-widest font-bold flex items-center gap-2">
              <Zap className={`h-3 w-3 ${resolvedCurveData.optimal_arb.expected_profit_usd > 0 ? 'text-emerald-400' : 'text-white/40'}`} />
              TARGET ENGINE
            </div>
          </CardHeader>
          <CardContent className="p-4">
            {isSyncing ? (
              <div className="py-2 flex flex-col items-center text-center space-y-3">
                <div className="h-5 w-5 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin" />
                <div className="text-[11px] text-emerald-500/70 uppercase tracking-widest font-mono animate-pulse">Establishing Connection...</div>
              </div>
            ) : resolvedCurveData.optimal_arb.expected_profit_usd <= 0 ? (
              <div className="py-2 flex flex-col items-center text-center space-y-3">
                <AlertTriangle className="h-6 w-6 text-yellow-500/70" />
                <div className="text-[11px] text-yellow-500/70 uppercase tracking-widest font-mono">Awaiting Spreads</div>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="flex items-center justify-between bg-black/40 border border-white/10 p-2 rounded-sm">
                  <span className="text-[9px] font-mono text-white/80">{resolvedCurveData.optimal_arb.direction.split('_TO_')[0].replace('_', '-')}</span>
                  <ArrowRight className="h-3 w-3 text-emerald-500/70" />
                  <span className="text-[9px] font-mono text-white/80">{resolvedCurveData.optimal_arb.direction.split('_TO_')[1]?.replace('_DELTA_BALANCE', '').replace('_', '-') ?? ''}</span>
                </div>
                <div className="flex justify-between items-center text-[10px] font-mono">
                  <span className="text-white/50">Opt Size</span>
                  <span className="text-white">${formatNumber(resolvedCurveData.optimal_arb.optimal_size_usd, 0)}</span>
                </div>
                <div className="flex justify-between items-center text-[10px] font-mono pt-1 border-t border-white/[0.05]">
                  <span className="text-white/50">Net Profit</span>
                  <span className="text-emerald-400 font-bold">+${formatNumber(resolvedCurveData.optimal_arb.expected_profit_usd, 2)}</span>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Price Chart */}
      <div className="mt-4">
        <VenuePriceChart blended={blendedPrice} />

      </div>
    </div>
  );
}
