import asyncio
from web3 import AsyncWeb3
import sys

w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider('https://mainnet-rpc.assetchain.org'))

async def check():
    txs = [
        "0xa5b910fc69d0e1ef658120aa452993f7781c7cc8983e12fd9256532b49ee9f80",
        "0x2bd51e67af83d2ecfc8b323cc4fba3212bc3addcf69abde34966fbcd57ca14f9"
    ]
    for tx_hash in txs:
        tx = await w3.eth.get_transaction(tx_hash)
        print(f"To: {tx['to']}")

asyncio.run(check())
