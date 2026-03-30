'use client';

import { formatCurrency } from '@/lib/utils';
import { 
  useArbitrageStatus, 
  useArbHistory,
} from '@/lib/hooks/useQueries';
import {
  AlertTriangle,
  Cpu,
  Check,
  BarChart3,
  LineChart,
  History,
} from 'lucide-react';
import React, { useState } from 'react';
import DexArbPage from './dex-arb-tab';
import { ConvergenceEngine } from '@/components/arbitrage/ConvergenceEngine';
import { GlobalInventoryBar } from '@/components/arbitrage/GlobalInventoryBar';
import { ArbHistoryPanel } from '@/components/arbitrage/ArbHistoryPanel';


export default function ArbitragePage() {
  const [activeTab, setActiveTab] = useState<'spot' | 'dex' | 'history'>('spot');
  const { data: status, isLoading: statusLoading } = useArbitrageStatus();
  const { data: history, isLoading: historyLoading } = useArbHistory(30);

  return (
    <div className="flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 animate-in fade-in duration-500 font-sans overflow-hidden relative">
      {/* Precision Grid Background */}
      <div className="absolute inset-0 pointer-events-none opacity-[0.04] bg-[linear-gradient(rgba(16,185,129,0.3)_1px,transparent_1px),linear-gradient(90deg,rgba(16,185,129,0.3)_1px,transparent_1px)] bg-[size:60px_60px] [mask-image:radial-gradient(ellipse_at_center,black_10%,transparent_80%)] z-0" />

      <div className="p-2 md:p-6 font-sans space-y-4 flex-1 flex flex-col relative z-10 overflow-x-hidden overflow-y-auto w-full">

        {/* ── Mission Control HUD ── */}
        <div className="flex items-stretch bg-[#0c0f14] border border-white/[0.06] rounded-sm overflow-hidden shrink-0 w-full">

          {/* Engine Identity */}
          <div className="flex flex-col justify-center gap-1 px-5 py-3.5 border-r border-white/[0.06] shrink-0 bg-emerald-500/[0.04]">
            <div className="flex items-center gap-2">
              <Cpu className="h-3 w-3 text-emerald-500/80" />
              <span className="text-[8px] font-mono tracking-[0.25em] text-emerald-500/60 uppercase">CORE</span>
            </div>
            <h1 className="text-[13px] font-bold tracking-widest uppercase text-white/80 whitespace-nowrap leading-none">ARB ENGINE</h1>
          </div>

          {statusLoading ? (
            <div className="flex items-center gap-2 px-6 text-[10px] font-mono tracking-widest text-white/25">
              <div className="h-2 w-2 border border-white/30 rounded-full animate-spin" />
              SYNCING
            </div>
          ) : (
            <>
              {/* Metrics */}
              {[
                { label: 'PROFIT 24H', value: formatCurrency(status?.total_profit_24h_usd || 0), color: 'text-emerald-400' },
                { label: 'VOLUME 24H', value: formatCurrency(status?.daily_volume_usd || 0), color: 'text-white/75' },
                { label: 'IMBALANCE',  value: formatCurrency(status?.inventory_imbalance_usd || 0), color: 'text-amber-400' },
                { label: 'EXEC / FOUND', value: `${status?.opportunities_executed_24h || 0} / ${status?.opportunities_detected_24h || 0}`, color: 'text-white/60' },
              ].map(({ label, value, color }) => (
                <div key={label} className="flex flex-col justify-center px-5 py-3 border-r border-white/[0.05] min-w-[100px]">
                  <span className="text-[7.5px] font-mono tracking-[0.18em] uppercase text-white/25 mb-1">{label}</span>
                  <span className={`text-[15px] font-mono font-bold leading-none tabular-nums ${color}`}>{value}</span>
                </div>
              ))}

              {/* Separator */}
              <div className="w-px self-stretch bg-white/[0.05] mx-2" />

              {/* Execution Flags */}
              <div className="flex items-center gap-2 px-4">
                <span className="text-[7.5px] font-mono tracking-[0.18em] text-white/20 uppercase mr-1">MODE</span>
                {[
                  { label: 'FIND',     active: status?.enabled },
                  { label: 'CEX-DEX', active: status?.execute_cex_dex },
                  { label: 'DEX-DEX', active: status?.execute_dex_dex },
                ].map(({ label, active }) => (
                  <div
                    key={label}
                    className={`flex items-center gap-1.5 px-2.5 py-1 rounded-[3px] text-[9px] font-mono font-bold tracking-widest border ${
                      active
                        ? 'bg-emerald-500/15 border-emerald-500/35 text-emerald-400'
                        : 'bg-white/[0.02] border-white/[0.07] text-white/20'
                    }`}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${active ? 'bg-emerald-500 shadow-[0_0_5px_rgba(16,185,129,0.8)]' : 'bg-white/[0.12]'}`} />
                    {label}
                  </div>
                ))}
              </div>

              {/* Separator */}
              <div className="w-px self-stretch bg-white/[0.05] mx-2" />

              {/* Circuit Breaker */}
              <div className="flex items-center px-4">
                {status?.circuit_breaker_active ? (
                  <div className="flex flex-col gap-0.5">
                    <span className="text-[7.5px] font-mono tracking-[0.18em] text-white/20 uppercase">BREAKER</span>
                    <div className="flex items-center gap-1.5 text-[10px] font-mono font-bold text-red-400">
                      <AlertTriangle className="h-3 w-3" />
                      TRIPPED · {status.consecutive_failures} FAIL
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col gap-0.5">
                    <span className="text-[7.5px] font-mono tracking-[0.18em] text-white/20 uppercase">BREAKER</span>
                    <div className="flex items-center gap-1.5 text-[10px] font-mono font-bold text-emerald-500">
                      <Check className="h-3 w-3" />
                      NOMINAL
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* Global Layout Flex Grid */}
        <div className="flex flex-col w-full flex-1 min-h-0 bg-[#0B0E14] gap-4">
          <GlobalInventoryBar />

          {/* Master Tabs Controller */}
          <div className="relative z-20 pb-4 pt-1 flex flex-wrap gap-3 mb-2 shrink-0">
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
            <button
              onClick={() => setActiveTab('history')}
              className={`group flex items-center gap-2 px-6 py-2.5 rounded-sm text-[11px] font-mono uppercase tracking-widest transition-all ${activeTab === 'history'
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 shadow-[0_0_15px_rgba(16,185,129,0.1)]'
                : 'bg-white/[0.02] text-white/50 border border-white/5 hover:bg-white/[0.05] hover:text-white/80'
                }`}
            >
              <History className={`h-3.5 w-3.5 transition-colors ${activeTab === 'history' ? 'text-emerald-500' : 'text-white/30 group-hover:text-white/50'}`} />
              Arb History
            </button>
          </div>

          {/* TAB CONTENT */}
          <div className="flex-1 w-full flex flex-col min-h-0 bg-black/20">
            {activeTab === 'spot' ? (
              <div className="w-full flex-1 flex items-start justify-center overflow-y-auto">
                <ConvergenceEngine />
              </div>
            ) : activeTab === 'dex' ? (
              <div className="w-full h-full min-h-[700px] flex flex-col [&>div]:bg-transparent [&>div]:p-0 [&>div]:min-h-0 [&>div]:h-full">
                <DexArbPage />
              </div>
            ) : (
              <div className="w-full flex-1 overflow-y-auto p-2 md:p-4">
                <ArbHistoryPanel items={history} isLoading={historyLoading} />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
