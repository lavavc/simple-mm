import asyncio
import websockets
import json

async def test_wss():
    uri = "wss://mainnet-rpc.assetchain.org"
    print(f"Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as ws:
            print("Connected! Sending eth_subscribe for newHeads...")
            payload = {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "eth_subscribe",
                "params": ["newHeads"]
            }
            await ws.send(json.dumps(payload))
            response = await ws.recv()
            print(f"Response: {response}")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_wss())
