'use client';

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatNumber, formatAddress } from '@/lib/utils';
import { useAccountBalances } from '@/lib/hooks/useQueries';
import {
  Copy,
  Check,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Wallet,
  Activity,
  Network
} from 'lucide-react';
import type { AccountBalance } from '@/types';

const roleInfo: Record<string, { name: string; description: string }> = {
  'uni-base-trade': {
    name: 'Uniswap Base Trade',
    description: 'Arbitrage swap execution account on Base',
  },
  'uni-bsc-trade': {
    name: 'Uniswap BSC Trade',
    description: 'Arbitrage swap execution account on BSC',
  },
  'uni-base-lp': {
    name: 'Uniswap Base LP',
    description: 'Liquidity provision account for Uniswap V4 on Base',
  },
  'uni-bsc-lp': {
    name: 'Uniswap BSC LP',
    description: 'Liquidity provision account for Uniswap V4 on BSC',
  },

  'quidax-trade': {
    name: 'Quidax Trade',
    description: 'Quidax account we trade from',
  },
  'quidax-lp': {
    name: 'Quidax LP',
    description: 'Quidax account we LP from',
  },
  blockradar: {
    name: 'Blockradar',
    description: 'B2C wallet system funding account',
  },
};

const ROLE_ORDER = [
  'uni-base-trade',
  'uni-bsc-trade',
  'uni-base-lp',
  'uni-bsc-lp',
  'quidax-trade',
  'quidax-lp',
  'quidax-trade-fund',
  'quidax-exchange',
  'blockradar',
];

