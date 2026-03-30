"""Tests for wallet activity matching in the DEX WS listener."""

from engine.core.arbitrage.listener import WalletActivitySubscription, matching_wallet_venues


_WALLET = "0x74b479868e3B8a21BDE4bb09F85177aCF9976A2d"
_TOKEN = "0x55d398326f99059fF775485246999027B3197955"


def _topic(address: str) -> str:
    normalized = address.lower().removeprefix("0x")
    return "0x" + ("0" * 24) + normalized


def test_matching_wallet_venues_detects_incoming_transfer():
    log = {
        "address": _TOKEN,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            _topic("0x1111111111111111111111111111111111111111"),
            _topic(_WALLET),
        ],
    }
    subs = [
        WalletActivitySubscription(
            venue_name="uni-bsc",
            wallet_address=_WALLET,
            token_address=_TOKEN,
            token_symbol="USDT",
        )
    ]

    assert matching_wallet_venues(log, subs) == {"uni-bsc"}


def test_matching_wallet_venues_ignores_unrelated_transfer():
    log = {
        "address": _TOKEN,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            _topic("0x1111111111111111111111111111111111111111"),
            _topic("0x2222222222222222222222222222222222222222"),
        ],
    }
    subs = [
        WalletActivitySubscription(
            venue_name="uni-bsc",
            wallet_address=_WALLET,
            token_address=_TOKEN,
            token_symbol="USDT",
        )
    ]

    assert matching_wallet_venues(log, subs) == set()
