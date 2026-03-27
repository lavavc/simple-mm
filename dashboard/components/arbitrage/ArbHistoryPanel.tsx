'use client';

import type { ReactNode } from 'react';
import type { ArbitrageHistoryEvent, ArbitrageHistoryItem, ArbitrageHistoryWalletSnapshot } from '@/types';
import { formatCurrency, formatNumber, formatRelativeTime, formatAddress, VENUE_LABELS } from '@/lib/utils';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { ArrowRight, CheckCircle2, Clock3, Wallet, XCircle } from 'lucide-react';

function getEvent(item: ArbitrageHistoryItem, type: ArbitrageHistoryEvent['event_type']) {
  return item.events.find((event) => event.event_type === type);
}

function humanizeCapReason(reason?: string) {
  if (!reason) return 'No cap applied';
  return reason
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      if (part === 'buy_wallet_stable') return 'buy wallet stable balance';
      if (part === 'sell_wallet_cngn') return 'sell wallet cNGN balance';
      if (part === 'max_single_trade') return 'max trade cap';
      return part.replace(/_/g, ' ');
    })
    .join(', ');
}

function walletLine(label: string, wallet?: ArbitrageHistoryWalletSnapshot) {
  if (!wallet) return null;
  const venueLabel = VENUE_LABELS[wallet.venue]?.name ?? wallet.venue;
  const stable = wallet.stable_balance != null
    ? `${formatNumber(wallet.stable_balance, 2)} ${wallet.stable_symbol ?? 'stable'}`
    : '—';
  const cngn = wallet.cngn_balance != null
    ? `${formatNumber(wallet.cngn_balance, 2)} cNGN`
    : '—';

  return (
    <div className="flex flex-col gap-1 rounded-sm border border-white/5 bg-black/20 px-3 py-2">
      <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-white/35">{label}</div>
      <div className="text-[12px] font-semibold text-white/80">{venueLabel}</div>
      <div className="text-[12px] font-mono text-white/55">{stable}</div>
      <div className="text-[12px] font-mono text-emerald-400/80">{cngn}</div>
    </div>
  );
}

function stageShell(
  title: string,
  tone: 'neutral' | 'good' | 'bad',
  body: ReactNode,
) {
  const toneClass =
    tone === 'good'
      ? 'border-emerald-500/20 bg-emerald-500/[0.03]'
      : tone === 'bad'
        ? 'border-red-500/20 bg-red-500/[0.03]'
        : 'border-white/5 bg-white/[0.02]';

  return (
    <div className={`rounded-sm border p-3 ${toneClass}`}>
      <div className="mb-3 flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.18em] text-white/45">
        {title}
      </div>
      {body}
    </div>
  );
}

