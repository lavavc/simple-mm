import asyncio
from web3 import AsyncWeb3
from eth_abi import decode

w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider('https://mainnet-rpc.assetchain.org'))

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

async def check():
    for t in ['0x26E490d30e73c36800788DC6d6315946C4BbEa24', '0x7923C0f6FA3d1BA6EAFCAedAaD93e737Fd22FC4F']:
        contract = w3.eth.contract(address=w3.to_checksum_address(t), abi=ERC20_ABI)
        try:
            name = await contract.functions.name().call()
            symbol = await contract.functions.symbol().call()
            decimals = await contract.functions.decimals().call()
            print(f"Token {t}: {name} ({symbol}) - {decimals} decimals")
        except Exception as e:
            print(f"Token {t}: Failed: {e}")

asyncio.run(check())
