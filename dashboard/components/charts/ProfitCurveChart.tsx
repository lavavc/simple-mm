'use client';

import React, { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, ISeriesApi, LineData, AreaData, Time } from 'lightweight-charts';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Activity } from 'lucide-react';
import { formatNumber } from '@/lib/utils';

export interface CurvePoint {
    size: number;
    base_to_bsc?: {
        cngn_acquired: number;
        profit: number;
        profit_no_fee: number;
        profit_after_slippage: number;
        min_acceptable_usd: number;
    };
    bsc_to_base?: {
        cngn_acquired: number;
        profit: number;
        profit_no_fee: number;
        profit_after_slippage: number;
        min_acceptable_usd: number;
    };
    // Legacy fallback bindings
    profit?: number;
    profit_no_fee?: number;
    profit_after_slippage?: number;
    min_acceptable_usd?: number;
    cngn_acquired?: number;
}

interface ProfitCurveChartProps {
    data: CurvePoint[];
    optimalSize: number;
    maxProfit: number;
    isSyncing: boolean;
    direction?: string;
    onHoverPoint?: (pt: CurvePoint | null) => void;
}

export function ProfitCurveChart({ data, optimalSize, maxProfit, isSyncing, direction, onHoverPoint }: ProfitCurveChartProps) {
    const chartContainerRef = useRef<HTMLDivElement>(null);
    const [hoverData, setHoverData] = useState<CurvePoint | null>(null);
    const [hoverPos, setHoverPos] = useState<{x: number, y: number} | null>(null);

    const isBaseToBsc = direction === 'UNI_BASE_TO_UNI_BSC_DELTA_BALANCE';
    const isBscToBase = direction === 'UNI_BSC_TO_UNI_BASE_DELTA_BALANCE';
    const isQuidaxToBsc = direction === 'QUIDAX_TO_UNI_BSC';
    const isQuidaxToBase = direction === 'QUIDAX_TO_UNI_BASE';
    const isBscToQuidax = direction === 'UNI_BSC_TO_QUIDAX';
    const isBaseToQuidax = direction === 'UNI_BASE_TO_QUIDAX';

    let trajectoryText = isBaseToBsc ? 'Base → BSC' : 'BSC → Base';
    let acquireVenue = isBaseToBsc ? 'Uni Base' : 'Uni BSC';
    
    if (isQuidaxToBsc) {
        trajectoryText = 'Quidax → BSC';
        acquireVenue = 'Quidax L2';
    } else if (isQuidaxToBase) {
        trajectoryText = 'Quidax → Base';
        acquireVenue = 'Quidax L2';
    } else if (isBscToQuidax) {
        trajectoryText = 'BSC → Quidax';
        acquireVenue = 'Uni BSC';
    } else if (isBaseToQuidax) {
        trajectoryText = 'Base → Quidax';
        acquireVenue = 'Uni Base';
    }

    useEffect(() => {
        if (!chartContainerRef.current || isSyncing || !data || data.length === 0) return;

        const chart = createChart(chartContainerRef.current, {
            layout: {
                background: { type: ColorType.Solid, color: 'transparent' },
                textColor: 'rgba(255, 255, 255, 0.5)',
            },
            grid: {
                vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
                horzLines: { color: 'rgba(255, 255, 255, 0.05)' },
            },
            rightPriceScale: {
                borderVisible: false,
                scaleMargins: { top: 0.1, bottom: 0.1 },
            },
            timeScale: {
                borderVisible: false,
                timeVisible: true,
                secondsVisible: false,
                tickMarkFormatter: (time: number) => `$${time}`,
                fixLeftEdge: true,
                fixRightEdge: true,
            },
            localization: {
                timeFormatter: (time: number) => `$${time}`,
            },
            crosshair: {
                mode: 1, // Magnet
                vertLine: {
                    color: 'rgba(255, 255, 255, 0.4)',
                    width: 1,
                    style: 3, 
                },
                horzLine: {
                    color: 'rgba(255, 255, 255, 0.4)',
                    width: 1,
                    style: 3,
                },
            },
            handleScroll: false,
            handleScale: false,
        });

        const bscSeries = chart.addLineSeries({
            color: 'rgba(59, 130, 246, 1)', // Blue for Base -> BSC
            lineWidth: 2,
            priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
        });

        const bscFloorSeries = chart.addLineSeries({
            color: 'rgba(59, 130, 246, 0.4)',
            lineWidth: 2,
            lineStyle: 2,
            priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
        });

        const baseSeries = chart.addLineSeries({
            color: 'rgba(139, 92, 246, 1)', // Purple for BSC -> Base
            lineWidth: 2,
            priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
        });

        const baseFloorSeries = chart.addLineSeries({
            color: 'rgba(139, 92, 246, 0.4)',
            lineWidth: 2,
            lineStyle: 2,
            priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
        });

        const d_bsc = data.map(d => ({ time: d.size as Time, value: d.base_to_bsc ? d.base_to_bsc.profit : (d.profit || 0) }));
        const d_bsc_floor = data.map(d => ({ time: d.size as Time, value: d.base_to_bsc ? d.base_to_bsc.profit_after_slippage : (d.profit_after_slippage || 0) }));
        
        const d_base = data.map(d => ({ time: d.size as Time, value: d.bsc_to_base ? d.bsc_to_base.profit : (d.profit || 0) }));
        const d_base_floor = data.map(d => ({ time: d.size as Time, value: d.bsc_to_base ? d.bsc_to_base.profit_after_slippage : (d.profit_after_slippage || 0) }));

        bscSeries.setData(d_bsc);
        bscFloorSeries.setData(d_bsc_floor);
        if (data[0]?.bsc_to_base) {
            baseSeries.setData(d_base);
            baseFloorSeries.setData(d_base_floor);
        }

        bscSeries.createPriceLine({
            price: 0,
            color: 'rgba(255, 255, 255, 0.3)',
            lineWidth: 1,
            lineStyle: 1,
            axisLabelVisible: true,
            title: '',
        });

        if (maxProfit > 0) {
            bscSeries.createPriceLine({
                price: maxProfit,
                color: 'rgba(16, 185, 129, 0.8)',
                lineWidth: 1,
                lineStyle: 3,
                axisLabelVisible: true,
                title: 'Peak',
            });
        }

        chart.timeScale().fitContent();

        const handleCrosshairMove = (param: any) => {
            if (!param.point || !param.time || param.point.x < 0 || param.point.x > chartContainerRef.current!.clientWidth || param.point.y < 0 || param.point.y > chartContainerRef.current!.clientHeight) {
                setHoverData(null);
                setHoverPos(null);
                return;
            }
            const size = param.time as number;
            const pt = data.find(d => d.size === size);
            if (pt) {
                setHoverData(pt);
                if (onHoverPoint) onHoverPoint(pt);
                // Position tooltip dynamically to the left or right depending on cursor position
                const chartWidth = chartContainerRef.current!.clientWidth;
                const tooltipWidth = 280; // approximate width of the tooltip
                const padding = 20;

                let newX = param.point.x + padding;
                // If cursor is on the right half, flip tooltip to the left of the cursor
                if (param.point.x > chartWidth / 2) {
                    newX = param.point.x - tooltipWidth - padding;
                }

                setHoverPos({
                    x: newX,
                    y: Math.max(10, Math.min(param.point.y - 150, chartContainerRef.current!.clientHeight - 300))
                });
            } else {
                setHoverData(null);
                setHoverPos(null);
                if (onHoverPoint) onHoverPoint(null);
            }
        };

        chart.subscribeCrosshairMove(handleCrosshairMove);

        const handleResize = () => {
            if(chartContainerRef.current) {
                chart.applyOptions({ width: chartContainerRef.current.clientWidth });
            }
        };
        window.addEventListener('resize', handleResize);

        return () => {
            window.removeEventListener('resize', handleResize);
            chart.unsubscribeCrosshairMove(handleCrosshairMove);
            chart.remove();
        };

    }, [data, isSyncing, maxProfit]);

    if (isSyncing || !data || data.length === 0) {
        return (
            <Card className="bg-[#12161C] border border-white/[0.05] rounded-sm shadow-none h-full min-h-[300px] flex flex-col justify-center items-center">
                <div className="h-6 w-6 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin mb-3" />
                <div className="text-[10px] text-emerald-500/50 uppercase tracking-widest font-mono animate-pulse">
                    Computing Arbitrage Vectors...
                </div>
            </Card>
        );
    }

    return (
        <Card className="bg-[#12161C] border border-[#12161C] rounded-sm shadow-none h-full flex flex-col relative overflow-hidden ring-1 ring-white/[0.05]">
            <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[80%] h-[100px] bg-emerald-500/5 blur-[50px] pointer-events-none" />

            <CardHeader className="p-4 border-b border-white/[0.05] flex-none relative z-10 bg-black/40">
                <div className="flex items-center justify-between mb-3">
                    <div className="text-[12px] text-white/90 uppercase tracking-[0.2em] font-bold flex items-center gap-2">
                        <Activity className="h-4 w-4 text-emerald-400" />
                        Execution Yield Curve
                    </div>
                    {maxProfit > 0 && (
                        <div className="flex gap-2 relative z-50">
                            <div className="text-[9px] font-mono text-white/40 bg-white/5 px-2 py-1 rounded-sm border border-white/5 flex items-center">
                                PEAK TARGET
                            </div>
                            <div className="text-[11px] font-mono font-bold text-emerald-400 bg-emerald-500/10 px-3 py-1 rounded-sm border border-emerald-500/20 pointer-events-auto">
                                +${maxProfit.toFixed(2)} @ ${optimalSize}
                            </div>
                        </div>
                    )}
                </div>
                {/* Legend */}
                <div className="flex flex-col gap-1.5 pl-2 z-40 relative pointer-events-auto">
                  <span className="text-[9px] font-mono tracking-[0.2em] text-white/35 uppercase">
                    Net Profit (Expected vs 10bps Floor)
                  </span>
                  <div className="flex flex-wrap items-center gap-4 text-[9px] font-mono tracking-widest">
                    <span className="flex items-center gap-1.5">
                      <span className="h-1.5 w-4 rounded-full bg-blue-500" />
                      <span className="text-white/40">Base → BSC</span>
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-0.5 w-4 bg-blue-500/40 border-t border-dashed border-blue-500" />
                      <span className="text-white/30">BSC Floor</span>
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-1.5 w-4 rounded-full bg-violet-500" />
                      <span className="text-white/40">BSC → Base</span>
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-0.5 w-4 bg-violet-500/40 border-t border-dashed border-violet-500" />
                      <span className="text-white/30">Base Floor</span>
                    </span>
                  </div>
                </div>
            </CardHeader>
            <CardContent className="p-0 flex-1 relative z-10 overflow-hidden">
                <div ref={chartContainerRef} className="w-full h-full min-h-[300px]" />
                
                {hoverData && hoverPos && (
                    <div 
                        className="absolute bg-[#12161C]/95 border border-white/[0.08] p-3 rounded shadow-[0_8px_32px_rgba(0,0,0,0.8)] backdrop-blur-md w-[260px] pointer-events-none z-50 transition-none font-mono tracking-tight"
                        style={{ left: hoverPos.x, top: hoverPos.y }}
                    >
                        <div className="flex justify-between items-center border-b border-white/10 pb-2 mb-2">
                            <span className="text-[10px] text-white/50 uppercase tracking-widest">Trade Vector Size</span>
                            <span className="text-[12px] text-white font-bold">${hoverData.size}</span>
                        </div>
                        
                        <div className="space-y-6">
                            {/* Base to BSC (Blue) */}
                            {hoverData.base_to_bsc && (
                                <div className="space-y-1.5">
                                   <div className="flex items-center justify-between mb-2">
                                     <span className="text-[10px] font-mono text-blue-400 font-bold uppercase tracking-widest">Base → BSC</span>
                                     <span className={`text-[11px] font-mono font-bold ${hoverData.base_to_bsc.profit >= 0 ? 'text-emerald-400' : 'text-white'}`}>
                                        {hoverData.base_to_bsc.profit >= 0 ? '+' : ''}${hoverData.base_to_bsc.profit.toFixed(2)}
                                     </span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/40 border-l-[2px] border-white/5 pl-3 ml-1">
                                     <span>Gross Spread</span>
                                     <span>{hoverData.base_to_bsc.profit_no_fee >= 0 ? '+' : ''}${hoverData.base_to_bsc.profit_no_fee.toFixed(2)}</span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/40 border-l-[2px] border-white/5 pl-3 ml-1 pb-1">
                                     <span>Protocol Fees</span>
                                     <span className="text-red-400/80">-${(hoverData.base_to_bsc.profit_no_fee - hoverData.base_to_bsc.profit).toFixed(2)}</span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/50 border-white/5 pl-2 ml-1 pt-1 border-t border-dashed mt-1">
                                     <span className="flex items-center gap-1.5"><span className="h-1 w-1 bg-orange-500 rounded-full" /> Guaranteed Floor</span>
                                     <span className="text-orange-400">${hoverData.base_to_bsc.min_acceptable_usd.toFixed(2)}</span>
                                   </div>
                                </div>
                            )}

                            {/* BSC to Base (Purple) */}
                            {hoverData.bsc_to_base && (
                                <div className="space-y-1.5 pt-2 border-t border-white/5">
                                   <div className="flex items-center justify-between mb-2">
                                     <span className="text-[10px] font-mono text-violet-400 font-bold uppercase tracking-widest">BSC → Base</span>
                                     <span className={`text-[11px] font-mono font-bold ${hoverData.bsc_to_base.profit >= 0 ? 'text-emerald-400' : 'text-white'}`}>
                                        {hoverData.bsc_to_base.profit >= 0 ? '+' : ''}${hoverData.bsc_to_base.profit.toFixed(2)}
                                     </span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/40 border-l-[2px] border-white/5 pl-3 ml-1">
                                     <span>Gross Spread</span>
                                     <span>{hoverData.bsc_to_base.profit_no_fee >= 0 ? '+' : ''}${hoverData.bsc_to_base.profit_no_fee.toFixed(2)}</span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/40 border-l-[2px] border-white/5 pl-3 ml-1 pb-1">
                                     <span>Protocol Fees</span>
                                     <span className="text-red-400/80">-${(hoverData.bsc_to_base.profit_no_fee - hoverData.bsc_to_base.profit).toFixed(2)}</span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/50 border-white/5 pl-2 ml-1 pt-1 border-t border-dashed mt-1">
                                     <span className="flex items-center gap-1.5"><span className="h-1 w-1 bg-orange-500 rounded-full" /> Guaranteed Floor</span>
                                     <span className="text-orange-400">${hoverData.bsc_to_base.min_acceptable_usd.toFixed(2)}</span>
                                   </div>
                                </div>
                            )}
                            
                            {/* Fallback for legacy single curve display */}
                            {!hoverData.base_to_bsc && !hoverData.bsc_to_base && (
                                <div className="space-y-1.5">
                                   <div className="flex items-center justify-between mb-2">
                                     <span className="text-[10px] font-mono text-blue-400 font-bold uppercase tracking-widest">Target Path</span>
                                     <span className={`text-[11px] font-mono font-bold ${(hoverData.profit || 0) >= 0 ? 'text-emerald-400' : 'text-white'}`}>
                                        {(hoverData.profit || 0) >= 0 ? '+' : ''}${(hoverData.profit || 0).toFixed(2)}
                                     </span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/40 border-l-[2px] border-white/5 pl-3 ml-1">
                                     <span>Gross Spread</span>
                                     <span>{(hoverData.profit_no_fee || 0) >= 0 ? '+' : ''}${(hoverData.profit_no_fee || 0).toFixed(2)}</span>
                                   </div>
                                   <div className="flex items-center justify-between text-[10px] font-mono text-white/40 border-l-[2px] border-white/5 pl-3 ml-1 pb-1">
                                     <span>Protocol Fees</span>
                                     <span className="text-red-400/80">-${((hoverData.profit_no_fee || 0) - (hoverData.profit || 0)).toFixed(2)}</span>
                                   </div>
                                </div>
                            )}
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
