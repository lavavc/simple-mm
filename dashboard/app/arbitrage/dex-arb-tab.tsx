'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { formatNumber } from '@/lib/utils';
import { ArrowRightLeft, Database, Zap, ArrowRight, AlertTriangle, Activity, TrendingUp, Circle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';

import { useAccountBalances } from '@/lib/hooks/useQueries';
import { ProfitCurveChart } from '@/components/charts/ProfitCurveChart';

interface CurvePoint {
    size: number;
    cngn_acquired: number;
    cngn_assetchain: number;
    profit: number;
    profit_after_slippage: number;
    min_acceptable_usd: number;
}

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
        uni_bsc_stable?: number;
        uni_bsc_cngn?: number;
        uni_base_stable?: number;
        uni_base_cngn?: number;
        assetchain_stable?: number;
        assetchain_cngn?: number;
        uni_bsc_ts?: number;
        uni_base_ts?: number;
        assetchain_ts?: number;
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
        assetchain_fee_bps?: number;
        estimated_gas_usd?: number;
    };
}

interface DexArbOpp {
    id: string;
    timestamp: number;
    direction: string;
    optimal_size_usd: number;
    expected_profit_usd: number;
    cngn_transferred: number;
    expected_usd_out: number;
    status: string;
    net_spread_bps: number;
    reason?: string;
    actual_profit_usd?: number;
    uni_bsc_price?: number;
    uni_base_price?: number;
    assetchain_price?: number;
    buy_tx_hash?: string;
    sell_tx_hash?: string;
    slippage_tolerance_bps?: number;
    uni_bsc_fee_bps?: number;
    uni_base_fee_bps?: number;
    assetchain_fee_bps?: number;
    estimated_gas_usd?: number;
}

