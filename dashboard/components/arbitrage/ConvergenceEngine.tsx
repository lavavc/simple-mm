'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useAccountBalances } from '@/lib/hooks/useQueries';
import { QuidaxOrderBook } from '../orderbook/QuidaxOrderBook';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Zap, ArrowRight, ArrowRightLeft, AlertTriangle } from 'lucide-react';
import { formatNumber } from '@/lib/utils';
import { OrderBookDepthChart } from '@/components/charts/OrderBookDepthChart';



interface DexArbData {
    timestamp: number;
    prices: {
        'uni-bsc': number;
        'uni-base': number;
        assetchain: number;
    };
    stats: {
        uni_bsc_liquidity_cngn_raw: string;
        uni_base_liquidity_cngn_raw: string;
        assetchain_liquidity_cngn_raw: string;
        uni_bsc_ts?: number;
        uni_base_ts?: number;
        assetchain_ts?: number;
    };
    curve_cex_to_dex: any[];
    curve_dex_to_cex: any[];
    all_arbs: {
        direction: string;
        optimal_size_usd: number;
        expected_profit_usd: number;
        cngn_transferred: number;
        expected_usd_out: number;
        net_spread_bps: number;
    }[];
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
        assetchain_fee_bps?: number;
        gas_usd?: number;
    };
}

