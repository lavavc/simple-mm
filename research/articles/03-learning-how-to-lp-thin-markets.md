# Learning How To LP A Thin Market

The first article in this series introduced our open-source market maker for cNGN. The second described the market it operates in: a Quidax order book that can show size but barely moves, and two Uniswap pools that move often but cannot absorb much size. This article is about the next problem. Once you decide to provide liquidity, where should you put it?

There is no clever answer hiding below. We have a simple live policy, a larger research system around it, and several results that stopped us from making the live policy more complicated. That is useful progress. In a thin market, knowing what the data cannot support matters as much as finding a strategy that looks good in a backtest.

## 1. How We Provide Liquidity Today

On Base and BSC, we provide concentrated liquidity on Uniswap V4. This means we do not spread our capital across every possible price. We choose a lower and upper price, and earn fees only while the pool trades inside that range. A narrow range puts more of our capital close to the current price, which increases fee income per dollar while it remains active. A wide range earns less efficiently, but is less likely to be left behind when the market moves.

Our range begins with each pool's own price history. The engine calculates an exponentially weighted moving average, or EWMA, which is simply an average that gives more weight to newer observations. It also estimates how much that pool has been moving. We place a range around the average and widen it when volatility is higher. We can also leave more room on one side when we expect the price to move in that direction (a parameter we call the "downside skew"). Base currently uses a range width of 2.75 standard deviations with a slight upward lean (long NGN); BSC uses 3 standard deviations and is symmetric. Both use an EWMA lambda of 0.975, which makes the estimate slow to react to a single jump.

The range is then rounded to the ticks that Uniswap accepts. If price leaves it, we do not immediately chase the market. The engine waits until price has moved another 10% beyond the boundary, then closes the position, swaps the remaining tokens into the ratio needed for the new range, and mints again. This delay is deliberate: every move costs gas, may lock in a change in inventory value, and creates another swap. Base charges traders 15 basis points per swap and BSC charges 12. Those fees are our income when we are active, but they are also a cost to anyone arbitraging the pools back into line.

Quidax's orderbook is obviously very different. There we create a ladder: limit orders to buy below a reference price and sell above it, with configurable spacing and size at each level. When the reference price moves far enough, the engine cancels the old ladder and replaces it, subject to a cooldown. This makes the trade-off easier to see. Quotes closer to the reference price are more likely to trade and make a tighter market, but leave less room for error; quotes further away trade less often but provide more protection against a stale or wrong reference. The DEX asks us to choose a continuous range. Quidax asks us to choose a set of discrete prices.

The operational plumbing tracks whether a position is in range, its token composition, its value, and its share of active liquidity. It also keeps Base and BSC separate: a position or balance on one chain does not magically make inventory available on the other. The production code is documented in [`dashboard/docs/lp`](../../dashboard/docs/lp/).

## 2. The Questions This Leaves Open

The first question is range width. A tighter range can earn a larger share of fees, but only until price leaves it. A wider range stays useful for longer, but spreads the same capital more thinly. The right answer depends on fee volume, volatility, gas, the cost of changing the token mix, and how often a range needs to be rebuilt. A range that earns the most gross fees can still be the worse position after all those costs.

The second question is how quickly the EWMA should move. If it reacts quickly, the range follows recent prices but may chase noise. If it reacts slowly, it ignores short shocks but can remain centered on an old market. The same problem applies to every parameter. When should the volatility multiplier, skew, or rebalance threshold change? A backtest can always find a different winner in each period. That does not mean the engine should change settings every week. Data for such a new, thin market is just unstable.

The third question is whether we should move before price leaves the range. If flow is strongly one-sided, or one venue has already moved, an early shift could avoid an inactive position or a bad inventory conversion. It could also turn ordinary noise into needless trading costs. The useful test is not whether an early move would have looked smart once. It is whether the signal works on later data, at the decision time, and saves more than the move costs.

Thin markets make all three questions harder. There are fewer independent observations, long periods without a price update, and occasional trades large enough to dominate a window. The price itself may be a record of one participant's activity rather than a broad market view. We therefore need to know whether Base, BSC, or Quidax moves first; whether a move persists; and whether another venue adds information beyond a pool's own history. Without that, a “fair value” can easily become the same pool price fed back into its own strategy.

## 3. How The Research Is Set Up

We keep a small research canon in [`research/literature`](../literature/). The concentrated-liquidity papers give us the mechanics of ranges, liquidity math, position lifecycles, and early exits. The market-microstructure papers cover tick data, order-book imbalance, trade direction, flow and sizing. The backtesting papers provide the most important rule: model attainable size, capital use, fees, latency, churn and drawdown, then test later periods rather than choosing a policy on the whole history.

The repo's autoresearch work lives in [`research/autoresearch`](../autoresearch/). It is a set of explicit research briefs, datasets, scripts, result manifests and decision gates that let an automated research loop test a narrow hypothesis, record what happened, and pass the result into the next experiment. We point it at the raw Uniswap V4 histories, reconstructed marginal prices, Quidax snapshots and costed LP backtester. We're careful to specify that evidence is required before a result could change the live engine.

We gave this system four LP problems. 

