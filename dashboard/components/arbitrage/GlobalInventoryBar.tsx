'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useAccountBalances, useArbitrageStatus } from '@/lib/hooks/useQueries';
import { formatNumber } from '@/lib/utils';

interface VenueCardProps {
  name: string;
  tag: string;
  tagColor: string;
  usdValue: number;
  stableLabel: string;
  stableAmt: number;
  cNGNAmt: number;
  cNGNUsdEstimate?: number;  // best-effort spot-price estimate
  cNGNUsdValue?: number;     // precise order-book/AMM valuation
  loading: boolean;
  accentColor: 'emerald' | 'blue' | 'violet';
}

const ACCENTS = {
  emerald: {
    bar: 'bg-emerald-500',
    barGlow: 'shadow-[0_0_8px_rgba(16,185,129,0.6)]',
    badge: 'bg-emerald-500/10 border-emerald-500/25 text-emerald-400',
    dot: 'bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.7)]',
    dimDot: 'bg-emerald-500/30',
    value: 'text-white',
  },
  blue: {
    bar: 'bg-blue-500',
    barGlow: 'shadow-[0_0_8px_rgba(59,130,246,0.6)]',
    badge: 'bg-blue-500/10 border-blue-500/25 text-blue-400',
    dot: 'bg-blue-400 shadow-[0_0_6px_rgba(59,130,246,0.7)]',
    dimDot: 'bg-blue-500/30',
    value: 'text-white',
  },
  violet: {
    bar: 'bg-violet-500',
    barGlow: 'shadow-[0_0_8px_rgba(139,92,246,0.6)]',
    badge: 'bg-violet-500/10 border-violet-500/25 text-violet-400',
    dot: 'bg-violet-400 shadow-[0_0_6px_rgba(139,92,246,0.7)]',
    dimDot: 'bg-violet-500/30',
    value: 'text-white',
  },
};