// Displays the current state of the highest priced DEX pool based on the optimal arbitrage direction
function TargetEngine({ data, isSyncing, arb }: { data: DexArbData | null, isSyncing: boolean, arb?: DexArbData['all_arbs'][0] }) {
    const currentArb = arb || data?.optimal_arb;
    const isProfitable = (currentArb?.expected_profit_usd || 0) > 0;
    
    if (isSyncing) {
        return (
            <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none overflow-hidden">
                <CardHeader className="p-3 border-b border-white/[0.02]">
                    <div className="text-[11px] text-white/60 uppercase tracking-widest font-bold flex items-center gap-2">
                        <Zap className="h-3 w-3 text-white/40" />
                        TARGET ENGINE
                    </div>
                </CardHeader>
                <CardContent className="p-4 py-8 flex flex-col items-center text-center space-y-3">
                    <div className="h-5 w-5 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin" />
                    <div className="text-[11px] text-emerald-500/70 uppercase tracking-widest font-mono animate-pulse">Establishing Connection...</div>
                </CardContent>
            </Card>
        );
    }

    if (!currentArb || !isProfitable) {
        return (
            <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none overflow-hidden">
                <CardHeader className="p-3 border-b border-white/[0.02]">
                    <div className="text-[11px] text-white/60 uppercase tracking-widest font-bold flex items-center gap-2">
                        <Zap className="h-3 w-3 text-white/40" />
                        TARGET ENGINE
                    </div>
                </CardHeader>
                <CardContent className="p-4 py-8 flex flex-col items-center text-center space-y-3">
                    <AlertTriangle className="h-6 w-6 text-yellow-500/70" />
                    <div className="text-[11px] text-yellow-500/70 uppercase tracking-widest font-mono">Awaiting Spreads</div>
                </CardContent>
            </Card>
        );
    }

    const profitUsd = currentArb.expected_profit_usd;
    const optimalSize = currentArb.optimal_size_usd;

    return (
        <Card className={`bg-[#12161C] border rounded-sm shadow-none transition-colors duration-500 border-emerald-500/30`}>
            <CardHeader className="p-3 border-b border-emerald-500/10 bg-emerald-500/[0.02]">
                <div className="text-[11px] text-white/60 uppercase tracking-widest font-bold flex items-center gap-2">
                    <Zap className="h-3 w-3 text-emerald-400" />
                    TARGET ENGINE
                </div>
            </CardHeader>
            <CardContent className="p-4">
                <div className="space-y-4">
                    <div className="flex items-center justify-between bg-black/40 border border-white/10 p-2.5 rounded-sm">
                        <span className="text-[10px] font-mono text-white/80">{currentArb.direction.split('_TO_')[0].replace('_', '-')}</span>
                        <ArrowRight className="h-3 w-3 text-emerald-500/70" />
                        <span className="text-[10px] font-mono text-white/80">{currentArb.direction.split('_TO_')[1]?.replace('_DELTA_BALANCE', '').replace('_', '-') ?? ''}</span>
                    </div>

                    <div className="grid grid-cols-2 gap-3 pb-2">
                        <div className="bg-black/20 p-2.5 rounded-sm border border-white/[0.05]">
                            <div className="text-[10px] text-white/50 uppercase tracking-widest mb-1.5">Opt Size</div>
                            <div className="text-base font-mono text-white">${formatNumber(optimalSize, 0)}</div>
                        </div>
                        <div className="bg-emerald-500/5 p-2.5 rounded-sm border border-emerald-500/20">
                            <div className="text-[10px] text-emerald-500/60 uppercase tracking-widest mb-1.5">Net Profit</div>
                            <div className="text-base font-mono text-emerald-400">+${formatNumber(profitUsd, 2)}</div>
                        </div>
                    </div>

                    <div className="grid grid-cols-1 gap-2 text-xs font-mono border-t border-white/5 pt-3">
                        <div className="text-[9px] uppercase tracking-widest text-white/50 mb-1 flex items-center gap-2">
                            <ArrowRightLeft className="h-3 w-3 text-white/30" />
                            Execution Path
                        </div>
                        
                        {currentArb.direction.startsWith('QUIDAX') ? (
                            <>
                                <div className="flex flex-col gap-1 p-2 bg-white/5 rounded-sm border border-emerald-500/10">
                                    <div className="flex justify-between items-center text-white/80">
                                        <span className="text-blue-400">1. Buy Quidax</span>
                                        <span>${formatNumber(optimalSize, 2)} 
                                            <ArrowRight className="inline h-3 w-3 mx-2 text-white/20" /> 
                                            <span className="text-white">{formatNumber(currentArb.cngn_transferred, 2)} cNGN</span>
                                        </span>
                                    </div>
                                    <div className="flex justify-between text-[9px] text-white/40 items-center">
                                        <span>Taker Fee (0.10%)</span>
                                        <span className="text-rose-400/80">-{formatNumber(currentArb.cngn_transferred / 0.999 * 0.001, 2)} cNGN</span>
                                    </div>
                                </div>
                                <div className="flex flex-col gap-1 p-2 bg-white/5 rounded-sm border border-emerald-500/10">
                                    <div className="flex justify-between items-center text-white/80">
                                        <span className="text-emerald-400">2. Sell {currentArb.direction.includes('BSC') ? 'UNI BSC' : 'UNI BASE'}</span>
                                        <span>{formatNumber(currentArb.cngn_transferred, 2)} cNGN 
                                            <ArrowRight className="inline h-3 w-3 mx-2 text-white/20" /> 
                                            <span className="text-white">${formatNumber(currentArb.expected_usd_out, 2)}</span>
                                        </span>
                                    </div>
                                    <div className="flex justify-between text-[9px] text-white/40 items-center">
                                        <span>LP Swap Fee ({(
                                            (currentArb.direction.includes('BSC') ? data?.optimal_arb?.uni_bsc_fee_bps : data?.optimal_arb?.uni_base_fee_bps) || 0) / 100
                                        }%)</span>
                                        <span className="text-rose-400/80">Factored in AMM</span>
                                    </div>
                                </div>
                            </>
                        ) : (
                            <>
                                <div className="flex flex-col gap-1 p-2 bg-white/5 rounded-sm border border-emerald-500/10">
                                    <div className="flex justify-between items-center text-white/80">
                                        <span className="text-emerald-400">1. Buy {currentArb.direction.includes('BSC') ? 'UNI BSC' : 'UNI BASE'}</span>
                                        <span>${formatNumber(optimalSize, 2)} 
                                            <ArrowRight className="inline h-3 w-3 mx-2 text-white/20" /> 
                                            <span className="text-white">{formatNumber(currentArb.cngn_transferred, 2)} cNGN</span>
                                        </span>
                                    </div>
                                    <div className="flex justify-between text-[9px] text-white/40 items-center">
                                        <span>LP Swap Fee ({(
                                            (currentArb.direction.includes('BSC') ? data?.optimal_arb?.uni_bsc_fee_bps : data?.optimal_arb?.uni_base_fee_bps) || 0) / 100
                                        }%)</span>
                                        <span className="text-rose-400/80">Factored in AMM</span>
                                    </div>
                                </div>
                                <div className="flex flex-col gap-1 p-2 bg-white/5 rounded-sm border border-emerald-500/10">
                                    <div className="flex justify-between items-center text-white/80">
                                        <span className="text-blue-400">2. Sell Quidax</span>
                                        <span>{formatNumber(currentArb.cngn_transferred, 2)} cNGN 
                                            <ArrowRight className="inline h-3 w-3 mx-2 text-white/20" /> 
                                            <span className="text-white">${formatNumber(currentArb.expected_usd_out, 2)}</span>
                                        </span>
                                    </div>
                                    <div className="flex justify-between text-[9px] text-white/40 items-center">
                                        <span>Taker Fee (0.10%)</span>
                                        <span className="text-rose-400/80">-${formatNumber(currentArb.expected_usd_out / 0.999 * 0.001, 2)}</span>
                                    </div>
                                </div>
                            </>
                        )}
                        <div className="flex justify-between items-center text-white/80 pt-2 border-t border-white/10 mt-1">
                            <span className="text-white/50 whitespace-nowrap mr-3 mt-0.5 text-[10px]">Net Spread</span>
                            <div className="text-right break-words max-w-[65%]">
                                <div className="text-emerald-400/90 text-[11px]">+{currentArb.net_spread_bps} BPS</div>
                                <div className="text-white/40 text-[9px] mt-0.5">Formula: ((Out - In) / In) * 10000</div>
                            </div>
                        </div>
                    </div>
                </div>
            </CardContent>
        </Card>
    );
}

