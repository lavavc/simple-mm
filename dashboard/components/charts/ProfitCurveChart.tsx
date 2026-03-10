'use client';

import React, { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, ISeriesApi, LineData, AreaData, Time } from 'lightweight-charts';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Activity } from 'lucide-react';

interface CurvePoint {
    size: number;
    cngn_acquired: number;
    profit: number;
    profit_no_fee: number;
    profit_after_slippage: number;
    min_acceptable_usd: number;
}

interface ProfitCurveChartProps {
    data: CurvePoint[];
    optimalSize: number;
    maxProfit: number;
    isSyncing: boolean;
    direction?: string;
}

export function ProfitCurveChart({ data, optimalSize, maxProfit, isSyncing, direction }: ProfitCurveChartProps) {
    const chartContainerRef = useRef<HTMLDivElement>(null);
    const [hoverData, setHoverData] = useState<CurvePoint | null>(null);
    const [hoverPos, setHoverPos] = useState<{x: number, y: number} | null>(null);

    const isBaseToBsc = direction === 'UNI_BASE_TO_UNI_BSC_DELTA_BALANCE';
    const trajectoryText = isBaseToBsc ? 'Base → BSC' : 'BSC → Base';
    const acquireVenue = isBaseToBsc ? 'Uni Base' : 'Uni BSC';

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

        const grossSeries = chart.addAreaSeries({
            topColor: 'rgba(59, 130, 246, 0.3)',
            bottomColor: 'rgba(59, 130, 246, 0.0)',
            lineColor: 'rgba(59, 130, 246, 1)',
            lineWidth: 2,
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        });

        const netSeries = chart.addAreaSeries({
            topColor: 'rgba(16, 185, 129, 0.5)',
            bottomColor: 'rgba(16, 185, 129, 0.0)',
            lineColor: 'rgba(16, 185, 129, 1)',
            lineWidth: 3,
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        });

        const floorSeries = chart.addLineSeries({
            color: 'rgba(249, 115, 22, 0.8)',
            lineWidth: 2,
            lineStyle: 2,
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        });

        // lightweight-charts requires Date format for non-Unix strings, but we can fake an exact unix timestamp progression
        // by making the earliest entry day 1, etc.
        // Actually, you can use business days objects: { year: 2024, month: 1, day: size }
        // BUT `time: size as unknown as Time` usually works fine out of box on v4 if it's strictly increasing and formatted manually.
        // Wait, lightweight charts requires unix time > 1000000000 to be rendered properly out of the box unless formatted.
        // Best hack for abstract X-Axis like "size": Format them as daily UTC strings starting from epoch and use custom format.
        // Let's use `time: pt.size as Time` with a fallback format to be safe. Since `tickMarkFormatter` handles rendering, it should work.

        // In Lightweight Charts v4, any strictly increasing number > 0 can be used if it's treated as a timestamp. E.g. unix secs since 1970.
        // So size = 1 becomes 1s since 1970. size = 1000 = 1000s. Since we disable time formats and use formatters, it is visually just numbers!
        const d_gross = data.map(d => ({ time: d.size as Time, value: d.profit_no_fee }));
        const d_net = data.map(d => ({ time: d.size as Time, value: d.profit }));
        const d_floor = data.map(d => ({ time: d.size as Time, value: d.profit_after_slippage }));

        grossSeries.setData(d_gross);
        netSeries.setData(d_net);
        floorSeries.setData(d_floor);

        netSeries.createPriceLine({
            price: 0,
            color: 'rgba(255, 255, 255, 0.3)',
            lineWidth: 1,
            lineStyle: 1,
            axisLabelVisible: true,
            title: '',
        });

        if (maxProfit > 0) {
            netSeries.createPriceLine({
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
                <div className="flex items-center justify-between">
                    <div className="text-[12px] text-white/90 uppercase tracking-[0.2em] font-bold flex items-center gap-2">
                        <Activity className="h-4 w-4 text-emerald-400" />
                        Execution Yield Curve
                    </div>
                    {maxProfit > 0 && (
                        <div className="flex gap-2">
                            <div className="text-[9px] font-mono text-white/40 bg-white/5 px-2 py-1 rounded-sm border border-white/5 flex items-center">
                                PEAK TARGET
                            </div>
                            <div className="text-[11px] font-mono font-bold text-emerald-400 bg-emerald-500/10 px-3 py-1 rounded-sm border border-emerald-500/20">
                                +${maxProfit.toFixed(2)} @ ${optimalSize}
                            </div>
                        </div>
                    )}
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
                        
                        <div className="space-y-3">
                            <div>
                                <div className="text-[9px] text-white/30 uppercase tracking-widest mb-1.5 font-bold">Yield Projections</div>
                                <div className="flex items-center justify-between text-[11px] mb-1">
                                    <span className="text-white/60 flex items-center gap-1.5">
                                        <span className="w-1 h-1 rounded-full bg-blue-500" /> Gross Spread
                                    </span>
                                    <span className="text-white/80">
                                        {hoverData.profit_no_fee > 0 ? "+" : ""}${hoverData.profit_no_fee.toFixed(2)}
                                    </span>
                                </div>
                                <div className="flex items-center justify-between text-[11px] mb-1">
                                    <span className="text-white/60 flex items-center gap-1.5">
                                        <span className="w-1 h-1 rounded-full bg-rose-500/80" /> Protocol Fees
                                    </span>
                                    <span className="text-rose-400">
                                        -${(hoverData.profit_no_fee - hoverData.profit).toFixed(2)}
                                    </span>
                                </div>
                                <div className="flex items-center justify-between text-[11px] mb-1 border-t border-white/[0.05] pt-1 mt-1">
                                    <span className="text-white/70 flex items-center gap-1.5 font-bold">
                                        <span className="w-1 h-1 rounded-full bg-emerald-500" /> Net Expected
                                    </span>
                                    <span className={hoverData.profit > 0 ? "text-emerald-400 font-bold" : "text-white/60 font-bold"}>
                                        {hoverData.profit > 0 ? "+" : ""}${hoverData.profit.toFixed(2)}
                                    </span>
                                </div>
                                <div className="flex items-center justify-between text-[11px] mb-1">
                                    <span className="text-white/50 flex items-center gap-1.5">
                                        <span className="w-1 h-1 rounded-full bg-orange-500" /> Guaranteed Floor
                                    </span>
                                    <span className="text-white/50">
                                        {hoverData.profit_after_slippage > 0 ? "+" : ""}${hoverData.profit_after_slippage.toFixed(2)}
                                    </span>
                                </div>
                            </div>

                            <div>
                                <div className="text-[9px] text-white/30 uppercase tracking-widest mb-1.5 font-bold">Execution Trajectory</div>
                                <div className="flex items-center justify-between text-[11px] mb-1">
                                    <span className="text-white/60 flex items-center gap-1.5">
                                        Expected USD Out <span className="text-[8px] bg-white/5 px-1 rounded-sm text-white/40">{trajectoryText}</span>
                                    </span>
                                    <span className="text-white/80">${(hoverData.size + hoverData.profit).toFixed(2)}</span>
                                </div>
                                <div className="flex items-center justify-between text-[11px] mb-1">
                                    <span className="text-white/60">Min Acceptable (10bps)</span>
                                    <span className="text-orange-400/80">${hoverData.min_acceptable_usd.toFixed(2)}</span>
                                </div>
                            </div>

                            <div>
                                <div className="text-[9px] text-white/30 uppercase tracking-widest mb-1.5 font-bold">Cross-Chain Inventories</div>
                                <div className="flex items-center justify-between text-[11px] mb-1">
                                    <span className="text-white/60 flex items-center gap-1.5">
                                        <span className="w-1 h-1 rounded-full bg-purple-500" /> Acq @ {acquireVenue}
                                    </span>
                                    <span className="text-white/80">{hoverData.cngn_acquired?.toLocaleString('en-US', { maximumFractionDigits: 0 })} cNGN</span>
                                </div>
                            </div>
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