function VenueCard({ name, tag, tagColor, usdValue, stableLabel, stableAmt, cNGNAmt, cNGNUsdEstimate, cNGNUsdValue, loading, accentColor }: VenueCardProps) {
  const a = ACCENTS[accentColor];
  const isOk = usdValue > 10;
  const cNGNDisplay = (cNGNUsdValue && cNGNUsdValue > 0) ? cNGNUsdValue : null;

  return (
    <div className="flex-1 flex flex-col gap-3 px-6 py-4 relative min-w-[200px]">
      {/* Top accent bar */}
      <div className={`absolute top-0 left-6 right-6 h-[1.5px] ${isOk ? `${a.bar} ${a.barGlow}` : 'bg-white/[0.07]'} rounded-full`} />

      {/* Venue label + tag */}
      <div className="flex items-center justify-between mt-1">
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-semibold text-white/70 tracking-wide">{name}</span>
          <span className={`text-[8px] font-mono font-bold tracking-[0.15em] px-1.5 py-0.5 rounded-sm border ${a.badge}`}>{tag}</span>
        </div>
        <div className={`h-2 w-2 rounded-full ${isOk ? `${a.dot}` : 'bg-white/15'}`} />
      </div>

      {/* Main USD value */}
      {loading ? (
        <div className="flex flex-col gap-1.5">
          <div className="h-7 w-28 bg-white/[0.06] rounded animate-pulse" />
          <div className="h-3 w-36 bg-white/[0.03] rounded animate-pulse" />
        </div>
      ) : (
        <>
          <div className="flex items-baseline gap-1">
            <span className="text-[10px] font-mono text-white/30 mb-0.5">$</span>
            <span className="text-[26px] font-mono font-bold text-white leading-none tracking-tight tabular-nums">
              {formatNumber(usdValue, 2)}
            </span>
          </div>

          {/* Token breakdown */}
          <div className="flex items-center gap-1.5 text-[9px] font-mono tracking-wider">
            <span className="text-white/55 tabular-nums">{formatNumber(stableAmt, 2)}</span>
            <span className="text-white/25">{stableLabel}</span>
            <span className="text-white/[0.12] mx-0.5">|</span>
            <span className="text-white/55 tabular-nums">{formatNumber(cNGNAmt, 0)}</span>
            <span className="text-white/25">cNGN</span>
            {cNGNDisplay != null && cNGNAmt > 0 && (
              <span className="text-white/35 ml-1">≈ ${formatNumber(cNGNDisplay, 2)}</span>
            )}
          </div>

          {/* cNGN valuation — only show when we have real orderbook-walk / AMM values */}
          {cNGNDisplay != null && cNGNAmt > 0 && (
            <div className="flex items-center gap-2 text-[8.5px] font-mono tracking-wider border-t border-white/[0.05] pt-1.5">
              <span className="text-white/30">cNGN val:</span>
              <span className="text-amber-400/90">${formatNumber(cNGNDisplay, 2)}</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function GlobalInventoryBar() {
  const { data: balances, isLoading } = useAccountBalances();
  const { data: status } = useArbitrageStatus();

  const { data: cexDexData } = useQuery<any>({
    queryKey: ['quidax_dex_optimal_arb'],
    queryFn: () => null,
    staleTime: Infinity,
  });

  const uniBscTrade = balances?.find((b) => b.role === 'uni-bsc-trade' || b.role === 'trade_uni_bsc');
  const uniBaseTrade = balances?.find((b) => b.role === 'uni-base-trade' || b.role === 'trade_uni_base');
  const quidaxTrade = balances?.find((b) => b.role === 'quidax-exchange');

  const isArmed = !!(status?.enabled || status?.execute_cex_dex || status?.execute_dex_dex);

  const val = cexDexData?.portfolio_value;

  // Only use valuation data when genuinely non-zero.
  // Falls back to spot price if portfolio_value hasn't arrived yet.
  const quidaxValValid = val && (val.quidax_usdt + val.quidax_cngn_usd) > 0;
  const bscValValid    = val && (val.uni_bsc_usdt + val.uni_bsc_cngn_usd) > 0;
  const baseValValid   = val && (val.uni_base_usdc + val.uni_base_cngn_usd) > 0;

  const quidaxUsd = quidaxValValid
    ? (val.quidax_usdt + val.quidax_cngn_usd)
    : ((Number(quidaxTrade?.token_balances?.USDT) || 0) + (Number(quidaxTrade?.token_balances?.cNGN) || 0) * (cexDexData?.prices?.quidax || 0.00066));
  const uniBscUsd = bscValValid
    ? (val.uni_bsc_usdt + val.uni_bsc_cngn_usd)
    : ((Number(uniBscTrade?.token_balances?.USDT) || 0) + (Number(uniBscTrade?.token_balances?.cNGN) || 0) * (cexDexData?.prices?.['uni-bsc'] || 0.00066));
  const uniBaseUsd = baseValValid
    ? (val.uni_base_usdc + val.uni_base_cngn_usd)
    : ((Number(uniBaseTrade?.token_balances?.USDC) || 0) + (Number(uniBaseTrade?.token_balances?.cNGN) || 0) * (cexDexData?.prices?.['uni-base'] || 0.00066));

  // Best-effort cNGN → USD estimates from spot price (always available even before val data)
  const quidaxCNGNEstimate = (Number(quidaxTrade?.token_balances?.cNGN) || 0) * (cexDexData?.prices?.quidax || 0.00066);
  const bscCNGNEstimate    = (Number(uniBscTrade?.token_balances?.cNGN) || 0) * (cexDexData?.prices?.['uni-bsc'] || 0.00066);
  const baseCNGNEstimate   = (Number(uniBaseTrade?.token_balances?.cNGN) || 0) * (cexDexData?.prices?.['uni-base'] || 0.00066);

  const totalUsd = quidaxUsd + uniBscUsd + uniBaseUsd;

  // Allocation ratios for the bar
  const qPct = totalUsd > 0 ? (quidaxUsd / totalUsd) * 100 : 33.3;
  const bscPct = totalUsd > 0 ? (uniBscUsd / totalUsd) * 100 : 33.3;
  const basePct = totalUsd > 0 ? (uniBaseUsd / totalUsd) * 100 : 33.4;

  return (
    <div className="relative w-full shrink-0 bg-[#0c0f14] border border-white/[0.06] rounded-sm overflow-hidden">

      {/* Main content row */}
      <div className="flex items-stretch">

        {/* ── Left: Identity block ── */}
        <div className={`flex flex-col justify-between px-5 py-4 border-r shrink-0 w-36 gap-3 ${isArmed ? 'border-white/[0.06]' : 'border-white/[0.04]'}`}>
          <div className="flex flex-col gap-1">
            <span className="text-[8px] font-mono tracking-[0.2em] text-white/25 uppercase">VENUE CAPITAL</span>
            {isLoading ? (
              <div className="h-5 w-20 bg-white/[0.06] rounded animate-pulse" />
            ) : (
              <span className="text-[15px] font-mono font-bold text-white/80 tabular-nums">${formatNumber(totalUsd, 2)}</span>
            )}
          </div>

          {/* Armed / Standby pill */}
          <div className={`inline-flex items-center gap-1.5 self-start px-2 py-1 rounded-sm border text-[8px] font-mono font-bold tracking-[0.15em] uppercase ${
            isArmed
              ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
              : 'bg-white/[0.03] border-white/[0.07] text-white/25'
          }`}>
            <span className={`h-1.5 w-1.5 rounded-full ${isArmed ? 'bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.8)] animate-pulse' : 'bg-white/20'}`} />
            {isArmed ? 'ARMED' : 'STANDBY'}
          </div>
        </div>

        {/* ── Venue Cards ── */}
        <VenueCard
          name="Quidax" tag="CEX" tagColor="emerald"
          accentColor="emerald"
          usdValue={quidaxUsd}
          stableLabel="USDT"
          stableAmt={Number(quidaxTrade?.token_balances?.USDT) || 0}
          cNGNAmt={Number(quidaxTrade?.token_balances?.cNGN) || 0}
          cNGNUsdEstimate={quidaxCNGNEstimate}
          cNGNUsdValue={val?.quidax_cngn_usd}
          loading={isLoading || !quidaxTrade}
        />

        <div className="w-px self-stretch bg-white/[0.04] my-3" />

        <VenueCard
          name="Uniswap BSC" tag="BSC" tagColor="blue"
          accentColor="blue"
          usdValue={uniBscUsd}
          stableLabel="USDT"
          stableAmt={Number(uniBscTrade?.token_balances?.USDT) || 0}
          cNGNAmt={Number(uniBscTrade?.token_balances?.cNGN) || 0}
          cNGNUsdEstimate={bscCNGNEstimate}
          cNGNUsdValue={val?.uni_bsc_cngn_usd}
          loading={isLoading || !uniBscTrade}
        />

        <div className="w-px self-stretch bg-white/[0.04] my-3" />

        <VenueCard
          name="Uniswap Base" tag="BASE" tagColor="violet"
          accentColor="violet"
          usdValue={uniBaseUsd}
          stableLabel="USDC"
          stableAmt={Number(uniBaseTrade?.token_balances?.USDC) || 0}
          cNGNAmt={Number(uniBaseTrade?.token_balances?.cNGN) || 0}
          cNGNUsdEstimate={baseCNGNEstimate}
          cNGNUsdValue={val?.uni_base_cngn_usd}
          loading={isLoading || !uniBaseTrade}
        />
      </div>

      {/* ── Allocation bar at bottom ── */}
      <div className="flex h-[3px]">
        <div className="bg-emerald-500/60 transition-all duration-700" style={{ width: `${qPct}%` }} />
        <div className="bg-blue-500/60 transition-all duration-700" style={{ width: `${bscPct}%` }} />
        <div className="bg-violet-500/60 transition-all duration-700" style={{ width: `${basePct}%` }} />
      </div>
    </div>
  );
}
