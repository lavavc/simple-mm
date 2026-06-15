import plistlib
from pathlib import Path

import pytest

from scripts.install_pool_history_launchd import install_launch_agent, render_launch_agent


def test_render_launch_agent_uses_absolute_paths_and_explicit_schedule(tmp_path):
    repo = tmp_path / "automated-infra"
    python = Path("/opt/python/bin/python3")

    payload = plistlib.loads(
        render_launch_agent(repo_root=repo, python_executable=python, hour=3, minute=15)
    )

    assert payload["Label"] == "com.cngn.pool-history-update"
    assert payload["ProgramArguments"] == [
        str(python),
        str(repo / "scripts" / "update_v4_pool_history.py"),
    ]
    assert payload["WorkingDirectory"] == str(repo)
    assert payload["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert payload["StandardOutPath"] == str(repo / "logs" / "pool-history-update.stdout.log")
    assert payload["StandardErrorPath"] == str(repo / "logs" / "pool-history-update.stderr.log")
    assert payload["RunAtLoad"] is False


def test_install_launch_agent_writes_rendered_plist(tmp_path):
    destination = tmp_path / "Library" / "LaunchAgents" / "agent.plist"

    install_launch_agent(
        destination=destination,
        repo_root=tmp_path / "repo",
        python_executable=Path("/usr/bin/python3"),
        hour=4,
        minute=30,
    )

    payload = plistlib.loads(destination.read_bytes())
    assert payload["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}


@pytest.mark.parametrize(("hour", "minute"), [(-1, 0), (24, 0), (0, -1), (0, 60)])
def test_render_launch_agent_rejects_invalid_schedule(tmp_path, hour, minute):
    with pytest.raises(ValueError):
        render_launch_agent(
            repo_root=tmp_path,
            python_executable=Path("/usr/bin/python3"),
            hour=hour,
            minute=minute,
        )
