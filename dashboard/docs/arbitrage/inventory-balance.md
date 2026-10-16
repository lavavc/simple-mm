---
title: Inventory Balance
order: 7
---

Arbitrage works best when each trading account holds enough cNGN and stablecoins to trade in either direction. Quidax, Base, and BSC therefore have independent soft targets, initially 50/50 by value. Opposite imbalances at two venues do not cancel each other out.

Each candidate trade is assessed against the projected inventory at both venues. Moving toward a target earns a preference; moving away reduces it. The preference grows with imbalance, so a less-profitable trade can win when it restores more useful inventory. Overshooting a target reduces that benefit.

Inventory preference is separate from profit. Every eligible trade must still meet the minimum expected monetary profit after trading fees and gas. Credit for balancing cannot make a losing trade eligible. Funding, exposure, loss, volume, and circuit-breaker limits remain in force. Trades may reduce an existing exposure breach, but cannot worsen it.

Only fresh balances and prices are used. Locked funds count toward inventory ownership but cannot finance new trades. Both legs must be funded from their own accounts; no bridge or transfer is assumed.

LP holdings and ladder decisions remain separate. This policy manages arb accounts only, encouraging balance through profitable trading rather than enforcing exact allocations.
