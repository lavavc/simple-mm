
'use client';

import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { formatNumber } from '@/lib/utils';
import type { OrderBookDepth, OrderBookLevel } from '@/types';
import { Loader2 } from 'lucide-react';

interface ProcessedLevel extends OrderBookLevel {
  cumulative: number;
}

interface QuidaxOrderBookProps {
  activeLevels?: Array<{price: number, amount: number}>;
  activeDirection?: string;
}

export function QuidaxOrderBook({ activeLevels, activeDirection }: QuidaxOrderBookProps) {
  const { data: depth, isLoading: loading, error: queryError } = useQuery<OrderBookDepth | null>({
    queryKey: ['quidaxDepth'],
    queryFn: async () => {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || '/api'}/venues/quidax/depth?limit=20`);
      const data = await response.json();
      if (!response.ok) {
         throw new Error(data.detail || 'Failed to fetch order book depth');
      }
      return data;
    },
    staleTime: Infinity,
  });

  const error = queryError?.message || null;

  // Process data for cumulative totals & max depth size
  const processedBids: ProcessedLevel[] = [];
  const processedAsks: ProcessedLevel[] = [];
  let maxCumulative = 0;

  if (depth) {
    let cumBids = 0;
    for (const bid of depth.bids.slice(0, 15)) {
      cumBids += parseFloat(bid.amount);
      processedBids.push({ ...bid, cumulative: cumBids });
    }
    
    let cumAsks = 0;
    for (const ask of depth.asks.slice(0, 15)) {
      cumAsks += parseFloat(ask.amount);
      processedAsks.push({ ...ask, cumulative: cumAsks });
    }
    
    maxCumulative = Math.max(
      processedBids.length > 0 ? processedBids[processedBids.length - 1].cumulative : 0,
      processedAsks.length > 0 ? processedAsks[processedAsks.length - 1].cumulative : 0
    );
  }

  // Reverse asks for display so the lowest ask is resting at the spread
  const displayAsks = [...processedAsks].reverse();

  // Helper to render a single row in the order book
  const renderRow = (level: ProcessedLevel, type: 'ask' | 'bid', index: number) => {
    const price = parseFloat(level.price);
    const amount = parseFloat(level.amount);
    const cumulative = level.cumulative;
    
    // Width of the background depth bar based on cumulative volume
    const depthWidth = maxCumulative > 0 ? `${Math.min(100, (cumulative / maxCumulative) * 100)}%` : '0%';
    
    let isActive = false;
    let activeRatio = 0;
    if (activeLevels && activeLevels.length > 0 && activeDirection) {
        // If we are selling Quidax USDT to get cNGN, we hit BIDs.
        if (activeDirection.startsWith('QUIDAX_TO_') && type === 'bid') {
            const match = activeLevels.find(l => l.price === price);
            if (match) { isActive = true; activeRatio = match.amount / amount; }
        }
        // If we are buying USDT with cNGN on Quidax, we hit ASKs.
        else if (activeDirection.endsWith('_TO_QUIDAX') && type === 'ask') {
            const match = activeLevels.find(l => l.price === price);
            if (match) { isActive = true; activeRatio = match.amount / amount; }
        }
    }

    const colorClass = type === 'ask' ? 'text-red-500' : 'text-emerald-500';
    let bgClass = type === 'ask' ? 'bg-red-500/10' : 'bg-emerald-500/10';
    const activeColor = type === 'ask' ? 'bg-red-500/40' : 'bg-emerald-500/40';

    return (
      <div 
        key={`${type}-${price}-${index}`} 
        className={`relative flex justify-between items-center text-[10px] font-mono hover:bg-white/5 cursor-pointer z-10 overflow-hidden px-3 h-[22px] transition-colors ${isActive ? (type === 'ask' ? 'bg-red-500/20 shadow-[inset_0_0_10px_rgba(239,68,68,0.2)]' : 'bg-emerald-500/20 shadow-[inset_0_0_10px_rgba(16,185,129,0.2)]') : ''}`}
      >
        {/* Active Volume Consumption Bar */}
        {isActive && (
            <div 
              className={`absolute top-0 right-0 h-full ${activeColor} z-[-1] transition-all duration-300`} 
              style={{ width: `${Math.min(100, activeRatio * 100)}%` }} 
            />
        )}
        
        {/* Visual Depth Bar */}
        <div 
          className={`absolute top-0 right-0 h-full ${bgClass} z-[-2] transition-all duration-300`} 
          style={{ width: depthWidth }} 
        />
        
        <span className={`${colorClass} w-[30%] text-left font-semibold`}>{formatNumber(price, 1)}</span>
        <span className="text-white/90 w-[35%] text-right">{amount >= 1000 ? formatNumber(amount / 1000, 2) + 'k' : formatNumber(amount, 2)}</span>
        <span className="text-white/40 w-[35%] text-right">{cumulative >= 1000 ? formatNumber(cumulative / 1000, 2) + 'k' : formatNumber(cumulative, 2)}</span>
      </div>
    );
  };

  return (
    <Card className="border-white/[0.05] bg-[#12161C] shrink-0 w-full max-w-[380px] mx-auto shadow-2xl">
      <CardHeader className="py-3 px-4 border-b border-white/[0.05]">
        <div className="flex items-center justify-between">
          <CardTitle className="text-[11px] font-mono font-bold tracking-widest uppercase text-white/70 flex items-center gap-2">
            Order Book
            <Badge variant="outline" className="text-[9px] h-4 px-1.5 uppercase font-mono tracking-widest border-emerald-500/30 text-emerald-500 bg-emerald-500/10">Level 2</Badge>
          </CardTitle>
          <div className="flex items-center gap-2">
             {loading && <Loader2 className="h-3 w-3 animate-spin text-white/40" />}
            <span className="text-[10px] text-white/50 font-mono tracking-widest uppercase">cNGN/USDT</span>
          </div>
        </div>
      </CardHeader>
      
      <CardContent className="p-0">
        {error ? (
          <div className="p-8 text-center text-[11px] font-mono text-red-400">
            {error}
          </div>
        ) : !depth && !loading ? (
          <div className="p-8 text-center text-[11px] font-mono text-white/40">
            NO ORDER BOOK DATA INTERCEPTED
          </div>
        ) : (
          <div className="flex flex-col">
            {/* Header Row */}
            <div className="flex justify-between text-[9px] font-medium text-white/40 uppercase tracking-widest py-1.5 px-3 border-b border-white/[0.02] bg-white/[0.01]">
              <span className="w-[22%] text-left">Price</span>
              <span className="w-[26%] text-right">Size</span>
              <span className="w-[26%] text-right">Sum</span>
            </div>

            {/* Asks (Sell Orders) */}
            <div className="flex flex-col justify-end pt-1 bg-gradient-to-b from-transparent to-red-500/[0.01]">
              {displayAsks.map((ask, i) => renderRow(ask, 'ask', i))}
            </div>

            {/* Spread Indicator Strip */}
            <div className="py-2 border-y border-white/[0.03] bg-black/40 flex items-center justify-between px-3 z-20">
               <span className="text-base font-bold tracking-tight text-white flex items-center gap-2">
                 {depth && depth.asks.length > 0 && depth.bids.length > 0 
                   ? formatNumber(parseFloat(depth.asks[0].price), 2)
                   : '0.00'}
                 <span className="text-[9px] font-medium text-white/30 uppercase tracking-widest">cNGN</span>
               </span>
               <span className="text-[9px] text-white/50 font-mono tracking-widest bg-white/5 px-2 py-0.5 rounded-sm border border-white/5">
                 SPREAD: {depth && depth.asks.length > 0 && depth.bids.length > 0 
                   ? formatNumber(((parseFloat(depth.asks[0].price) - parseFloat(depth.bids[0].price)) / parseFloat(depth.bids[0].price)) * 10000, 1) + ' BPS'
                   : 'N/A'}
               </span>
            </div>

            {/* Bids (Buy Orders) */}
            <div className="flex flex-col pb-1 bg-gradient-to-t from-transparent to-emerald-500/[0.01]">
               {processedBids.map((bid, i) => renderRow(bid, 'bid', i))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
