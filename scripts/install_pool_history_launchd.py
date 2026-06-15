"""Render and install the daily pool-history macOS LaunchAgent."""

from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path

LABEL = "com.cngn.pool-history-update"
REPO_ROOT = Path(__file__).resolve().parents[1]


def render_launch_agent(
    *,
    repo_root: Path,
    python_executable: Path,
    hour: int,
    minute: int,
) -> bytes:
    if not 0 <= hour <= 23:
        raise ValueError("hour must be between 0 and 23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be between 0 and 59")
    repo_root = repo_root.resolve()
    python_executable = python_executable.resolve()
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            str(python_executable),
            str(repo_root / "scripts" / "update_v4_pool_history.py"),
        ],
        "WorkingDirectory": str(repo_root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(repo_root / "logs" / "pool-history-update.stdout.log"),
        "StandardErrorPath": str(repo_root / "logs" / "pool-history-update.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def install_launch_agent(
    *,
    destination: Path,
    repo_root: Path,
    python_executable: Path,
    hour: int,
    minute: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    repo_root.joinpath("logs").mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        render_launch_agent(
            repo_root=repo_root,
            python_executable=python_executable,
            hour=hour,
            minute=minute,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hour", type=int, required=True, help="Local hour, 0-23")
    parser.add_argument("--minute", type=int, required=True, help="Local minute, 0-59")
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist",
    )
    args = parser.parse_args()
    install_launch_agent(
        destination=args.destination,
        repo_root=REPO_ROOT,
        python_executable=Path(sys.executable),
        hour=args.hour,
        minute=args.minute,
    )
    print(f"installed {LABEL} at {args.destination}")


if __name__ == "__main__":
    main()
