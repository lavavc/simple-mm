"""Run LP episode analysis against the engine SQLite database."""

from __future__ import annotations

import argparse
import asyncio
import json

from engine.db.repository import open_repository
from engine.lp.research import analyze_lp_strategy


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Analyze LP episodes from persisted engine data.")
    parser.add_argument("--db", required=True, help="Path to SQLite DB")
    parser.add_argument("--venue", required=True, help="Venue name, e.g. uni-base")
    parser.add_argument("--limit", type=int, default=5000, help="Max actions/snapshots to load")
    args = parser.parse_args()

    repo = await open_repository(args.db)
    try:
        actions = await repo.actions.get_actions_in_window(args.venue, limit=args.limit)
        snapshots = await repo.positions.get_position_snapshots(args.venue, limit=args.limit)
        report = analyze_lp_strategy(args.venue, actions, snapshots)
    finally:
        await repo.close()

    print(json.dumps(
        {
            "summary": {
                "venue": report.summary.venue,
                "total_episodes": report.summary.total_episodes,
                "closed_episodes": report.summary.closed_episodes,
                "profitable_episodes": report.summary.profitable_episodes,
                "profitable_share": str(report.summary.profitable_share),
                "total_pnl_usd": str(report.summary.total_pnl_usd),
                "avg_pnl_return": None if report.summary.avg_pnl_return is None else str(report.summary.avg_pnl_return),
                "win_score": str(report.summary.win_score),
                "avg_delta_traversed": None if report.summary.avg_delta_traversed is None else str(report.summary.avg_delta_traversed),
                "dominant_position_type": report.summary.dominant_position_type,
                "type_counts": report.summary.type_counts,
            },
            "open_episode": None if report.open_episode is None else {
                "token_id": report.open_episode.token_id,
                "started_at_ms": report.open_episode.started_at_ms,
                "pnl_usd": None if report.open_episode.pnl_usd is None else str(report.open_episode.pnl_usd),
                "pnl_return": None if report.open_episode.pnl_return is None else str(report.open_episode.pnl_return),
                "position_type": report.open_episode.position_type,
                "delta_traversed": None if report.open_episode.delta_traversed is None else str(report.open_episode.delta_traversed),
            },
        },
        indent=2,
    ))


if __name__ == "__main__":
    asyncio.run(_main())
