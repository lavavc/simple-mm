'use client';

import type { ArbitrageHistoryItem } from '@/types';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { formatAddress, formatCurrency, formatNumber, formatRelativeTime, VENUE_LABELS } from '@/lib/utils';

function humanizeStatus(status: string) {
  const labels: Record<string, string> = {
    routed: 'Routed',
    completed: 'Completed',
    buy_failed: 'Buy failed',
    sell_preflight_failed: 'Sell preflight failed',
    buy_preflight_failed: 'Buy preflight failed',
    sell_quote_unavailable: 'Sell quote unavailable',
    half_open: 'Half open',
    pool_cache_cold: 'Pool cache cold',
    execution_error: 'Execution error',
  };
  return labels[status] ?? status.replace(/_/g, ' ');
}

function badgeClass(status: string) {
  if (status === 'completed') return 'bg-emerald-500/10 text-emerald-400';
  if (status === 'routed') return 'bg-amber-500/10 text-amber-300';
  return 'bg-red-500/10 text-red-400';
}

function walletValue(label: string, wallet?: ArbitrageHistoryItem['buy_wallet']) {
  if (!wallet) return '—';
  const stable = wallet.stable_balance != null
    ? `${formatNumber(wallet.stable_balance, 2)} ${wallet.stable_symbol ?? 'stable'}`
    : '—';
  const cngn = wallet.cngn_balance != null
    ? `${formatNumber(wallet.cngn_balance, 2)} cNGN`
    : '—';
  return `${label}: ${stable} | ${cngn}`;
}

function normalizeReason(reason?: string) {
  if (!reason) return '';
  let text = reason.trim();

  const tupleMatch = text.match(/\('([^']+)'(?:,\s*'[^']+')+\)/);
  if (tupleMatch) {
    text = text.replace(tupleMatch[0], tupleMatch[1]);
  }

  text = text.replace(/:\s*0x[0-9a-fA-F]{8,}/g, '');
  text = text.replace(/\s+(Direction:|Trade size:|Estimated sell:|Min out:|Wallet:)/g, '\n$1');
  return text;
}

function outcomeText(item: ArbitrageHistoryItem) {
  const status = humanizeStatus(item.latest_status);
  const reason = normalizeReason(item.reason);
  if (!reason) return status;
  if (reason.startsWith(status)) return reason;
  return `${status}: ${reason}`;
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
        <CardContent className="px-4 py-8 text-[12px] font-mono uppercase tracking-[0.18em] text-white/35">
          Loading history...
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
        <CardContent className="px-4 py-8 text-[12px] font-mono uppercase tracking-[0.18em] text-white/35">
          No routed arb history yet
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-white/5 bg-[#12161C] shadow-none">
      <CardHeader className="border-b border-white/5 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="text-[11px] font-mono uppercase tracking-[0.22em] text-white/55">Arb History</div>
          <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-white/30">
            routed → executed / failed
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 px-4 py-4">
        {items.map((item) => (
          <div key={item.opportunity_id} className="rounded-sm border border-white/5 bg-black/20 p-4">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="rounded-sm border border-emerald-500/20 bg-emerald-500/10 px-2 py-1 text-[10px] font-mono uppercase tracking-[0.18em] text-emerald-400">
                    {item.pipeline === 'cex_dex' ? 'CEX-DEX' : 'DEX-DEX'}
                  </span>
                  <span className="text-[12px] font-semibold text-white/85">
                    {VENUE_LABELS[item.buy_venue]?.name ?? item.buy_venue}
                  </span>
                  <span className="text-white/30">→</span>
                  <span className="text-[12px] font-semibold text-white/85">
                    {VENUE_LABELS[item.sell_venue]?.name ?? item.sell_venue}
                  </span>
                </div>
                <div className="text-[11px] font-mono uppercase tracking-[0.16em] text-white/35">
                  {item.direction.replace(/_/g, ' ')}
                </div>
              </div>
              <div className="flex flex-col items-start gap-2 md:items-end">
                <div className={`rounded-sm px-2 py-1 text-[10px] font-mono uppercase tracking-[0.18em] ${badgeClass(item.latest_status)}`}>
                  {humanizeStatus(item.latest_status)}
                </div>
                <div className="text-[11px] font-mono text-white/35">
                  updated {formatRelativeTime(item.updated_at)}
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-sm border border-white/5 bg-white/[0.02] p-3">
                <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-white/35">Sizing</div>
                <div className="space-y-1 text-[12px]">
                  <div className="flex items-center justify-between">
                    <span className="text-white/45">Optimal</span>
                    <span className="font-mono text-white/80">{item.optimal_size_usd != null ? formatCurrency(item.optimal_size_usd) : '—'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-white/45">Routed</span>
                    <span className="font-mono text-white/80">{item.routed_size_usd != null ? formatCurrency(item.routed_size_usd) : '—'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-white/45">Executed</span>
                    <span className="font-mono text-white/80">{item.executed_size_usd != null ? formatCurrency(item.executed_size_usd) : '—'}</span>
                  </div>
                </div>
              </div>

              <div className="rounded-sm border border-white/5 bg-white/[0.02] p-3">
                <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-white/35">Profit</div>
                <div className="space-y-1 text-[12px]">
                  <div className="flex items-center justify-between">
                    <span className="text-white/45">Expected</span>
                    <span className="font-mono text-emerald-400/85">{item.expected_profit_usd != null ? formatCurrency(item.expected_profit_usd) : '—'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-white/45">Net routed</span>
                    <span className="font-mono text-emerald-400/85">{item.net_profit_usd != null ? formatCurrency(item.net_profit_usd) : '—'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-white/45">Actual</span>
                    <span className="font-mono text-white/80">{item.actual_profit_usd != null ? formatCurrency(item.actual_profit_usd) : '—'}</span>
                  </div>
                </div>
              </div>

              <div className="rounded-sm border border-white/5 bg-white/[0.02] p-3">
                <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-white/35">Wallets</div>
                <div className="space-y-2 text-[12px] font-mono text-white/65">
                  <div>{walletValue(VENUE_LABELS[item.buy_venue]?.name ?? item.buy_venue, item.buy_wallet)}</div>
                  <div>{walletValue(VENUE_LABELS[item.sell_venue]?.name ?? item.sell_venue, item.sell_wallet)}</div>
                </div>
              </div>

              <div className="rounded-sm border border-white/5 bg-white/[0.02] p-3">
                <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-white/35">Outcome</div>
                <div className="space-y-2 text-[12px]">
                  <div className="whitespace-pre-wrap break-words font-mono leading-relaxed text-white/65">
                    {outcomeText(item)}
                  </div>
                  <div className="text-[11px] font-mono text-white/40">
                    routed {formatRelativeTime(item.routed_at)}
                  </div>
                  {item.buy_tx_hash && (
                    <div className="break-all font-mono text-blue-400/85">Buy tx: {formatAddress(item.buy_tx_hash)}</div>
                  )}
                  {item.sell_tx_hash && (
                    <div className="break-all font-mono text-purple-400/85">Sell tx: {formatAddress(item.sell_tx_hash)}</div>
                  )}
                </div>
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