export function ArbHistoryPanel({
  items,
  isLoading = false,
}: {
  items?: ArbitrageHistoryItem[];
  isLoading?: boolean;
}) {
  if (isLoading) {
    return (
      <Card className="border-white/5 bg-[#12161C] shadow-none">
        <CardHeader className="border-b border-white/5 px-4 py-3">
          <div className="text-[11px] font-mono uppercase tracking-[0.22em] text-white/55">Arb History</div>
        </CardHeader>
        <CardContent className="px-4 py-8">
          <div className="text-[12px] font-mono uppercase tracking-[0.18em] text-white/40">Loading lifecycle history...</div>
        </CardContent>
      </Card>
    );
  }

  if (!items?.length) {
    return (
      <Card className="border-white/5 bg-[#12161C] shadow-none">
        <CardHeader className="border-b border-white/5 px-4 py-3">
          <div className="text-[11px] font-mono uppercase tracking-[0.22em] text-white/55">Arb History</div>
        </CardHeader>
        <CardContent className="px-4 py-8">
          <div className="text-[12px] font-mono uppercase tracking-[0.18em] text-white/40">No routed arb history yet</div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-white/5 bg-[#12161C] shadow-none">
      <CardHeader className="border-b border-white/5 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-[11px] font-mono uppercase tracking-[0.22em] text-white/55">Arb History</div>
          <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-white/30">
            detected → routed → executed / failed
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 px-4 py-4">
        {items.map((item) => {
          const detected = getEvent(item, 'detected');
          const routed = getEvent(item, 'routed');
          const finalEvent = item.events.find((event) => event.event_type === 'executed')
            ?? item.events.find((event) => event.event_type === 'failed');
          const isExecuted = finalEvent?.event_type === 'executed';
          const sizeDelta = (item.optimal_size_usd != null && item.routed_size_usd != null)
            ? item.optimal_size_usd - item.routed_size_usd
            : undefined;

          return (
            <div key={item.opportunity_id} className="rounded-sm border border-white/5 bg-black/20 p-4">
              <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-sm border border-emerald-500/20 bg-emerald-500/10 px-2 py-1 text-[10px] font-mono uppercase tracking-[0.18em] text-emerald-400">
                      {item.pipeline === 'cex_dex' ? 'CEX-DEX' : 'DEX-DEX'}
                    </span>
                    <span className="text-[12px] font-semibold text-white/85">
                      {(VENUE_LABELS[item.buy_venue]?.name ?? item.buy_venue)}
                    </span>
                    <ArrowRight className="h-3.5 w-3.5 text-white/30" />
                    <span className="text-[12px] font-semibold text-white/85">
                      {(VENUE_LABELS[item.sell_venue]?.name ?? item.sell_venue)}
                    </span>
                  </div>
                  <div className="text-[11px] font-mono uppercase tracking-[0.16em] text-white/35">
                    {item.direction.replace(/_/g, ' ')}
                  </div>
                </div>

                <div className="flex flex-col items-start gap-2 text-left md:items-end md:text-right">
                  <div className={`rounded-sm px-2 py-1 text-[10px] font-mono uppercase tracking-[0.18em] ${
                    isExecuted ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400'
                  }`}>
                    {item.latest_status.replace(/_/g, ' ')}
                  </div>
                  <div className="text-[11px] font-mono text-white/35">
                    updated {formatRelativeTime(item.updated_at)}
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
                {stageShell(
                  'Detected',
                  'neutral',
                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Optimal size</span>
                      <span className="font-mono text-white/80">
                        {item.optimal_size_usd != null ? formatCurrency(item.optimal_size_usd) : '—'}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Expected profit</span>
                      <span className="font-mono text-emerald-400/85">
                        {item.expected_profit_usd != null ? formatCurrency(item.expected_profit_usd) : '—'}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Spread</span>
                      <span className="font-mono text-white/65">
                        {detected?.net_spread_bps != null ? `${detected.net_spread_bps} bps` : '—'}
                      </span>
                    </div>
                    <div className="pt-1 text-[11px] font-mono text-white/30">
                      {detected ? `detected ${formatRelativeTime(detected.timestamp)}` : '—'}
                    </div>
                  </div>,
                )}

                {stageShell(
                  'Routed',
                  'neutral',
                  <div className="space-y-3">
                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Executable size</span>
                      <span className="font-mono text-white/80">
                        {item.routed_size_usd != null ? formatCurrency(item.routed_size_usd) : '—'}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Net profit after routing</span>
                      <span className="font-mono text-emerald-400/85">
                        {item.net_profit_usd != null ? formatCurrency(item.net_profit_usd) : '—'}
                      </span>
                    </div>
                    <div className="space-y-1 text-[12px]">
                      <div className="text-white/45">Why this size</div>
                      <div className="font-mono text-white/70">{humanizeCapReason(item.cap_reason)}</div>
                      {sizeDelta != null && sizeDelta > 0 && (
                        <div className="font-mono text-amber-400/90">
                          {formatCurrency(sizeDelta)} below optimal
                        </div>
                      )}
                    </div>
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                      {walletLine('Buy wallet', routed?.buy_wallet)}
                      {walletLine('Sell wallet', routed?.sell_wallet)}
                    </div>
                  </div>,
                )}

                {stageShell(
                  isExecuted ? 'Executed' : 'Failed',
                  isExecuted ? 'good' : 'bad',
                  <div className="space-y-3">
                    <div className="flex items-center gap-2">
                      {isExecuted ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                      ) : (
                        <XCircle className="h-4 w-4 text-red-400" />
                      )}
                      <div className={`text-[12px] font-semibold ${isExecuted ? 'text-emerald-400' : 'text-red-400'}`}>
                        {isExecuted ? 'Trade completed' : 'Execution stopped'}
                      </div>
                    </div>

                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Executed size</span>
                      <span className="font-mono text-white/80">
                        {item.executed_size_usd != null ? formatCurrency(item.executed_size_usd) : '—'}
                      </span>
                    </div>

                    <div className="flex items-center justify-between text-[12px]">
                      <span className="text-white/45">Actual profit</span>
                      <span className={`font-mono ${isExecuted ? 'text-emerald-400/90' : 'text-white/45'}`}>
                        {item.actual_profit_usd != null ? formatCurrency(item.actual_profit_usd) : '—'}
                      </span>
                    </div>

                    <div className="space-y-1 text-[12px]">
                      <div className="text-white/45">Reason</div>
                      <div className="font-mono text-white/70">
                        {item.reason ?? (isExecuted ? 'Executed cleanly' : 'Awaiting final result')}
                      </div>
                    </div>

                    <div className="grid grid-cols-1 gap-2">
                      <div className="flex items-center gap-2 text-[12px] text-white/55">
                        <Wallet className="h-3.5 w-3.5 text-white/30" />
                        <span className="font-mono">Buy tx: {finalEvent?.buy_tx_hash ? formatAddress(finalEvent.buy_tx_hash, 5) : '—'}</span>
                      </div>
                      <div className="flex items-center gap-2 text-[12px] text-white/55">
                        <Clock3 className="h-3.5 w-3.5 text-white/30" />
                        <span className="font-mono">Sell tx: {finalEvent?.sell_tx_hash ? formatAddress(finalEvent.sell_tx_hash, 5) : '—'}</span>
                      </div>
                    </div>
                  </div>,
                )}
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
