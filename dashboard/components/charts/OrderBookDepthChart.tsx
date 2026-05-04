'use client';

import React, { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, Time } from 'lightweight-charts';

export interface CurvePointV2 {
  size: number;
  base_to_bsc: {
    cngn_acquired: number;
    profit: number;
    profit_after_slippage: number;
    min_acceptable_usd: number;
    usdt_out: number;
  };
  bsc_to_base: {
    cngn_acquired: number;
    profit: number;
    profit_after_slippage: number;
    min_acceptable_usd: number;
    usdt_out: number;
  };
}

interface OrderBookDepthChartProps {
  curveCexToDex?: CurvePointV2[];
  curveDexToCex?: CurvePointV2[];
  direction?: string;
  optimalSize?: number;
}

export function OrderBookDepthChart({ curveCexToDex, curveDexToCex, direction, optimalSize }: OrderBookDepthChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const [hoverData, setHoverData] = useState<CurvePointV2 | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);
  const [activeDirection, setActiveDirection] = useState<'CEX_TO_DEX' | 'DEX_TO_CEX'>('DEX_TO_CEX');

  const curveData = activeDirection === 'CEX_TO_DEX' ? curveCexToDex : curveDexToCex;

  useEffect(() => {
    if (!chartContainerRef.current || !curveData || curveData.length < 2 || !direction) return;

    const bscPoints: { time: Time; value: number }[] = [];
    const bscSecondaryPoints: { time: Time; value: number }[] = []; 
    const basePoints: { time: Time; value: number }[] = [];
    const baseSecondaryPoints: { time: Time; value: number }[] = [];

    for (const point of curveData) {
      if (point.size < 0) continue;
      const t = point.size as Time;
      const usdIn = point.size;

      const bscProfit = point.base_to_bsc?.profit ?? 0;
      const baseProfit = point.bsc_to_base?.profit ?? 0;
      const bscMin = point.base_to_bsc?.min_acceptable_usd ?? usdIn;
      const baseMin = point.bsc_to_base?.min_acceptable_usd ?? usdIn;

      bscPoints.push({ time: t, value: bscProfit });
      bscSecondaryPoints.push({ time: t, value: bscMin - usdIn });
      
      basePoints.push({ time: t, value: baseProfit });
      baseSecondaryPoints.push({ time: t, value: baseMin - usdIn });
    }

    if (bscPoints.length === 0 && basePoints.length === 0) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: 'rgba(255,255,255,0.5)',
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.05)' },
        horzLines: { color: 'rgba(255,255,255,0.05)' },
      },
      rightPriceScale: {
        autoScale: true,
        borderVisible: false,
        scaleMargins: { top: 0.15, bottom: 0.05 },
        minimumWidth: 70,
      },
      timeScale: {
        borderVisible: false,
        fixLeftEdge: true,
        fixRightEdge: true,
        tickMarkFormatter: (t: number) => `$${t >= 1000 ? (t / 1000).toFixed(1) + 'k' : t}`,
      },
      localization: {
        timeFormatter: (t: number) => `$${t.toLocaleString()}`,
        priceFormatter: (p: number) => `$${p.toFixed(2)}`,
      },
      crosshair: {
        mode: 1,
        vertLine: { color: 'rgba(255,255,255,0.25)', width: 1, style: 3 },
        horzLine: { color: 'rgba(255,255,255,0.25)', width: 1, style: 3 },
      },
    });

    const bscSeries = chart.addLineSeries({
      color: 'rgba(59, 130, 246, 1)', // Blue
      lineWidth: 2,
    });
    bscSeries.setData(bscPoints);

    const baseSeries = chart.addLineSeries({
      color: 'rgba(139, 92, 246, 1)', // Violet
      lineWidth: 2,
    });
    baseSeries.setData(basePoints);

    const bscWorstSeries = chart.addLineSeries({
      color: 'rgba(59, 130, 246, 0.4)', // Faded Blue
      lineWidth: 2,
      lineStyle: 2, // Dashed
    });
    bscWorstSeries.setData(bscSecondaryPoints);

    const baseWorstSeries = chart.addLineSeries({
      color: 'rgba(139, 92, 246, 0.4)', // Faded Violet
      lineWidth: 2,
      lineStyle: 2, // Dashed
    });
    baseWorstSeries.setData(baseSecondaryPoints);

    if (optimalSize && optimalSize > 0) {
      const optCurvePoint = curveData.find(d => d.size >= optimalSize) || curveData[curveData.length - 1];
      if (optCurvePoint) {
        const bscProfit = optCurvePoint.base_to_bsc?.profit ?? Number.NEGATIVE_INFINITY;
        const baseProfit = optCurvePoint.bsc_to_base?.profit ?? Number.NEGATIVE_INFINITY;

        const winningRoute =
          bscProfit >= baseProfit
            ? { label: 'Uniswap BSC', profit: bscProfit }
            : { label: 'Uniswap Base', profit: baseProfit };

        if (winningRoute.profit > 0) {
          (winningRoute.label === 'Uniswap BSC' ? bscSeries : baseSeries).createPriceLine({
            price: winningRoute.profit,
            color: 'rgba(250,204,21,0.5)',
            lineWidth: 1,
            lineStyle: 3,
            axisLabelVisible: true,
            title: `Optimal ${winningRoute.label} $${optimalSize}`,
          });
        }
      }
    }

    // Add a horizontal line at y=0 to clearly mark the breakeven threshold
    bscSeries.createPriceLine({
      price: 0,
      color: 'rgba(255, 255, 255, 0.25)',
      lineWidth: 1,
      lineStyle: 2, // Dashed
      axisLabelVisible: true,
      title: 'Breakeven',
    });

    chart.timeScale().fitContent();

    const handleCrosshairMove = (param: any) => {
      // param.time might be 1 but point might be out of bounds theoretically, rely on param.time existence
      if (param.time === undefined || param.time === null) {
        setHoverData(null);
        setHoverPos(null);
        return;
      }
      const size = param.time as number;
      const pt = curveData.find(d => d.size === size);
      if (pt) {
        setHoverData(pt);
        const cw = chartContainerRef.current!.clientWidth;
        const tw = 320; 
        const px = param.point?.x ?? (size === pt.size && size === 1 ? 0 : cw); // fallback for edge cases
        const py = param.point?.y ?? 100;
        const offset = px > cw / 2 ? -tw - 20 : 20;
        setHoverPos({
          x: px + offset,
          y: Math.max(10, py - 100),
        });
      } else {
        setHoverData(null);
        setHoverPos(null);
      }
    };

    chart.subscribeCrosshairMove(handleCrosshairMove);

    const handleResize = () => {
      if (chartContainerRef.current) chart.applyOptions({ width: chartContainerRef.current.clientWidth });
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      try {
        chart.unsubscribeCrosshairMove(handleCrosshairMove);
        chart.remove();
      } catch (e) {
          // ignore cleanup errors on fast remounts
      }
    };
  }, [curveData, direction, optimalSize]);

  if (!curveData || curveData.length === 0) {
    return (
      <div className="w-full h-full flex flex-col items-center justify-center gap-2 bg-[#12161C] border border-white/[0.05] rounded-sm">
        <div className="h-5 w-5 border-2 border-emerald-500/30 border-t-emerald-500 rounded-full animate-spin" />
        <span className="text-[9px] font-mono text-white/25 tracking-widest uppercase">Waiting for arb data...</span>
      </div>
    );
  }

  return (
    <div className="w-full h-full flex flex-col relative bg-[#12161C] border border-white/[0.05] rounded-sm shadow-xl">
      <div className="pt-4 px-4 pb-2 z-40 flex flex-col gap-4 pointer-events-auto border-b border-white/[0.02]">
        {/* Sleek Toggle */}
        <div className="flex items-center bg-black/30 backdrop-blur-md border border-white/5 rounded-full p-1 w-fit shadow-lg transition-all hover:bg-black/50">
          <button
            onClick={() => setActiveDirection('DEX_TO_CEX')}
            className={`px-4 py-1.5 text-[9px] font-mono tracking-widest uppercase transition-all rounded-full ${activeDirection === 'DEX_TO_CEX' ? 'bg-white/10 text-white font-bold' : 'text-white/40 hover:text-white/70'}`}
          >
            Buy DEX, Sell Quidax
          </button>
          <button
            onClick={() => setActiveDirection('CEX_TO_DEX')}
            className={`px-4 py-1.5 text-[9px] font-mono tracking-widest uppercase transition-all rounded-full ${activeDirection === 'CEX_TO_DEX' ? 'bg-white/10 text-white font-bold' : 'text-white/40 hover:text-white/70'}`}
          >
            Buy Quidax, Sell DEX
          </button>
        </div>

        {/* Legend */}
        <div className="flex flex-col gap-1.5 pl-2">
          <span className="text-[9px] font-mono tracking-[0.2em] text-white/35 uppercase">
            Net Profit (Expected vs 10bps Floor)
          </span>
          <div className="flex flex-wrap items-center gap-4 text-[9px] font-mono tracking-widest">
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-4 rounded-full bg-blue-500" />
              <span className="text-white/40">Uni BSC</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-0.5 w-4 bg-blue-500/40 border-t border-dashed border-blue-500" />
              <span className="text-white/30">BSC Floor</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-4 rounded-full bg-violet-500" />
              <span className="text-white/40">Uni Base</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-0.5 w-4 bg-violet-500/40 border-t border-dashed border-violet-500" />
              <span className="text-white/30">Base Floor</span>
            </span>
          </div>
        </div>
      </div>

      <div ref={chartContainerRef} className="flex-1 w-full min-h-0 relative" />

      <div className="absolute bottom-2 left-4 text-[8px] font-mono shrink-0 pointer-events-none">
        <span className="text-white/20 uppercase tracking-widest">X: Size (USD) — Y: Expected Profit (USD)</span>
      </div>

      {hoverData && hoverPos && hoverData.base_to_bsc && hoverData.bsc_to_base && (
        <div 
          className="absolute z-50 bg-[#141923]/95 backdrop-blur-md border border-white/5 rounded-sm shadow-2xl pointer-events-none p-5 min-w-[320px]"
          style={{ left: hoverPos.x, top: hoverPos.y }}
        >
          <div className="flex justify-between items-center mb-5 border-b border-white/10 pb-3">
            <span className="text-[10px] font-mono text-white/50 tracking-widest uppercase">TRADE VECTOR SIZE</span>
            <span className="text-sm font-mono text-white font-bold">${hoverData.size.toFixed(0)}</span>
          </div>

          <div className="space-y-6">
            {/* BSC */}
            <div className="space-y-1.5">
               <div className="flex items-center justify-between mb-2">
                 <span className="text-[10px] font-mono text-blue-400 font-bold uppercase tracking-widest">UNISWAP BSC</span>
                 <span className={`text-[11px] font-mono font-bold ${hoverData.base_to_bsc.profit >= 0 ? 'text-emerald-400' : 'text-white'}`}>
                    {hoverData.base_to_bsc.profit >= 0 ? '+' : ''}${hoverData.base_to_bsc.profit.toFixed(2)}
                 </span>
               </div>
               <div className="flex items-center justify-between text-[10px] font-mono text-white/50 border-white/5 pl-2 ml-1 pt-1 border-t border-dashed mt-1">
                 <span className="flex items-center gap-1.5"><span className="h-1 w-1 bg-orange-500 rounded-full" /> Guaranteed Floor</span>
                 <span className="text-orange-400">${hoverData.base_to_bsc.min_acceptable_usd.toFixed(2)}</span>
               </div>
            </div>

            {/* Base */}
            <div className="space-y-1.5 pt-2 border-t border-white/5">
               <div className="flex items-center justify-between mb-2">
                 <span className="text-[10px] font-mono text-violet-400 font-bold uppercase tracking-widest">UNISWAP BASE</span>
                 <span className={`text-[11px] font-mono font-bold ${hoverData.bsc_to_base.profit >= 0 ? 'text-emerald-400' : 'text-white'}`}>
                    {hoverData.bsc_to_base.profit >= 0 ? '+' : ''}${hoverData.bsc_to_base.profit.toFixed(2)}
                 </span>
               </div>
               <div className="flex items-center justify-between text-[10px] font-mono text-white/50 border-white/5 pl-2 ml-1 pt-1 border-t border-dashed mt-1">
                 <span className="flex items-center gap-1.5"><span className="h-1 w-1 bg-orange-500 rounded-full" /> Guaranteed Floor</span>
                 <span className="text-orange-400">${hoverData.bsc_to_base.min_acceptable_usd.toFixed(2)}</span>
               </div>
            </div>

            {/* Common */}
            <div className="pt-3 border-t border-white/10 mt-2">
                <div className="flex justify-between text-[10px] font-mono items-center">
                    <span className="text-white/40 uppercase tracking-widest flex items-center gap-1.5">
                       <span className="h-1 w-1 bg-purple-500 rounded-full" /> cNGN Transferred
                    </span>
                    <span className="text-white font-bold">{hoverData.base_to_bsc.cngn_acquired.toLocaleString(undefined, { maximumFractionDigits: 0 })} cNGN</span>
                </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
