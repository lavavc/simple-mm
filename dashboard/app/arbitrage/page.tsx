'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { formatCurrency, formatBps, formatRelativeTime, formatNumber } from '@/lib/utils';
import { useArbitrageStatus, useOpportunities, useTriggerScan } from '@/lib/hooks/useQueries';
import {
  RefreshCw,
  Power,
  Play,
  AlertTriangle,
  ArrowRight,
  RotateCcw,
  Activity,
  Cpu,
  Zap,
  Crosshair,
  Check,
  Database,
  BarChart3,
  LineChart
} from 'lucide-react';
import React, { useState } from 'react';
import DexArbPage from './dex-arb-tab';

export default function ArbitragePage() {
  const [activeTab, setActiveTab] = useState<'spot' | 'dex'>('spot');

  return (
    <div className="flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 animate-in fade-in duration-500 font-sans overflow-hidden relative">
      {/* Precision Grid Background */}
      <div className="absolute inset-0 pointer-events-none opacity-[0.04] bg-[linear-gradient(rgba(16,185,129,0.3)_1px,transparent_1px),linear-gradient(90deg,rgba(16,185,129,0.3)_1px,transparent_1px)] bg-[size:60px_60px] [mask-image:radial-gradient(ellipse_at_center,black_10%,transparent_80%)] z-0" />

      {/* Master Tabs Controller */}
      <div className="relative z-20 px-2 md:px-6 pt-6 pb-2 border-b border-white/[0.05] bg-black/20 flex flex-wrap gap-3 shadow-[0_10px_30px_rgba(0,0,0,0.5)]">
        <button
          onClick={() => setActiveTab('spot')}
          className={`group flex items-center gap-2 px-6 py-2.5 rounded-sm text-[11px] font-mono uppercase tracking-widest transition-all ${activeTab === 'spot'
            ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 shadow-[0_0_15px_rgba(16,185,129,0.1)]'
            : 'bg-white/[0.02] text-white/50 border border-white/5 hover:bg-white/[0.05] hover:text-white/80'
            }`}
        >
          <BarChart3 className={`h-3.5 w-3.5 transition-colors ${activeTab === 'spot' ? 'text-emerald-500' : 'text-white/30 group-hover:text-white/50'}`} />
          Spot / Orderbook
        </button>
        <button
          onClick={() => setActiveTab('dex')}
          className={`group flex items-center gap-2 px-6 py-2.5 rounded-sm text-[11px] font-mono uppercase tracking-widest transition-all ${activeTab === 'dex'
            ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 shadow-[0_0_15px_rgba(16,185,129,0.1)]'
            : 'bg-white/[0.02] text-white/50 border border-white/5 hover:bg-white/[0.05] hover:text-white/80'
            }`}
        >
          <LineChart className={`h-3.5 w-3.5 transition-colors ${activeTab === 'dex' ? 'text-emerald-500' : 'text-white/30 group-hover:text-white/50'}`} />
          AMM Curves
        </button>
      </div>

      {/* RENDER ACTIVE TAB */}
      <div className="relative z-10 flex-1 flex flex-col overflow-y-auto w-full">
        {activeTab === 'spot' ? <SpotArbitrageTab /> : (
          <div className="w-full flex-1 [&>div]:min-h-0 [&>div]:bg-transparent">
            <DexArbPage />
          </div>
        )}
      </div>
    </div>
  );
}

