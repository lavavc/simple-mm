import asyncio
from web3 import AsyncWeb3

w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider('https://mainnet-rpc.assetchain.org'))

POOL_ABI = [
    {"constant": True, "inputs": [], "name": "factory", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "fee", "outputs": [{"name": "", "type": "uint24"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "tickSpacing", "outputs": [{"name": "", "type": "int24"}], "type": "function"}
]

async def check():
    pool = w3.eth.contract("0xE2a45a102B00Fad6447d0AD859b43BAf8bF6DeF1", abi=POOL_ABI)
    factory = await pool.functions.factory().call()
    t0 = await pool.functions.token0().call()
    t1 = await pool.functions.token1().call()
    fee = await pool.functions.fee().call()
    ts = await pool.functions.tickSpacing().call()
    print(f"Factory: {factory}")
    print(f"Token0: {t0}")
    print(f"Token1: {t1}")
    print(f"Fee: {fee}")
    print(f"Tick Spacing: {ts}")

asyncio.run(check())