export function ConvergenceEngine() {
    // Subscribe to the real CEX-DEX curve WebSocket data
    const { data: cexDexData } = useQuery<DexArbData | null>({
        queryKey: ['quidax_dex_arb_curve'],
        queryFn: () => null,
        staleTime: Infinity, // updated entirely by websocket
    });

    const isSyncing = !cexDexData;

    // Pool stats and spot prices for the depth chart
    const optimalSize = cexDexData?.optimal_arb?.optimal_size_usd;

    return (
        <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr_340px] gap-4 w-full text-slate-300 relative">
            
            {/* Left Column: Target Engines */}
            <div className="space-y-4">
                <div className="flex flex-col gap-4">
                    {isSyncing ? (
                        <TargetEngine data={cexDexData || null} isSyncing={true} />
                    ) : cexDexData?.all_arbs?.filter((a) => a.expected_profit_usd > 0).length ? (
                        cexDexData.all_arbs
                            .filter((a) => a.expected_profit_usd > 0)
                            .map((arb, i) => (
                                <TargetEngine key={i} data={cexDexData} isSyncing={false} arb={arb} />
                            ))
                    ) : (
                        <TargetEngine data={cexDexData || null} isSyncing={false} />
                    )}
                </div>
            </div>

            {/* Center: Price Impact Chart */}
            <div className="min-h-[600px] flex flex-col">
                <OrderBookDepthChart 
                    curveCexToDex={cexDexData?.curve_cex_to_dex}
                    curveDexToCex={cexDexData?.curve_dex_to_cex}
                    direction={cexDexData?.optimal_arb?.direction}
                    optimalSize={optimalSize} 
                />
            </div>

            {/* Right Column: Orderbook */}
            <div className="w-full">
                {/* @ts-ignore */}
                <QuidaxOrderBook />
            </div>
        </div>
    );
}
