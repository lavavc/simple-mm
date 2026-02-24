import asyncio
import json
import urllib.request

async def get_txs():
    url = "https://scan.assetchain.org/api/v2/addresses/0xE2a45a102B00Fad6447d0AD859b43BAf8bF6DeF1/transactions"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        
        txs = data.get('items', [])
        for tx in txs[-5:]:  # get oldest txs
            print(f"Hash: {tx['hash']}, To: {tx['to']['hash']}")

asyncio.run(get_txs())