function AccountCard({ account }: { account: AccountBalance }) {
  const [expanded, setExpanded] = useState(account.needs_refill);
  const [copied, setCopied] = useState(false);
  const info = roleInfo[account.role] || { name: account.role, description: '' };

  const handleCopy = () => {
    navigator.clipboard.writeText(account.address);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const explorerUrl =
    account.chain_id === 8453
      ? `https://basescan.org/address/${account.address}`
      : account.chain_id === 56
      ? `https://bscscan.com/address/${account.address}`
      : null;

  return (
    <Card className={`relative overflow-hidden bg-gradient-to-b from-white/[0.03] to-white/[0.01] backdrop-blur-xl transition-all duration-300 rounded-sm ${account.needs_refill ? 'border-yellow-500/40 shadow-[0_0_30px_rgba(234,179,8,0.05)]' : 'border-white/[0.05] hover:border-emerald-500/40 hover:shadow-[0_0_30px_rgba(16,185,129,0.05)]'}`}>
      {/* Subtle ambient glow top border */}
      <div className={`absolute top-0 left-0 right-0 h-[1px] ${account.needs_refill ? 'bg-gradient-to-r from-transparent via-yellow-500/50 to-transparent' : 'bg-gradient-to-r from-transparent via-emerald-500/20 to-transparent opacity-0 transition-opacity group-hover:opacity-100'}`} />

      <CardHeader className="pb-3 border-b border-white/[0.05]">
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <Wallet className={`h-4 w-4 ${account.needs_refill ? 'text-yellow-500/80' : 'text-emerald-500/80'}`} />
              <CardTitle className="text-[10px] font-mono tracking-widest uppercase text-white/90">{info.name}</CardTitle>
              {account.needs_refill && (
                <div className="flex items-center gap-1 bg-yellow-500/10 border border-yellow-500/30 px-2 py-0.5 rounded-sm text-[8px] font-mono uppercase tracking-widest text-yellow-500">
                  <AlertTriangle className="h-2 w-2" />
                  REFILL REQUIRED
                </div>
              )}
            </div>
            <p className="text-[9px] font-mono uppercase tracking-widest text-white/30">{info.description}</p>
          </div>
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-white/30 hover:text-white/80 transition-colors p-1"
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        {/* Address & Network */}
        <div className="flex flex-wrap items-center gap-2 mb-6">
          <code className="text-[10px] bg-black/60 border border-white/5 text-emerald-400 font-mono px-3 py-1.5 rounded-sm shadow-inner relative group">
            <span className="absolute inset-0 bg-emerald-500/5 opacity-0 group-hover:opacity-100 transition-opacity rounded-sm" />
            {account.address}
          </code>
          <button onClick={handleCopy} className="p-1.5 hover:bg-white/5 rounded-sm border border-transparent hover:border-white/10 transition-colors text-white/40">
            {copied ? (
              <Check className="h-3 w-3 text-emerald-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </button>
          {explorerUrl && (
            <a href={explorerUrl} target="_blank" rel="noopener noreferrer" className="p-1.5 hover:bg-white/5 rounded-sm border border-transparent hover:border-white/10 transition-colors text-white/40">
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
          <div className="ml-auto flex items-center gap-1.5 bg-white/5 border border-white/10 px-2 py-1 rounded-sm text-[8px] font-mono uppercase tracking-widest text-white/50">
            <Network className="h-2 w-2" />
            {account.chain_id === 8453 ? 'BASE' : account.chain_id === 56 ? 'BSC' : account.chain_id === 0 ? 'CEX' : `CHAIN ${account.chain_id}`}
          </div>
        </div>

        {/* Balances grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {/* Native balance — hidden for CEX accounts */}
          {account.chain_id !== 0 && (
            <div className="p-3 bg-white/[0.02] border border-white/[0.05] rounded-sm flex flex-col items-start shadow-inner">
              <div className="text-[8px] text-white/30 font-mono uppercase tracking-widest mb-1">
                GAS ({account.native_symbol})
              </div>
              <div
                className={`text-sm font-mono tracking-tight font-bold ${account.refill_reasons.some((r) => r.includes(account.native_symbol))
                  ? 'text-yellow-500'
                  : 'text-white/90'
                  }`}
              >
                {formatNumber(account.native_balance, 4)}
              </div>
            </div>
          )}

          {/* Token balances */}
          {Object.entries(account.token_balances).map(([token, balance]) => (
            <div key={token} className="p-3 bg-white/[0.02] border border-white/[0.05] rounded-sm flex flex-col items-start shadow-inner">
              <div className="text-[8px] text-white/30 font-mono uppercase tracking-widest mb-1">{token}</div>
              <div
                className={`text-sm font-mono tracking-tight font-bold ${account.refill_reasons.some((r) => r.includes(token))
                  ? 'text-yellow-500'
                  : 'text-white/90'
                  }`}
              >
                {formatNumber(balance, token === 'cNGN' ? 0 : 2)}
              </div>
            </div>
          ))}
        </div>

        {/* Expanded: Refill instructions */}
        {expanded && account.needs_refill && (
          <div className="mt-4 p-4 bg-yellow-500/5 border border-yellow-500/20 rounded-sm">
            <h4 className="text-[9px] font-mono tracking-widest uppercase text-yellow-500/80 mb-3">
              Action Required: Refill Vectors
            </h4>
            <ul className="text-[10px] font-mono text-yellow-500/60 space-y-2 mb-4">
              {account.refill_reasons.map((reason, i) => (
                <li key={i} className="flex items-center gap-2">
                  <AlertTriangle className="h-3 w-3" />
                  {reason}
                </li>
              ))}
            </ul>
            <div className="flex items-center gap-3">
              <span className="text-[9px] font-mono tracking-widest uppercase text-white/30">DESTINATION:</span>
              <code className="text-[10px] font-mono text-yellow-500/80 bg-black/40 border border-yellow-500/20 px-3 py-1.5 rounded-sm flex-1 truncate">
                {account.address}
              </code>
              <button onClick={handleCopy} className="px-3 py-1.5 bg-yellow-500/10 hover:bg-yellow-500/20 text-yellow-500 border border-yellow-500/30 rounded-sm text-[9px] font-mono tracking-widest uppercase transition-colors">
                {copied ? 'COPIED' : 'COPY'}
              </button>
            </div>
          </div>
        )}

        {/* Expanded: Thresholds */}
        {expanded && (
          <div className="mt-4 pt-4 border-t border-white/[0.05]">
            <div className="flex items-center justify-between mb-4">
              <h4 className="text-[9px] font-mono tracking-widest uppercase text-white/40">
                Operational Minimum Thresholds
              </h4>
              <button disabled className="px-3 py-1 text-[9px] font-mono tracking-widest uppercase text-white/20 border border-white/10 rounded-sm cursor-not-allowed">
                LOCKED
              </button>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-[10px] font-mono">
              <div className="bg-white/5 px-3 py-2 rounded-sm border border-white/5">
                <span className="text-white/30 uppercase tracking-widest block mb-1 text-[8px]">
                  MIN {account.native_symbol}
                </span>
                <span className="text-white/80">0.005</span>
              </div>
              <div className="bg-white/5 px-3 py-2 rounded-sm border border-white/5">
                <span className="text-white/30 uppercase tracking-widest block mb-1 text-[8px]">MIN cNGN</span>
                <span className="text-white/80">10,000</span>
              </div>
              <div className="bg-white/5 px-3 py-2 rounded-sm border border-white/5">
                <span className="text-white/30 uppercase tracking-widest block mb-1 text-[8px]">MIN USDC</span>
                <span className="text-white/80">100</span>
              </div>
              <div className="bg-white/5 px-3 py-2 rounded-sm border border-white/5">
                <span className="text-white/30 uppercase tracking-widest block mb-1 text-[8px]">MIN USDT</span>
                <span className="text-white/80">100</span>
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function AccountsPage() {
  const { data: rawAccounts, isLoading } = useAccountBalances();

  const accounts = rawAccounts?.slice().sort((a, b) => {
    const ai = ROLE_ORDER.indexOf(a.role);
    const bi = ROLE_ORDER.indexOf(b.role);
    return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
  });

  const needsRefill = accounts?.filter((a) => a.needs_refill).length || 0;

  return (
    <div className="relative flex flex-col min-h-[calc(100vh-4rem)] bg-[#0B0E14] text-slate-300 p-2 md:p-6 animate-in fade-in duration-500 font-sans space-y-8">

      {/* Subtle background grid pattern for premium terminal feel */}
      <div className="absolute inset-0 pointer-events-none opacity-[0.03] bg-[linear-gradient(rgba(255,255,255,1)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,1)_1px,transparent_1px)] bg-[size:40px_40px] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_70%)]" />

      {/* Top Status Bar */}
      <div className="flex items-center justify-between border-b border-white/[0.05] pb-3">
        <div className="flex items-center gap-3">
          <Activity className="h-4 w-4 text-emerald-500" />
          <h1 className="text-xs font-bold tracking-widest uppercase text-white">Accounts <span className="text-white/40 font-mono ml-2 normal-case tracking-normal">HD wallet accounts for trading operations</span></h1>
        </div>
        <div className="flex items-center gap-3">
          {isLoading ? (
            <div className="flex items-center gap-2 bg-emerald-500/10 border border-emerald-500/20 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-emerald-500/80">
              <div className="h-2 w-2 border-t-2 border-emerald-500 rounded-full animate-spin" />
              <span>Querying Chains...</span>
            </div>
          ) : needsRefill > 0 ? (
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono bg-yellow-500/10 border border-yellow-500/20 text-yellow-500/80">
              <div className="h-2 w-2 rounded-full bg-yellow-500 animate-pulse" />
              {needsRefill} ACCOUNT{needsRefill > 1 ? 'S' : ''} NEED{needsRefill === 1 ? 'S' : ''} REFILL
            </div>
          ) : (
            <div className="flex items-center gap-2 bg-emerald-500/10 border border-emerald-500/20 px-3 py-1.5 rounded-sm text-[10px] uppercase tracking-widest font-mono text-emerald-500/80">
              <Check className="h-3 w-3" />
              <span>All Systems Funded</span>
            </div>
          )}
        </div>
      </div>

      {/* Account summary */}
      <Card className="relative bg-gradient-to-br from-white/[0.02] to-transparent border-white/[0.05] backdrop-blur-md shadow-2xl">
        <CardHeader className="border-b border-white/[0.05] pb-4">
          <div className="flex items-center gap-2">
            <div className="h-4 w-1 bg-emerald-500/50 rounded-full" />
            <CardTitle className="text-[10px] font-mono font-bold tracking-widest uppercase text-white/60">Wallet Inventory Matrix</CardTitle>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="border-b border-white/[0.05] text-white/30 tracking-widest uppercase">
                  <th className="text-left py-3 font-medium">Role</th>
                  <th className="text-left py-3 font-medium">Address</th>
                  <th className="text-center py-3 font-medium">Network</th>
                  <th className="text-right py-3 font-medium">GAS</th>
                  <th className="text-right py-3 font-medium">cNGN</th>
                  <th className="text-right py-3 font-medium">USDC/T</th>
                  <th className="text-right py-3 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="text-white/80">
                {isLoading ? (
                  Array.from({ length: 4 }).map((_, i) => (
                    <tr key={i} className="border-b border-white/[0.02] last:border-0 hover:bg-white/[0.02] transition-colors">
                      <td className="py-4"><div className="h-3 w-24 bg-white/10 rounded-sm animate-pulse" /></td>
                      <td className="py-4"><div className="h-3 w-28 bg-white/5 rounded-sm animate-pulse" /></td>
                      <td className="py-4"><div className="h-5 w-16 bg-white/5 rounded-sm animate-pulse mx-auto" /></td>
                      <td className="py-4"><div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse ml-auto" /></td>
                      <td className="py-4"><div className="h-3 w-20 bg-white/10 rounded-sm animate-pulse ml-auto" /></td>
                      <td className="py-4"><div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse ml-auto" /></td>
                      <td className="py-4"><div className="h-5 w-20 bg-white/10 rounded-sm animate-pulse ml-auto" /></td>
                    </tr>
                  ))
                ) : (
                  accounts?.map((account) => (
                    <tr key={account.role} className="border-b border-white/[0.02] last:border-0 hover:bg-white/[0.02] transition-colors">
                      <td className="py-3 flex items-center gap-2 text-white">
                        {roleInfo[account.role]?.name || account.role}
                      </td>
                      <td className="py-3 text-white/50">
                        {formatAddress(account.address, 6)}
                      </td>
                      <td className="py-3 text-center">
                        <span className="bg-white/5 border border-white/10 px-2 py-1 rounded-sm text-[8px] text-white/60 uppercase tracking-widest">
                          {account.chain_id === 8453 ? 'BASE' : account.chain_id === 56 ? 'BSC' : account.chain_id === 0 ? 'CEX' : account.chain_id}
                        </span>
                      </td>
                      <td className={`py-3 text-right ${account.refill_reasons.some(r => r.includes(account.native_symbol)) ? 'text-yellow-500 font-bold' : ''}`}>
                        {formatNumber(account.native_balance, 4)}
                      </td>
                      <td className={`py-3 text-right ${account.refill_reasons.some(r => r.includes('cNGN')) ? 'text-yellow-500 font-bold' : ''}`}>
                        {formatNumber(account.token_balances.cNGN || 0, 0)}
                      </td>
                      <td className={`py-3 text-right ${account.refill_reasons.some(r => r.includes('USDC') || r.includes('USDT')) ? 'text-yellow-500 font-bold' : ''}`}>
                        {(() => {
                          const stable = (account.token_balances.USDC || 0) + (account.token_balances.USDT || 0);
                          return stable > 0 ? formatNumber(stable, 2) : <span className="text-white/20">—</span>;
                        })()}
                      </td>
                      <td className="py-3 text-right">
                        {account.needs_refill ? (
                          <span className="text-yellow-500 bg-yellow-500/10 border border-yellow-500/20 px-2 py-1 rounded-sm text-[8px] uppercase tracking-wider inline-flex items-center gap-1">
                            <AlertTriangle className="h-2 w-2" /> REFILL
                          </span>
                        ) : (
                          <span className="text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-2 py-1 rounded-sm text-[8px] uppercase tracking-wider">
                            OPERATIONAL
                          </span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* Individual account cards */}
      <div className="space-y-4">
        {isLoading ? (
          Array.from({ length: 2 }).map((_, i) => (
            <Card key={i} className="bg-white/[0.02] border-white/[0.05] h-[200px] flex items-center justify-center rounded-sm">
              <div className="flex flex-col items-center gap-3">
                <Activity className="h-5 w-5 text-emerald-500/30 animate-pulse" />
                <span className="text-[10px] font-mono uppercase tracking-widest text-emerald-500/30 animate-pulse">Syncing Chain Data...</span>
              </div>
            </Card>
          ))
        ) : (
          accounts?.map((account) => (
            <AccountCard key={account.role} account={account} />
          ))
        )}
      </div>

      {(!isLoading && (!accounts || accounts.length === 0)) && (
        <Card className="bg-white/[0.02] border border-dashed border-white/10 rounded-sm">
          <CardContent className="py-12 text-center flex flex-col items-center justify-center">
            <span className="text-[10px] font-mono tracking-widest uppercase text-white/30">&gt; NO KEYSTORE DETECTED</span>
            <span className="text-[9px] font-mono tracking-widest uppercase text-white/20 mt-2">INITIALIZE WALLET_MNEMONIC ENGINE ENVIRONMENT VARIABLE</span>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