1. **Range selection:** test EWMA and fixed-range policies across widths, smoothing speeds, skews and exit rules, including fees and position-management costs. 
2. **Parameter change:** use walk-forward windows and parameter-stability checks to distinguish a repeatable setting from a different in-sample winner every time.

The other two problems connect LP decisions to market structure.

3. **Thin-market signals:** test whether past swap flow, fee intensity, volatility and liquidity conditions justify entering, skewing or leaving a range early. 
4. **Cross-venue information:** test whether Base, BSC or Quidax tends to move first, and whether the other venue's price improves a forecast or a costed LP decision. Each pool is evaluated separately because its fee tier, activity and token order differ.

There are strict limits. Historical V4 files contain two old price methodologies, so research uses `sqrt_price_x96` as the canonical marginal pool price and separates periods around the known methodology boundaries. All features are built “as of” the decision timestamp, with the age of each source recorded. Random train/test splits are avoided in favor of walk-forward tests. Most importantly, the DEX cannot serve as both the input and an independent truth label. We do not yet have a clean external cNGN price series covering the relevant period, so several results remain diagnostic rather than deployable.

## 4. What We Have Learned So Far

Our first lead-lag study found a clear-looking pattern in 99 days of one-minute venue snapshots. Base led BSC at a peak lag of three minutes, with a correlation of 0.086 and a block-shuffle p-value below 0.01. The result was positive in all three sub-periods. Quidax did not lead either pool: its correlations were near zero, and its midpoint changed in only 161 of 142,568 minutes. When Quidax did re-quote, it moved in the direction of Base's prior move 69% of the time over 15 minutes, 66% over 30 minutes, and 63% over 60 minutes, although the event counts were small.

The trade-level follow-up made that pattern more concrete. After a Base swap moved its pool by at least 10 basis points, BSC recovered roughly 21–24% of the move within three minutes and 49–55% within 30 minutes, depending on the implementation. In the reverse direction, Base recovered only about 1% within three minutes and 4% within 30. Base moves also tended to continue, while BSC moves tended to reverse. Removing all 186 swaps linked to our own engine did not materially change the result. Quidax re-quotes were different again: matched responses came either after roughly 19–48 minutes or after 6–22 hours, and 78% of all quote changes occurred between 08:00 and 16:59 Lagos time. The book behaved like a manually maintained market, not a continuous source of price discovery.

That sounds like Base is the leader. It is a useful working description of this sample, but not sufficient to lable as a stable rule. An expanding walk-forward model tested whether adding one pool's price improved forecasts of the other. It did not. At the one-hour horizon, adding BSC made the Base forecast worse by 0.242 basis points of mean absolute error, while adding Base made the BSC forecast worse by 0.587 basis points. Alternative linear, autoregressive, nonlinear and outlier-resistant models did not rescue the result. Timing estimates also changed sign across weeks and alignment choices. Our current perspective is therefore narrower: the pools co-move, but directional leadership remains largely unresolved.

The short-horizon extension reinforces that caution. At 15 minutes, only 31.7% of BSC-to-Base shocks and 32.0% of Base-to-BSC shocks had a new target-pool observation. Roughly two-thirds had no recorded update at all. Mean responses were small and every simultaneous confidence band included zero. This is the basic thin-market problem in numbers: a visible lag may reflect information moving slowly, or simply one venue not trading.

The LP backtests gave us another reason not to overfit. We tested 2,640 EWMA combinations and 2,240 paper-style combinations. The “best” settings changed almost every walk-forward window: Base selected 26 distinct EWMA tuples in 26 windows, while BSC selected 25 in 37. After costs, every rank-one stream lost money: from -0.460% for Base paper-style LP to -2.086% for BSC paper-style LP. A smaller directional policy looked promising on Base, returning +1.039% across four active windows, but the same rule returned -1.272% across seven BSC windows. It did not transfer.

Finally, we tested whether the cross-pool forecast could at least improve the Base LP policy by deciding when it was allowed to enter. It could not. Across the same 23 reset-capital windows, the original policy returned +0.515% and the forecast-gated version returned +0.310%; the gated version also had a worse worst window. A later joint-portfolio test passed its file-integrity checks but failed its pre-set completeness gate, so there is no portfolio return to report.

## Conclusion

The practical finding is simple. Base, BSC and Quidax are not interchangeable copies of one market. Base often looked like the first mover, BSC often looked like the follower, and Quidax updated on a human schedule. But that pattern has not yet produced a stable forecasting rule or a better LP policy after costs. We have therefore kept the live engine simple: venue-local ranges, slow-moving estimates, explicit costs, and conservative reranging. 

The more complicated ideas remain in research until they work on later data, across the relevant venues, and against an independent measure of value. In a thin market, restraint is part of the strategy.

We're publishing our thinking in full here so that you can challenge it, and use it to inform whatever you do to improve the current code. Our goal is not just to be an efficient market maker for cNGN: it is to create a template that serves as the lower bard for market makers for any EM stablecoin, and show regulators how market makers work and why this leads to better, more transparent, more fair markets.

In using, open sourcing, and explaining what we have done with the most sophisticated tools that are currently available, we hope to contribute to geographic decentralisation, as this is the critical ingredient required for global networks that truly are capable of producing value for the people who choose to use them.