const fetchDexOpps = async () => {
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || '/api'}/arbitrage/dex-opportunities`, {
        headers: {
            'Authorization': `Bearer ${process.env.NEXT_PUBLIC_API_TOKEN || ''}`
        }
    });
    if (!res.ok) throw new Error('Failed to fetch DEX opps');
    return res.json() as Promise<DexArbOpp[]>;
};

export default function DexArbPage() {
    const { data: curveData } = useQuery<DexArbData | null>({
        queryKey: ['dex_arb_curve'],
        queryFn: () => null,
        staleTime: Infinity, // handled by socket stream
    });

    const { data, isLoading: isOppsLoading } = useQuery({
        queryKey: ['dex_arbitrage_opportunities'],
        queryFn: fetchDexOpps,
        refetchInterval: 10000,
    });
    const dexOpps: DexArbOpp[] = data || [];

    const [now, setNow] = React.useState(Date.now());

    React.useEffect(() => {
        const interval = setInterval(() => setNow(Date.now()), 100);
        return () => clearInterval(interval);
    }, []);

    const [expandedRow, setExpandedRow] = React.useState<string | null>(null);

    const isSyncing = !curveData;
    const timeSinceLastPacket = curveData?.timestamp ? Math.max(0, (now - curveData.timestamp) / 1000).toFixed(1) : "0.0";

    const resolvedCurveData = curveData || {
        timestamp: 0,
        prices: { 'uni-bsc': 0, 'uni-base': 0, assetchain: 0 },
        stats: {
            uni_bsc_liquidity_cngn_raw: "0", uni_base_liquidity_cngn_raw: "0", assetchain_liquidity_cngn_raw: "0",
            uni_bsc_stable: 0, uni_bsc_cngn: 0, uni_base_stable: 0, uni_base_cngn: 0, assetchain_stable: 0, assetchain_cngn: 0,
            uni_bsc_ts: 0, uni_base_ts: 0, assetchain_ts: 0
        },
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
                    <h1 className="text-xs font-bold tracking-widest uppercase text-white">Dex Arbitrage</h1>
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

            <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
                {/* LEFT COLUMN: Controls & Statuss */}
                <div className="lg:col-span-1 space-y-4">
                    <Card className={`bg-[#12161C] border rounded-sm shadow-none transition-colors duration-500 ${resolvedCurveData.optimal_arb.expected_profit_usd > 0 ? 'border-emerald-500/30' : 'border-white/[0.05]'}`}>
                        <CardHeader className={`p-3 border-b ${resolvedCurveData.optimal_arb.expected_profit_usd > 0 ? 'border-emerald-500/10 bg-emerald-500/[0.02]' : 'border-white/[0.02]'}`}>
                            <div className="text-[11px] text-white/60 uppercase tracking-widest font-bold flex items-center gap-2">
                                <Zap className={`h-3 w-3 ${resolvedCurveData.optimal_arb.expected_profit_usd > 0 ? 'text-emerald-400' : 'text-white/40'}`} />
                                TARGET ENGINE
                            </div>
                        </CardHeader>
                        <CardContent className="p-4">
                            {isSyncing ? (
                                <div className="py-6 flex flex-col items-center text-center space-y-3">
                                    <div className="h-5 w-5 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin" />
                                    <div className="text-[11px] text-emerald-500/70 uppercase tracking-widest font-mono animate-pulse">Establishing Connection...</div>
                                    <div className="text-[11px] font-mono text-white/40">Max: -$0.0000</div>
                                </div>
                            ) : resolvedCurveData.optimal_arb.expected_profit_usd <= 0 ? (
                                <div className="py-6 flex flex-col items-center text-center space-y-3">
                                    <AlertTriangle className="h-6 w-6 text-yellow-500/70" />
                                    <div className="text-[11px] text-yellow-500/70 uppercase tracking-widest font-mono">Awaiting Spreads</div>
                                    <div className="text-[11px] font-mono text-white/40">Max: <span className="text-red-400/70">-${Math.abs(resolvedCurveData.optimal_arb.expected_profit_usd).toFixed(4)}</span></div>
                                </div>
                            ) : (
                                <div className="space-y-4">
                                    <div className="flex items-center justify-between bg-black/40 border border-white/10 p-2.5 rounded-sm">
                                        <span className="text-[10px] font-mono text-white/80">{resolvedCurveData.optimal_arb.direction.split('_TO_')[0].replace('_', '-')}</span>
                                        <ArrowRight className="h-3 w-3 text-emerald-500/70" />
                                        <span className="text-[10px] font-mono text-white/80">{resolvedCurveData.optimal_arb.direction.split('_TO_')[1]?.replace('_DELTA_BALANCE', '').replace('_', '-') ?? ''}</span>
                                    </div>

                                    <div className="grid grid-cols-2 gap-3 pb-2">
                                        <div className="bg-black/20 p-2.5 rounded-sm border border-white/[0.05]">
                                            <div className="text-[10px] text-white/50 uppercase tracking-widest mb-1.5">Opt Size</div>
                                            <div className="text-base font-mono text-white">${formatNumber(resolvedCurveData.optimal_arb.optimal_size_usd, 0)}</div>
                                        </div>
                                        <div className="bg-emerald-500/5 p-2.5 rounded-sm border border-emerald-500/20">
                                            <div className="text-[10px] text-emerald-500/60 uppercase tracking-widest mb-1.5">Net Profit</div>
                                            <div className="text-base font-mono text-emerald-400">+${formatNumber(resolvedCurveData.optimal_arb.expected_profit_usd, 2)}</div>
                                        </div>
                                    </div>

                                    {/* Detailed breakdown & Post-Trade sections */}
                                    {(() => {
                                        const dir = resolvedCurveData.optimal_arb.direction || '';
                                        const dirParts = dir.split('_TO_');
                                        const fromVenue = dirParts[0] || 'UNI_BSC';
                                        const toVenue = dirParts[1]?.replace('_DELTA_BALANCE', '') || 'UNI_BASE';

                                        const getVenuePrice = (v: string) => {
                                            if (v === 'UNI_BSC') return resolvedCurveData.prices['uni-bsc'] || 0;
                                            if (v === 'UNI_BASE') return resolvedCurveData.prices['uni-base'] || 0;
                                            if (v === 'ASSETCHAIN') return resolvedCurveData.prices.assetchain || 0;
                                            return 0;
                                        };
                                        const getVenueShort = (v: string) => {
                                            if (v === 'UNI_BSC') return 'UBSC';
                                            if (v === 'UNI_BASE') return 'UBAS';
                                            if (v === 'ASSETCHAIN') return 'ASST';
                                            return v;
                                        };
                                        const getStable = (v: string) => v === 'UNI_BASE' ? 'USDC' : 'USDT';
                                        const getNetworkName = (v: string) => {
                                            if (v === 'UNI_BSC') return 'BSC Inventory';
                                            if (v === 'UNI_BASE') return 'Base Inventory';
                                            if (v === 'ASSETCHAIN') return 'AssetChain Inv';
                                            return `${v} Inv`;
                                        };

                                        const priceFrom = getVenuePrice(fromVenue);
                                        const priceTo = getVenuePrice(toVenue);
                                        const shortFrom = getVenueShort(fromVenue);
                                        const shortTo = getVenueShort(toVenue);
                                        const fromNetwork = getNetworkName(fromVenue);
                                        const toNetwork = getNetworkName(toVenue);
                                        const initialSpreadBps = priceFrom && priceTo ? Math.abs((priceTo - priceFrom) / priceFrom * 10000).toFixed(0) : 0;
                                        const execPath = `${getStable(fromVenue)} -> cNGN | cNGN -> ${getStable(toVenue)}`;

                                        return (
                                            <>
                                                <div className="grid grid-cols-1 gap-2 text-xs font-mono border-t border-white/5 pt-3">
                                                    <div className="flex flex-col gap-1 p-2 bg-white/5 rounded-sm border border-emerald-500/10">
                                                        <div className="flex justify-between items-center text-white/80">
                                                            <span className="text-emerald-400">1. Buy {fromVenue.replace('UNI_', 'UNI ')}</span>
                                                            <span>${formatNumber(resolvedCurveData.optimal_arb.optimal_size_usd, 2)} 
                                                                <ArrowRight className="inline h-3 w-3 mx-2 text-white/20" /> 
                                                                <span className="text-white">{formatNumber(resolvedCurveData.optimal_arb.cngn_transferred, 2)} cNGN</span>
                                                            </span>
                                                        </div>
                                                        <div className="flex justify-between text-[9px] text-white/40 items-center">
                                                            <span>LP Swap Fee ({(
                                                                (fromVenue === 'UNI_BSC' ? resolvedCurveData.optimal_arb.uni_bsc_fee_bps : fromVenue === 'UNI_BASE' ? resolvedCurveData.optimal_arb.uni_base_fee_bps : resolvedCurveData.optimal_arb.assetchain_fee_bps) || 0) / 100
                                                            }%)</span>
                                                            <span className="text-rose-400/80">Factored in AMM</span>
                                                        </div>
                                                    </div>
                                                    <div className="flex flex-col gap-1 p-2 bg-white/5 rounded-sm border border-emerald-500/10">
                                                        <div className="flex justify-between items-center text-white/80">
                                                            <span className="text-emerald-400">2. Sell {toVenue.replace('UNI_', 'UNI ')}</span>
                                                            <span>{formatNumber(resolvedCurveData.optimal_arb.cngn_transferred, 2)} cNGN 
                                                                <ArrowRight className="inline h-3 w-3 mx-2 text-white/20" /> 
                                                                <span className="text-white">${formatNumber(resolvedCurveData.optimal_arb.expected_usd_out, 2)}</span>
                                                            </span>
                                                        </div>
                                                        <div className="flex justify-between text-[9px] text-white/40 items-center">
                                                            <span>LP Swap Fee ({(
                                                                (toVenue === 'UNI_BSC' ? resolvedCurveData.optimal_arb.uni_bsc_fee_bps : toVenue === 'UNI_BASE' ? resolvedCurveData.optimal_arb.uni_base_fee_bps : resolvedCurveData.optimal_arb.assetchain_fee_bps) || 0) / 100
                                                            }%)</span>
                                                            <span className="text-rose-400/80">Factored in AMM</span>
                                                        </div>
                                                    </div>
                                                    <div className="flex justify-between items-center text-white/80 pt-2 border-t border-white/10 mt-1">
                                                        <span className="text-white/50 whitespace-nowrap mr-3 mt-0.5 text-[10px]">Net Spread</span>
                                                        <div className="text-right break-words max-w-[65%]">
                                                            <div className="text-emerald-400/90 text-[11px]">+{resolvedCurveData.optimal_arb.net_spread_bps} BPS</div>
                                                            <div className="text-white/40 text-[9px] mt-0.5">Formula: ((Out - In) / In) * 10000</div>
                                                        </div>
                                                    </div>
                                                </div>

                                                <div className="space-y-3 border-t border-white/10 pt-4 mb-4">
                                                    <div className="text-[11px] text-white/60 uppercase tracking-widest mb-1.5 font-bold">Post-Trade Execution Inventory</div>

                                                    <div className="bg-black/20 p-2.5 rounded-sm border border-white/[0.05] flex justify-between items-center text-[11px] font-mono">
                                                        <span className="text-white/70">{fromNetwork}</span>
                                                        <div className="text-right leading-tight">
                                                            <div className="text-red-400/90 text-xs">-${formatNumber(resolvedCurveData.optimal_arb.optimal_size_usd, 0)}</div>
                                                            <div className="text-emerald-400/70 mt-0.5">+{formatNumber(resolvedCurveData.optimal_arb.cngn_transferred, 0)} cNGN</div>
                                                        </div>
                                                    </div>

                                                    <div className="bg-black/20 p-2.5 rounded-sm border border-white/[0.05] flex justify-between items-center text-[11px] font-mono">
                                                        <span className="text-white/70">{toNetwork}</span>
                                                        <div className="text-right leading-tight">
                                                            <div className="text-emerald-400/90 text-xs">+${formatNumber(resolvedCurveData.optimal_arb.expected_usd_out, 2)}</div>
                                                            <div className="text-red-400/70 mt-0.5">-{formatNumber(resolvedCurveData.optimal_arb.cngn_transferred, 0)} cNGN</div>
                                                        </div>
                                                    </div>
                                                </div>
                                            </>
                                        );
                                    })()}

                                    <Button className="w-full bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded-sm font-mono text-[11px] uppercase tracking-wider h-10 transition-colors mt-2">
                                        Execute Sequence
                                    </Button>
                                </div>
                            )}
                        </CardContent>
                    </Card>
                </div>

                {/* MIDDLE COLUMN: Prices + Chart */}
                <div className="lg:col-span-3 space-y-4">
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        {isSyncing ? (
                            <>
                                <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 p-4 pt-4">
                                        <div className="flex items-center gap-2">
                                            <div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse" />
                                            <div className="h-3 w-8 bg-white/5 rounded-sm animate-pulse" />
                                        </div>
                                        <div className="h-2 w-2 rounded-full bg-white/10 animate-pulse" />
                                    </CardHeader>
                                    <CardContent className="px-4 pb-4 pt-2">
                                        <div>
                                            <div className="flex items-center gap-2 mb-2">
                                                <Activity className="h-4 w-4 text-emerald-500/20" />
                                                <div className="h-6 w-24 bg-white/10 rounded-sm animate-pulse" />
                                                <div className="h-3 w-10 bg-white/5 rounded-sm animate-pulse ml-1" />
                                            </div>
                                            <div className="grid grid-cols-2 gap-2 mt-3 border-t border-white/[0.05] pt-3">
                                                <div>
                                                    <div className="h-2 w-16 bg-white/5 rounded-sm mb-2" />
                                                    <div className="h-3 w-20 bg-emerald-400/20 rounded-sm animate-pulse" />
                                                </div>
                                            </div>
                                            <div className="flex justify-end mt-4">
                                                <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                                            </div>
                                        </div>
                                    </CardContent>
                                </Card>
                                <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 p-4 pt-4">
                                        <div className="flex items-center gap-2">
                                            <div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse" />
                                            <div className="h-3 w-8 bg-white/5 rounded-sm animate-pulse" />
                                        </div>
                                        <div className="h-2 w-2 rounded-full bg-white/10 animate-pulse" />
                                    </CardHeader>
                                    <CardContent className="px-4 pb-4 pt-2">
                                        <div>
                                            <div className="flex items-center gap-2 mb-2">
                                                <Activity className="h-4 w-4 text-emerald-500/20" />
                                                <div className="h-6 w-24 bg-white/10 rounded-sm animate-pulse" />
                                                <div className="h-3 w-10 bg-white/5 rounded-sm animate-pulse ml-1" />
                                            </div>
                                            <div className="grid grid-cols-2 gap-2 mt-3 border-t border-white/[0.05] pt-3">
                                                <div>
                                                    <div className="h-2 w-16 bg-white/5 rounded-sm mb-2" />
                                                    <div className="h-3 w-20 bg-emerald-400/20 rounded-sm animate-pulse" />
                                                </div>
                                            </div>
                                            <div className="flex justify-end mt-4">
                                                <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                                            </div>
                                        </div>
                                    </CardContent>
                                </Card>
                                <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 p-4 pt-4">
                                        <div className="flex items-center gap-2">
                                            <div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse" />
                                            <div className="h-3 w-8 bg-white/5 rounded-sm animate-pulse" />
                                        </div>
                                        <div className="h-2 w-2 rounded-full bg-white/10 animate-pulse" />
                                    </CardHeader>
                                    <CardContent className="px-4 pb-4 pt-2">
                                        <div>
                                            <div className="flex items-center gap-2 mb-2">
                                                <Activity className="h-4 w-4 text-emerald-500/20" />
                                                <div className="h-6 w-24 bg-white/10 rounded-sm animate-pulse" />
                                                <div className="h-3 w-10 bg-white/5 rounded-sm animate-pulse ml-1" />
                                            </div>
                                            <div className="grid grid-cols-2 gap-2 mt-3 border-t border-white/[0.05] pt-3">
                                                <div>
                                                    <div className="h-2 w-16 bg-white/5 rounded-sm mb-2" />
                                                    <div className="h-3 w-20 bg-emerald-400/20 rounded-sm animate-pulse" />
                                                </div>
                                            </div>
                                            <div className="flex justify-end mt-4">
                                                <div className="h-2 w-16 bg-white/5 rounded-sm animate-pulse" />
                                            </div>
                                        </div>
                                    </CardContent>
                                </Card>
                            </>
                        ) : (
                            <>
                                <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 p-4 pt-4">
                                        <div className="flex items-center gap-2">
                                            <div className="text-[10px] text-white/90 uppercase tracking-widest font-mono font-bold">UNI BSC</div>
                                            <Badge variant="outline" className="text-[8px] bg-emerald-500/10 border-emerald-500/30 text-emerald-400 font-mono">EXECUTABLE</Badge>
                                        </div>
                                        <Circle className="h-2 w-2 fill-emerald-500 text-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
                                    </CardHeader>
                                    <CardContent className="px-4 pb-4 pt-2">
                                        <div className="flex items-center gap-2 mb-2">
                                            <TrendingUp className="h-4 w-4 text-emerald-500/50" />
                                            <span className="text-xl font-bold font-mono tracking-tight text-white">${(resolvedCurveData.prices['uni-bsc'] || 0).toFixed(7)}</span>
                                            <span className="text-[10px] text-white/40 uppercase tracking-widest font-mono">USD</span>
                                        </div>
                                        <div className="grid grid-cols-2 gap-2 mt-3 text-[10px] font-mono border-t border-white/[0.05] pt-3">
                                            <div>
                                                <div className="text-white/30 uppercase tracking-widest mb-1 text-[8px]">IMPLIED RATE</div>
                                                <div className="flex items-center gap-1.5 text-emerald-400">
                                                    <ArrowRightLeft className="h-3 w-3" />
                                                    {formatNumber(1 / (resolvedCurveData.prices['uni-bsc'] || 1), 2)} cNGN
                                                </div>
                                            </div>
                                            <div className="col-span-2 mt-2 pt-2 border-t border-white/[0.05]">
                                                <div className="text-white/30 uppercase tracking-widest mb-2 text-[10px]">Pool balances</div>
                                                <div className="flex justify-between items-end mb-1">
                                                    <div className="text-white font-bold">{formatNumber(resolvedCurveData.stats.uni_bsc_stable || 0, 2)} USDT</div>
                                                    <div className="text-emerald-400 font-bold">{formatNumber(resolvedCurveData.stats.uni_bsc_cngn || 0, 2)} cNGN</div>
                                                </div>
                                                <div className="h-1.5 w-full bg-white/5 rounded-full overflow-hidden flex">
                                                    <div
                                                        className="h-full bg-[#2563eb]"
                                                        style={{ width: `${Math.max(10, Math.min(90, ((resolvedCurveData.stats.uni_bsc_stable || 0) / ((resolvedCurveData.stats.uni_bsc_stable || 1) + (resolvedCurveData.stats.uni_bsc_cngn || 1) / 1500)) * 100))}%` }}
                                                    />
                                                    <div className="h-full flex-1 bg-emerald-400" />
                                                </div>
                                            </div>
                                        </div>
                                        <p className="text-[8px] text-white/30 tracking-widest uppercase font-mono mt-4 text-right">
                                            {resolvedCurveData.stats.uni_bsc_ts ? Math.max(0, Math.floor(now / 1000 - resolvedCurveData.stats.uni_bsc_ts)) : timeSinceLastPacket}S AGO
                                        </p>
                                    </CardContent>
                                </Card>

                                <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 p-4 pt-4">
                                        <div className="flex items-center gap-2">
                                            <div className="text-[10px] text-white/90 uppercase tracking-widest font-mono font-bold">UNI BASE</div>
                                            <Badge variant="outline" className="text-[8px] bg-emerald-500/10 border-emerald-500/30 text-emerald-400 font-mono">EXECUTABLE</Badge>
                                        </div>
                                        <Circle className="h-2 w-2 fill-emerald-500 text-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
                                    </CardHeader>
                                    <CardContent className="px-4 pb-4 pt-2">
                                        <div className="flex items-center gap-2 mb-2">
                                            <TrendingUp className="h-4 w-4 text-emerald-500/50" />
                                            <span className="text-xl font-bold font-mono tracking-tight text-white">${(resolvedCurveData.prices['uni-base'] || 0).toFixed(7)}</span>
                                            <span className="text-[10px] text-white/40 uppercase tracking-widest font-mono">USD</span>
                                        </div>
                                        <div className="grid grid-cols-2 gap-2 mt-3 text-[10px] font-mono border-t border-white/[0.05] pt-3">
                                            <div>
                                                <div className="text-white/30 uppercase tracking-widest mb-1 text-[8px]">IMPLIED RATE</div>
                                                <div className="flex items-center gap-1.5 text-emerald-400">
                                                    <ArrowRightLeft className="h-3 w-3" />
                                                    {formatNumber(1 / (resolvedCurveData.prices['uni-base'] || 1), 2)} cNGN
                                                </div>
                                            </div>
                                            <div className="col-span-2 mt-2 pt-2 border-t border-white/[0.05]">
                                                <div className="text-white/30 uppercase tracking-widest mb-2 text-[10px]">Pool balances</div>
                                                <div className="flex justify-between items-end mb-1">
                                                    <div className="text-white font-bold">{formatNumber(resolvedCurveData.stats.uni_base_stable || 0, 2)} USDC</div>
                                                    <div className="text-emerald-400 font-bold">{formatNumber(resolvedCurveData.stats.uni_base_cngn || 0, 2)} cNGN</div>
                                                </div>
                                                <div className="h-1.5 w-full bg-white/5 rounded-full overflow-hidden flex">
                                                    <div
                                                        className="h-full bg-[#2563eb]"
                                                        style={{ width: `${Math.max(10, Math.min(90, ((resolvedCurveData.stats.uni_base_stable || 0) / ((resolvedCurveData.stats.uni_base_stable || 1) + (resolvedCurveData.stats.uni_base_cngn || 1) / 1500)) * 100))}%` }}
                                                    />
                                                    <div className="h-full flex-1 bg-emerald-400" />
                                                </div>
                                            </div>
                                        </div>
                                        <p className="text-[8px] text-white/30 tracking-widest uppercase font-mono mt-4 text-right">
                                            {resolvedCurveData.stats.uni_base_ts ? Math.max(0, Math.floor(now / 1000 - resolvedCurveData.stats.uni_base_ts)) : timeSinceLastPacket}S AGO
                                        </p>
                                    </CardContent>
                                </Card>

                                <Card className="hover:border-emerald-500/50 transition-colors bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 p-4 pt-4">
                                        <div className="flex items-center gap-2">
                                            <div className="text-[10px] text-white/90 uppercase tracking-widest font-mono font-bold">ASSETCHAIN</div>
                                            <Badge variant="outline" className="text-[8px] bg-blue-500/10 border-blue-500/30 text-blue-400 font-mono">OBSERVATIONAL</Badge>
                                        </div>
                                        <Circle className="h-2 w-2 fill-blue-500 text-blue-500 shadow-[0_0_8px_rgba(59,130,246,0.8)]" />
                                    </CardHeader>
                                    <CardContent className="px-4 pb-4 pt-2">
                                        <div className="flex items-center gap-2 mb-2">
                                            <TrendingUp className="h-4 w-4 text-blue-500/50" />
                                            <span className="text-xl font-bold font-mono tracking-tight text-white">${(resolvedCurveData.prices.assetchain || 0).toFixed(7)}</span>
                                            <span className="text-[10px] text-white/40 uppercase tracking-widest font-mono">USD</span>
                                        </div>
                                        <div className="grid grid-cols-2 gap-2 mt-3 text-[10px] font-mono border-t border-white/[0.05] pt-3">
                                            <div>
                                                <div className="text-white/30 uppercase tracking-widest mb-1 text-[8px]">IMPLIED RATE</div>
                                                <div className="flex items-center gap-1.5 text-blue-400">
                                                    <ArrowRightLeft className="h-3 w-3" />
                                                    {formatNumber(1 / (resolvedCurveData.prices.assetchain || 1), 2)} cNGN
                                                </div>
                                            </div>
                                            <div className="col-span-2 mt-2 pt-2 border-t border-white/[0.05]">
                                                <div className="text-white/30 uppercase tracking-widest mb-2 text-[10px]">Pool balances</div>
                                                <div className="flex justify-between items-end mb-1">
                                                    <div className="text-white font-bold">{formatNumber(resolvedCurveData.stats.assetchain_stable || 0, 2)} USDT</div>
                                                    <div className="text-blue-400 font-bold">{formatNumber(resolvedCurveData.stats.assetchain_cngn || 0, 2)} cNGN</div>
                                                </div>
                                                <div className="h-1.5 w-full bg-white/5 rounded-full overflow-hidden flex">
                                                    <div
                                                        className="h-full bg-slate-500"
                                                        style={{ width: `${Math.max(10, Math.min(90, ((resolvedCurveData.stats.assetchain_stable || 0) / ((resolvedCurveData.stats.assetchain_stable || 1) + (resolvedCurveData.stats.assetchain_cngn || 1) / 1500)) * 100))}%` }}
                                                    />
                                                    <div className="h-full flex-1 bg-blue-400" />
                                                </div>
                                            </div>
                                        </div>
                                        <p className="text-[8px] text-white/30 tracking-widest uppercase font-mono mt-4 text-right">
                                            {resolvedCurveData.stats.assetchain_ts ? Math.max(0, Math.floor(now / 1000 - resolvedCurveData.stats.assetchain_ts)) : timeSinceLastPacket}S AGO
                                        </p>
                                    </CardContent>
                                </Card>
                            </>
                        )}
                    </div>

                    <div className="h-[600px] mt-4 mb-4">
                        <ProfitCurveChart 
                            data={resolvedCurveData.curve} 
                            optimalSize={resolvedCurveData.optimal_arb.optimal_size_usd} 
                            maxProfit={resolvedCurveData.optimal_arb.expected_profit_usd}
                            isSyncing={isSyncing}
                            direction={resolvedCurveData.optimal_arb.direction}
                        />
                    </div>

                    <div className="mt-6 hidden">
                        <Card className="bg-white/[0.02] border-white/[0.05] rounded-sm shadow-none">
                            <CardHeader className="p-4 border-b border-white/[0.05]">
                            <div className="text-[11px] text-white/60 font-bold uppercase tracking-widest font-mono flex items-center gap-2">
                                <Database className="h-3.5 w-3.5" />
                                Execution Ledger
                                {isSyncing && <div className="h-3 w-3 border border-white/40 rounded-full border-t-white animate-spin ml-2" />}
                            </div>
                        </CardHeader>
                        <CardContent className="p-0">
                            <div className="overflow-x-auto">
                                <table className="w-full text-left font-mono">
                                    <thead>
                                        <tr className="border-b border-white/[0.05] text-[11px] text-white/50 uppercase tracking-widest">
                                            <th className="py-3 px-4 font-medium">Timestamp</th>
                                            <th className="py-3 px-4 font-medium">Vector Route</th>
                                            <th className="py-3 px-4 font-medium text-right">Size (USD)</th>
                                            <th className="py-3 px-4 font-medium text-right">Spread</th>
                                            <th className="py-3 px-4 font-medium text-right">Net Profit</th>
                                            <th className="py-3 px-4 font-medium text-right">Status</th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-white/[0.04]">
                                        {isOppsLoading && dexOpps.length === 0 ? (
                                            Array.from({ length: 5 }).map((_, i) => (
                                                <tr key={`opp-skel-${i}`} className="border-b border-white/[0.04] last:border-0 hover:bg-white/[0.02] transition-colors">
                                                    <td className="py-4 px-4">
                                                        <div className="h-3 w-16 bg-white/10 rounded-sm animate-pulse" />
                                                    </td>
                                                    <td className="py-4 px-4">
                                                        <div className="flex items-center gap-2">
                                                            <div className="h-3 w-10 bg-white/10 rounded-sm animate-pulse" />
                                                            <ArrowRight className="h-2.5 w-2.5 text-white/10" />
                                                            <div className="h-3 w-10 bg-white/10 rounded-sm animate-pulse" />
                                                        </div>
                                                    </td>
                                                    <td className="py-4 px-4">
                                                        <div className="h-3 w-14 bg-white/10 rounded-sm animate-pulse ml-auto" />
                                                    </td>
                                                    <td className="py-4 px-4">
                                                        <div className="flex items-center justify-end gap-1">
                                                            <div className="h-3 w-10 bg-emerald-400/20 rounded-sm animate-pulse" />
                                                            <div className="h-2 w-6 bg-white/5 rounded-sm animate-pulse" />
                                                        </div>
                                                    </td>
                                                    <td className="py-4 px-4">
                                                        <div className="h-3 w-16 bg-emerald-400/20 rounded-sm animate-pulse ml-auto" />
                                                    </td>
                                                    <td className="py-4 px-4">
                                                        <div className="h-3 w-12 bg-white/10 rounded-sm animate-pulse ml-auto" />
                                                    </td>
                                                </tr>
                                            ))
                                        ) : dexOpps.length > 0 ? dexOpps.slice(0, 10).map((opp) => (
                                            <React.Fragment key={opp.id}>
                                                <tr onClick={() => setExpandedRow(expandedRow === opp.id ? null : opp.id)} className={`hover:bg-white/[0.04] transition-colors text-[11px] text-white/90 cursor-pointer ${expandedRow === opp.id ? 'bg-white/[0.04]' : ''}`}>
                                                    <td className="py-3 px-4 text-white/50">
                                                        {new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false }).format(opp.timestamp)}
                                                    </td>
                                                    <td className="py-3 px-4 flex items-center gap-2">
                                                        {opp.direction.split('_TO_')[0].replace('_', '-')} <ArrowRight className="h-2.5 w-2.5 text-white/30" /> {opp.direction.split('_TO_')[1]?.replace('_DELTA_BALANCE', '').replace('_', '-') ?? ''}
                                                    </td>
                                                    <td className="py-3 px-4 text-right text-white">
                                                        ${formatNumber(opp.optimal_size_usd, 0)}
                                                    </td>
                                                    <td className="py-3 px-4 text-right">
                                                        <span className={`${opp.net_spread_bps > 0 ? 'text-emerald-400/90' : 'text-red-400/90'}`}>
                                                            {opp.net_spread_bps > 0 ? '+' : ''}{opp.net_spread_bps}
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
                                                        {opp.status === 'expired' && <span className="text-[10px] uppercase tracking-wider text-white/40">Expired</span>}
                                                        {opp.status === 'executing' && <span className="text-[10px] uppercase tracking-wider text-amber-500/90">Routing</span>}
                                                        {opp.status === 'completed' && <span className="text-[10px] uppercase tracking-wider text-emerald-500/90">Secured</span>}
                                                    </td>
                                                </tr>
                                                {expandedRow === opp.id && (
                                                    <tr className="bg-black/60 border-b-0">
                                                        <td colSpan={6} className="p-0">
                                                            <div className="p-4 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-9 gap-5 border-l-2 border-emerald-500/30 bg-emerald-500/5 text-[10px] font-mono shadow-inner items-start">
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Vector ID</div>
                                                                    <div className="text-white/80">{opp.id.split('-').pop()}</div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Transfer Size</div>
                                                                    <div className="text-white">{formatNumber(opp.cngn_transferred, 2)} cNGN</div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Uni BSC Block Price</div>
                                                                    <div className="text-white">{opp.uni_bsc_price ? `$${Number(opp.uni_bsc_price).toFixed(7)}` : 'N/A'}</div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Uni Base Block Price</div>
                                                                    <div className="text-white">{opp.uni_base_price ? `$${Number(opp.uni_base_price).toFixed(7)}` : 'N/A'}</div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Asset Block Price</div>
                                                                    <div className="text-white">{opp.assetchain_price ? `$${Number(opp.assetchain_price).toFixed(7)}` : 'N/A'}</div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Slippage / Gas</div>
                                                                    <div className="text-white">{opp.slippage_tolerance_bps ? `${opp.slippage_tolerance_bps / 100}%` : 'N/A'} <span className="text-white/40">|</span> {opp.estimated_gas_usd ? `~$${opp.estimated_gas_usd}` : 'N/A'}</div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">Protocol Fees</div>
                                                                    <div className="text-white">UBSC: {opp.uni_bsc_fee_bps ? `${(opp.uni_bsc_fee_bps / 100)}%` : 'N/A'} <span className="text-white/40">|</span> UBAS: {opp.uni_base_fee_bps ? `${(opp.uni_base_fee_bps / 100)}%` : 'N/A'} <span className="text-white/40">|</span> ASST: {opp.assetchain_fee_bps ? `${(opp.assetchain_fee_bps / 100)}%` : 'N/A'}</div>
                                                                </div>
                                                                <div className="col-span-2">
                                                                    <div className="text-emerald-500/70 uppercase tracking-widest mb-1.5 text-[9px]">System Notes</div>
                                                                    <div className="text-white/70 mb-2.5">{opp.reason || 'Optimal Spread Curve Logged'}</div>

                                                                    {(opp.buy_tx_hash || opp.sell_tx_hash) && (
                                                                        <div className="flex gap-4">
                                                                            {opp.buy_tx_hash && (
                                                                                <a href={`#${opp.buy_tx_hash}`} className="flex items-center gap-1.5 text-[10px] text-blue-400 hover:text-blue-300 transition-colors uppercase tracking-wider">
                                                                                    Buy Leg TX
                                                                                </a>
                                                                            )}
                                                                            {opp.sell_tx_hash && (
                                                                                <a href={`#${opp.sell_tx_hash}`} className="flex items-center gap-1.5 text-[10px] text-purple-400 hover:text-purple-300 transition-colors uppercase tracking-wider">
                                                                                    Sell Leg TX
                                                                                </a>
                                                                            )}
                                                                        </div>
                                                                    )}
                                                                </div>
                                                            </div>
                                                        </td>
                                                    </tr>
                                                )}
                                            </React.Fragment>
                                        )) : (
                                            <tr>
                                                <td colSpan={6} className="py-10 text-center text-white/30 uppercase tracking-widest text-[11px]">
                                                    No Profitable Spreads Identified Yet
                                                </td>
                                            </tr>
                                        )}
                                    </tbody>
                                </table>
                            </div>
                        </CardContent>
                    </Card>
                    </div>
                </div>
            </div>
        </div>
    );
}
