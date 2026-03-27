"""
Portfolio valuation: mark-to-market USD value of cNGN holdings across all venues.

CEX venues use an order book walk for realistic fill simulation.
DEX venues use AMM swap math against the cached pool state.

Adding a new CEX venue: call cex_holdings_value with the venue's order book and fee.
Adding a new DEX venue: call dex_holdings_value with the pool's cached state.
"""
from decimal import Decimal
from engine.core.arbitrage.pool_state import get_cached_pool_state, swap_token0_for_token1, swap_token1_for_token0
from engine.core.arbitrage.cex_dex import walk_orderbook_asks, QUIDAX_FEE


def cex_holdings_value(order_book_asks: list, cngn_amount: Decimal, fee: Decimal) -> Decimal:
    """USD value of cNGN holdings if sold into a CEX order book."""
    value, _ = walk_orderbook_asks(order_book_asks, cngn_amount, fee)
    return value


def dex_holdings_value(
    cngn_amount: Decimal,
    sqrt_p: Decimal,
    liquidity: Decimal,
    fee: Decimal,
    token0_decimals: int,
    token1_decimals: int,
    cngn_is_token0: bool,
) -> Decimal:
    """USD value of cNGN holdings if swapped on an AMM pool."""
    swap_fn = swap_token0_for_token1 if cngn_is_token0 else swap_token1_for_token0
    return swap_fn(cngn_amount, sqrt_p, liquidity, fee, token0_decimals, token1_decimals)


def portfolio_value(quidax_depth, balances: list, cex_fee: Decimal = QUIDAX_FEE) -> dict:
    """
    Mark-to-market USD value of all cNGN holdings across every venue.
    Returns a flat dict compatible with the liquidation_valuation broadcast format.
    """
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    result = {
        "quidax_cngn_usd": 0.0,
        "quidax_usdt": 0.0,
        "uni_bsc_cngn_usd": 0.0,
        "uni_bsc_usdt": 0.0,
        "uni_base_cngn_usd": 0.0,
        "uni_base_usdc": 0.0,
    }

    if not balances:
        return result

    # Quidax CEX valuation
    quidax_bal = next((b for b in balances if b.role == "quidax-exchange"), None)
    if quidax_bal:
        result["quidax_usdt"] = float(quidax_bal.token_balances.get("USDT", Decimal(0)))
        if quidax_depth and quidax_depth.asks:
            asks = sorted(quidax_depth.asks, key=lambda x: x.price)
            bids = sorted(quidax_depth.bids, key=lambda x: x.price, reverse=True) if quidax_depth.bids else []
            q_cngn = quidax_bal.token_balances.get("cNGN", Decimal(0))
            if q_cngn > 0:
                result["quidax_cngn_usd"] = float(cex_holdings_value(asks, q_cngn, cex_fee))
            if bids and asks:
                result["quidax_mid"] = float(Decimal("1") / ((bids[0].price + asks[0].price) / 2))

    # Uniswap BSC: token0=USDT, token1=cNGN → selling cNGN uses swap_token1_for_token0
    bsc_bal = next((b for b in balances if b.role in ("uni-bsc-trade", "trade_uni_bsc")), None)
    if bsc_bal:
        result["uni_bsc_usdt"] = float(bsc_bal.token_balances.get("USDT", Decimal(0)))
        bsc_sqrt, bsc_liq, _, bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
        if bsc_sqrt:
            bsc_cngn = bsc_bal.token_balances.get("cNGN", Decimal(0))
            if bsc_cngn > 0:
                result["uni_bsc_cngn_usd"] = float(dex_holdings_value(bsc_cngn, bsc_sqrt, bsc_liq, bsc_fee, 18, 6, cngn_is_token0=False))

    # Uniswap Base: token0=cNGN, token1=USDC → selling cNGN uses swap_token0_for_token1
    base_bal = next((b for b in balances if b.role in ("uni-base-trade", "trade_uni_base")), None)
    if base_bal:
        result["uni_base_usdc"] = float(base_bal.token_balances.get("USDC", Decimal(0)))
        base_sqrt, base_liq, _, base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)
        if base_sqrt:
            base_cngn = base_bal.token_balances.get("cNGN", Decimal(0))
            if base_cngn > 0:
                result["uni_base_cngn_usd"] = float(dex_holdings_value(base_cngn, base_sqrt, base_liq, base_fee, 6, 6, cngn_is_token0=True))

    return result