function SpotArbitrageTab() {
  const { data: status, isLoading: statusLoading } = useArbitrageStatus();
  const { data: opportunities, isLoading: oppsLoading } = useOpportunities(50);
  const triggerScan = useTriggerScan();

  const token = process.env.NEXT_PUBLIC_API_TOKEN || '';

  const handleScan = () => {
    triggerScan.mutate(token);
  };

  return (
    <div className="p-2 md:p-6 font-sans space-y-6 flex-1 flex flex-col animate-in fade-in duration-300 w-full">
      {/* Top Status Bar */}
      <div className="flex flex-col md:flex-row md:items-center justify-between border-b border-white/[0.05] pb-4 z-10 gap-4 md:gap-0">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-emerald-500/10 rounded-sm border border-emerald-500/20">
            <Cpu className="h-5 w-5 text-emerald-500" />
          </div>
          <div>
            <h1 className="text-xs font-bold tracking-widest uppercase text-white drop-shadow-[0_0_10px_rgba(255,255,255,0.3)]">Spot Arbitrage Engine</h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {statusLoading ? (
            <div className="flex items-center gap-2 bg-white/5 border border-white/10 px-4 py-2 rounded-sm text-[11px] uppercase tracking-widest font-mono text-white/60">
              <div className="h-3 w-3 border-t-2 border-white/50 rounded-full animate-spin" />
              <span>INITIALIZING...</span>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-4 mr-4 text-[11px] font-mono tracking-widest uppercase border-r border-white/10 pr-6">
                <div className="flex flex-col">
                  <span className="text-white/50">CIRCUIT BREAKER</span>
                  {status?.circuit_breaker_active ? (
                    <span className="text-red-500 font-bold flex items-center gap-1 mt-0.5"><AlertTriangle className="h-3 w-3" /> ACTIVE ({status?.consecutive_failures || 0} FAILURE)</span>
                  ) : (
                    <span className="text-emerald-500 font-bold flex items-center gap-1 mt-0.5"><Check className="h-3 w-3" /> INACTIVE</span>
                  )}
                </div>
              </div>

              <button
                onClick={handleScan}
                disabled={triggerScan.isPending}
                className="group relative flex items-center gap-2 px-6 py-2 bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 rounded-sm text-[11px] font-mono uppercase tracking-widest text-emerald-400 transition-all overflow-hidden"
              >
                <div className="absolute inset-0 w-full h-full bg-gradient-to-r from-transparent via-white/10 to-transparent -translate-x-full group-hover:animate-[shimmer_1.5s_infinite]" />
                {triggerScan.isPending ? (
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Crosshair className="h-3.5 w-3.5" />
                )}
                MANUAL SCAN
              </button>

            </>
          )}
        </div>
      </div>

      {/* Cyberpunk HUD Grid */}
      <div className="grid grid-cols-1 md:grid-cols-12 gap-6 z-10 w-full h-full">

        {/* Left Column: Diagnostics & Params */}
        <div className="md:col-span-3 flex flex-col gap-6 w-full">

          <Card className="relative bg-[#12161C] border border-white/[0.05] shadow-none rounded-sm">
            <CardHeader className="pb-4 border-b border-white/[0.05] p-4">
              <div className="flex items-center justify-between">
                <CardTitle className="text-[11px] font-mono font-bold tracking-widest uppercase text-white/70">Execution Stats</CardTitle>
                <Activity className="h-4 w-4 text-white/40" />
              </div>
            </CardHeader>
            <CardContent className="pt-4 space-y-4 p-4">
              <div className="flex justify-between items-end border-b border-white/[0.02] pb-3">
                <div className="flex flex-col">
                  <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 mb-1">PROFIT 24H</span>
                  <span className="text-xl font-mono font-bold text-emerald-500">{formatCurrency(status?.total_profit_24h_usd || 0)}</span>
                </div>
              </div>
              <div className="flex justify-between items-end border-b border-white/[0.02] pb-3">
                <div className="flex flex-col">
                  <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 mb-1">VOLUME 24H</span>
                  <span className="text-sm font-mono font-bold text-white/90">{formatCurrency(status?.daily_volume_usd || 0)}</span>
                </div>
              </div>
              <div className="flex justify-between items-end border-b border-white/[0.02] pb-3">
                <div className="flex flex-col">
                  <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 mb-1">IMBALANCE</span>
                  <span className="text-sm font-mono font-bold text-yellow-500">{formatCurrency(status?.inventory_imbalance_usd || 0)}</span>
                </div>
              </div>
              <div className="flex justify-between items-end pt-1">
                <div className="flex flex-col">
                  <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 mb-1">EVENTS</span>
                  <span className="text-xs font-mono font-bold text-white/70">{status?.opportunities_executed_24h || 0} EXECUTED / {status?.opportunities_detected_24h || 0} DETECTED</span>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="relative bg-[#12161C] border border-white/[0.05] shadow-none rounded-sm">
            <CardHeader className="pb-4 border-b border-white/[0.05] p-4">
              <CardTitle className="text-[11px] font-mono font-bold tracking-widest uppercase text-white/70">Hardware Config</CardTitle>
            </CardHeader>
            <CardContent className="pt-4 grid grid-cols-2 gap-x-2 gap-y-4 p-4">
              <div>
                <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 block mb-0.5">SCAN FREQUENCY</span>
                <span className="text-[12px] font-mono font-bold text-white/90">{status?.params.scan_interval_seconds || 30} SECONDS</span>
              </div>
              <div>
                <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 block mb-0.5">MIN NET PROFIT</span>
                <span className="text-[12px] font-mono font-bold text-white/90">{status?.params.min_net_profit_bps} BPS</span>
              </div>
              <div>
                <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 block mb-0.5">MAX TICKET</span>
                <span className="text-[12px] font-mono font-bold text-white/90">{formatCurrency(status?.params.max_single_trade_usd || 0)}</span>
              </div>
              <div>
                <span className="text-[10px] font-mono tracking-widest uppercase text-white/50 block mb-0.5">MIN PROFIT</span>
                <span className="text-[12px] font-mono font-bold text-white/90">{status?.params.min_net_profit_bps} BPS</span>
              </div>
            </CardContent>
          </Card>

        </div>

        {/* Right Column: Dynamic Targets Matrix */}
        <div className="md:col-span-9 flex flex-col h-full w-full">
          <Card className="relative bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none flex-1 flex flex-col w-full h-full">
            <CardHeader className="p-4 border-b border-white/[0.05] shrink-0">
              <div className="flex items-center justify-between">
                <div className="text-[11px] text-white/60 font-bold uppercase tracking-widest font-mono flex items-center gap-2">
                  <Database className="h-3.5 w-3.5" />
                  Execution Ledger
                  {oppsLoading && <div className="h-3 w-3 border border-white/40 rounded-full border-t-white animate-spin ml-2" />}
                </div>
                <div className="flex items-center gap-2 bg-black/40 px-3 py-1.5 rounded-sm border border-white/5">
                  <div className={`h-1.5 w-1.5 rounded-full ${oppsLoading ? 'bg-yellow-500 animate-pulse' : 'bg-emerald-500'}`} />
                  <span className="text-[10px] font-mono tracking-widest uppercase text-white/50">{oppsLoading ? 'CALCULATING' : 'LIVE'}</span>
                </div>
              </div>
            </CardHeader>
            <CardContent className="pt-0 p-0 flex-1 overflow-hidden flex flex-col min-h-[300px]">
              {oppsLoading ? (
                <div className="flex-1 flex flex-col justify-center items-center py-12">
                  <div className="relative w-24 h-24 flex items-center justify-center">
                    <div className="absolute inset-0 border-t-2 border-emerald-500/20 rounded-full animate-spin [animation-duration:3s]" />
                    <div className="absolute inset-2 border-b-2 border-emerald-500/40 rounded-full animate-spin [animation-duration:2s] [animation-direction:reverse]" />
                    <div className="absolute inset-4 border-l-2 border-emerald-500/80 rounded-full animate-spin [animation-duration:1s]" />
                    <Crosshair className="h-4 w-4 text-emerald-500 animate-pulse" />
                  </div>
                  <span className="text-[11px] font-mono tracking-widest uppercase text-emerald-500/50 mt-6 animate-pulse">ACQUIRING TARGETS...</span>
                </div>
              ) : opportunities && opportunities.length > 0 ? (
                <div className="overflow-x-auto flex-1 h-full w-full relative">
                  <table className="w-full text-left font-mono">
                    <thead className="sticky top-0 bg-[#0B0E14]/90 backdrop-blur-md z-10 w-full">
                      <tr className="border-b border-white/[0.05] text-[11px] text-white/50 uppercase tracking-widest">
                        <th className="py-3 px-4 font-medium">Timestamp</th>
                        <th className="py-3 px-4 font-medium">Vector Route</th>
                        <th className="py-3 px-4 font-medium text-right">Size (USD)</th>
                        <th className="py-3 px-4 font-medium text-right">Spread</th>
                        <th className="py-3 px-4 font-medium text-right">Net Profit</th>
                        <th className="py-3 px-4 font-medium text-right">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.04] w-full">
                      {opportunities.map((opp, idx) => (
                        <tr key={opp.id} className="hover:bg-white/[0.04] transition-colors text-[11px] text-white/90 w-full">
                          <td className="py-3 px-4 text-white/50">
                            {new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false }).format(opp.timestamp)}
                          </td>
                          <td className="py-3 px-4 flex items-center gap-2">
                            {opp.buy_venue.substring(0, 4).toUpperCase()} <ArrowRight className="h-2.5 w-2.5 text-white/30" /> {opp.sell_venue.substring(0, 4).toUpperCase()}
                          </td>
                          <td className="py-3 px-4 text-right text-white">
                            ${formatNumber(opp.recommended_size_usd, 0)}
                          </td>
                          <td className="py-3 px-4 text-right">
                            <span className={`${opp.net_spread_bps > 0 ? 'text-emerald-400/90' : 'text-red-400/90'}`}>
                              {opp.net_spread_bps > 0 ? '+' : ''}{formatNumber(opp.net_spread_bps, 0)}
                            </span>
                            <span className="text-white/40 text-[9px] ml-1">BPS</span>
                          </td>
                          <td className="py-3 px-4 text-right">
                            <span className={opp.expected_profit_usd > 0 ? 'text-emerald-400/90 font-medium text-[12px]' : 'text-white/40'}>
                              {opp.expected_profit_usd > 0 ? '+' : ''}${formatNumber(opp.expected_profit_usd, 2)}
                            </span>
                          </td>
                          <td className="py-3 px-4 text-right">
                            {opp.status === 'detected' && <span className="text-[10px] uppercase tracking-wider text-blue-400/90">Targeting</span>}
                            {(opp.status === 'expired' || opp.status === 'abandoned') && <span className="text-[10px] uppercase tracking-wider text-white/40">Expired</span>}
                            {opp.status === 'executing' && <span className="text-[10px] uppercase tracking-wider text-amber-500/90">Routing</span>}
                            {opp.status === 'completed' && <span className="text-[10px] uppercase tracking-wider text-emerald-500/90">Secured</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="flex-1 flex flex-col items-center justify-center p-8 bg-black/20 m-6 rounded-sm border border-dashed border-white/10">
                  <Zap className="h-8 w-8 text-white/10 mb-4" />
                  <span className="text-[11px] font-mono tracking-widest uppercase text-white/40">&gt; ZERO ROUTING OPPORTUNITIES DETECTED</span>
                  <span className="text-[10px] font-mono tracking-widest uppercase text-white/30 mt-2">WAITING FOR SPREAD TO EXCEED ALGORITHMIC THRESHOLDS</span>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

      </div>
    </div>
  );
}
