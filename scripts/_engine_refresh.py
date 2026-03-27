"""Helpers for telling the local engine to refresh wallet balances after manual actions."""

from __future__ import annotations

import httpx

from engine.config import settings


async def trigger_engine_balance_refresh() -> None:
    """Best-effort request to refresh the running engine's wallet snapshot."""
    if not settings.engine_api_token:
        print("engine_balance_refresh=skipped reason=missing_api_token")
        return

    url = f"http://{settings.host}:{settings.port}/api/accounts/refresh-balances"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.engine_api_token}"},
            )
        if response.is_success:
            data = response.json()
            venues = ",".join(data.get("venues", []))
            print(f"engine_balance_refresh=ok venues={venues}")
            return
        print(
            "engine_balance_refresh=failed "
            f"status={response.status_code} detail={response.text.strip()}"
        )
    except Exception as e:
        print(f"engine_balance_refresh=failed error={e}")
