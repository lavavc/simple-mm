import asyncio
from engine.venues.dex.aerodrome import AerodromeAdapter
from engine.config import settings

async def main():
    print("Initializing...")
    adapter = AerodromeAdapter(
        lp_private_key=settings.dashboard_api_token or "0x0000000000000000000000000000000000000000000000000000000000000001",
    )
    print("Fetching position...")
    try:
        pos = await adapter.get_position()
        print(pos)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
