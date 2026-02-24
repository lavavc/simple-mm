'use client';

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { formatNumber, formatAddress } from '@/lib/utils';
import { useAccountBalances } from '@/lib/hooks/useQueries';
import {
  RefreshCw,
  Copy,
  Check,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  ExternalLink,
} from 'lucide-react';
import type { AccountBalance } from '@/types';

const roleInfo: Record<string, { name: string; description: string }> = {
  'aerodrome-lp': {
    name: 'Aerodrome LP',
    description: 'Liquidity provision account for Aerodrome DEX',
  },
  'aerodrome-trade': {
    name: 'Aerodrome Trade',
    description: 'Arbitrage swap execution account',
  },
  'pancakeswap-lp': {
    name: 'PancakeSwap LP',
    description: 'Liquidity provision account for PancakeSwap DEX',
  },
  'pancakeswap-trade': {
    name: 'PancakeSwap Trade',
    description: 'Arbitrage swap execution account',
  },
  blockradar: {
    name: 'Blockradar',
    description: 'B2C wallet system funding account',
  },
  quidax: {
    name: 'Quidax',
    description: 'CEX deposit and trading account',
  },
};

function AccountCard({ account }: { account: AccountBalance }) {
  const [expanded, setExpanded] = useState(account.needs_refill);
  const [copied, setCopied] = useState(false);
  const info = roleInfo[account.role] || { name: account.role, description: '' };

  const handleCopy = () => {
    navigator.clipboard.writeText(account.address);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Get explorer URL for Base chain
  const explorerUrl =
    account.chain_id === 8453
      ? `https://basescan.org/address/${account.address}`
      : `https://etherscan.io/address/${account.address}`;

  return (
    <Card className={account.needs_refill ? 'border-yellow-500/50' : ''}>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">{info.name}</CardTitle>
              {account.needs_refill && (
                <Badge variant="warning" className="flex items-center gap-1">
                  <AlertTriangle className="h-3 w-3" />
                  Needs Refill
                </Badge>
              )}
            </div>
            <p className="text-sm text-muted-foreground mt-1">{info.description}</p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {/* Address */}
        <div className="flex items-center gap-2 mb-4">
          <code className="text-sm bg-secondary px-2 py-1 rounded">
            {formatAddress(account.address, 8)}
          </code>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={handleCopy}>
            {copied ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </Button>
          <a href={explorerUrl} target="_blank" rel="noopener noreferrer">
            <Button variant="ghost" size="icon" className="h-7 w-7">
              <ExternalLink className="h-3 w-3" />
            </Button>
          </a>
          <Badge variant="outline" className="text-xs">
            Chain {account.chain_id}
          </Badge>
        </div>

        {/* Balances grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {/* Native balance */}
          <div className="p-3 bg-secondary/50 rounded-lg">
            <div className="text-xs text-muted-foreground mb-1">
              {account.native_symbol}
            </div>
            <div
              className={`font-mono font-medium ${
                account.refill_reasons.some((r) => r.includes(account.native_symbol))
                  ? 'text-yellow-500'
                  : ''
              }`}
            >
              {formatNumber(account.native_balance, 4)}
            </div>
          </div>

          {/* Token balances */}
          {Object.entries(account.token_balances).map(([token, balance]) => (
            <div key={token} className="p-3 bg-secondary/50 rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">{token}</div>
              <div
                className={`font-mono font-medium ${
                  account.refill_reasons.some((r) => r.includes(token))
                    ? 'text-yellow-500'
                    : ''
                }`}
              >
                {formatNumber(balance, token === 'cNGN' ? 0 : 2)}
              </div>
            </div>
          ))}
        </div>

        {/* Expanded: Refill instructions */}
        {expanded && account.needs_refill && (
          <div className="mt-4 p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-lg">
            <h4 className="text-sm font-medium text-yellow-500 mb-2">
              Refill Required
            </h4>
            <ul className="text-sm space-y-1 mb-3">
              {account.refill_reasons.map((reason, i) => (
                <li key={i} className="flex items-center gap-2">
                  <AlertTriangle className="h-3 w-3 text-yellow-500" />
                  {reason}
                </li>
              ))}
            </ul>
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Send to:</span>
              <code className="text-sm bg-background px-2 py-1 rounded flex-1">
                {account.address}
              </code>
              <Button variant="outline" size="sm" onClick={handleCopy}>
                {copied ? 'Copied!' : 'Copy'}
              </Button>
            </div>
          </div>
        )}

        {/* Expanded: Thresholds */}
        {expanded && (
          <div className="mt-4 pt-4 border-t">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-sm font-medium text-muted-foreground">
                Refill Thresholds
              </h4>
              <Button variant="ghost" size="sm" disabled>
                Edit
              </Button>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
              <div>
                <span className="text-muted-foreground block">
                  Min {account.native_symbol}
                </span>
                <span>0.005</span>
              </div>
              <div>
                <span className="text-muted-foreground block">Min cNGN</span>
                <span>10,000</span>
              </div>
              <div>
                <span className="text-muted-foreground block">Min USDC</span>
                <span>100</span>
              </div>
              <div>
                <span className="text-muted-foreground block">Min USDT</span>
                <span>100</span>
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function AccountsPage() {
  const { data: accounts, isLoading } = useAccountBalances();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const needsRefill = accounts?.filter((a) => a.needs_refill).length || 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Accounts</h1>
          <p className="text-muted-foreground">
            HD wallet accounts for trading operations
          </p>
        </div>
        {needsRefill > 0 && (
          <Badge variant="warning" className="text-base px-3 py-1">
            {needsRefill} account{needsRefill > 1 ? 's' : ''} need refill
          </Badge>
        )}
      </div>

      {/* Account summary */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Overview</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2 font-medium">Role</th>
                  <th className="text-left py-2 font-medium">Address</th>
                  <th className="text-center py-2 font-medium">Chain</th>
                  <th className="text-right py-2 font-medium">ETH</th>
                  <th className="text-right py-2 font-medium">cNGN</th>
                  <th className="text-center py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {accounts?.map((account) => (
                  <tr key={account.role} className="border-b">
                    <td className="py-2">
                      {roleInfo[account.role]?.name || account.role}
                    </td>
                    <td className="py-2 font-mono text-muted-foreground">
                      {formatAddress(account.address, 6)}
                    </td>
                    <td className="py-2 text-center">
                      <Badge variant="outline">{account.chain_id}</Badge>
                    </td>
                    <td className="py-2 text-right font-mono">
                      {formatNumber(account.native_balance, 4)}
                    </td>
                    <td className="py-2 text-right font-mono">
                      {formatNumber(account.token_balances.cNGN || 0, 0)}
                    </td>
                    <td className="py-2 text-center">
                      {account.needs_refill ? (
                        <Badge variant="warning">Needs Refill</Badge>
                      ) : (
                        <Badge variant="success">OK</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* Individual account cards */}
      <div className="space-y-4">
        {accounts?.map((account) => (
          <AccountCard key={account.role} account={account} />
        ))}
      </div>

      {(!accounts || accounts.length === 0) && (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            No accounts configured. Set WALLET_MNEMONIC or USE_TEST_ACCOUNTS=true.
          </CardContent>
        </Card>
      )}
    </div>
  );
}
