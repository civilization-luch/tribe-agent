"""Tests for the private opencode server the agent keeps alongside itself."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from tribe_agent.core.config import load_config
from tribe_agent.core.sidecar import DOWN, OK, OpencodeSidecar


def _config(tmp_path: Path):
    config = load_config()
    config.data_dir = str(tmp_path)
    config.community.mcp_url = "https://mcp.example.test/mcp"
    config.community.bot_token = "token"
    return config


def test_env_drops_inherited_opencode_variables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/legacy")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_leaked")
    monkeypatch.setenv("OPENCODE_CONFIG", "/legacy.jsonc")
    env = OpencodeSidecar(_config(tmp_path)).env()
    assert not any(key.startswith("OPENCODE") for key in env)


def test_env_points_at_the_isolated_home(tmp_path: Path) -> None:
    env = OpencodeSidecar(_config(tmp_path)).env()
    home = tmp_path / "opencode-home"
    assert env["XDG_CONFIG_HOME"] == str(home / "config")
    assert env["XDG_DATA_HOME"] == str(home / "data")
    assert env["XDG_STATE_HOME"] == str(home / "state")
    assert env["TRIBE_MCP_URL"] == "https://mcp.example.test/mcp"
    assert env["TRIBE_BOT_TOKEN"] == "token"


def test_url_matches_the_configured_port(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.opencode.port = 48123
    assert OpencodeSidecar(config).url == "http://127.0.0.1:48123"


def test_probe_reports_running_when_the_service_answers(tmp_path: Path) -> None:
    sidecar = OpencodeSidecar(_config(tmp_path))
    with patch.object(sidecar, "_service", AsyncMock(return_value=(0, "http://127.0.0.1:47391"))):
        assert asyncio_run(sidecar.probe()) == OK


def test_probe_reports_down_when_the_service_is_stopped(tmp_path: Path) -> None:
    sidecar = OpencodeSidecar(_config(tmp_path))
    with patch.object(sidecar, "_service", AsyncMock(return_value=(0, "stopped"))):
        assert asyncio_run(sidecar.probe()) == DOWN


def test_ensure_reuses_a_running_service(tmp_path: Path) -> None:
    sidecar = OpencodeSidecar(_config(tmp_path))
    start = AsyncMock()
    with (
        patch.object(sidecar, "_service", AsyncMock(return_value=(0, sidecar.url))),
        patch.object(sidecar, "start", start),
    ):
        asyncio_run(sidecar.ensure())
    start.assert_not_awaited()


def test_ensure_starts_a_stopped_service(tmp_path: Path) -> None:
    sidecar = OpencodeSidecar(_config(tmp_path))
    start = AsyncMock()
    with (
        patch.object(sidecar, "_service", AsyncMock(return_value=(0, "stopped"))),
        patch.object(sidecar, "start", start),
    ):
        asyncio_run(sidecar.ensure())
    start.assert_awaited_once()


def test_start_sets_the_port_and_boots_the_service(tmp_path: Path) -> None:
    sidecar = OpencodeSidecar(_config(tmp_path), port=48123)
    calls: list[tuple[str, ...]] = []

    async def fake_service(*args: str) -> tuple[int, str]:
        calls.append(args)
        return (0, sidecar.url) if args[0] == "start" else (0, "")

    with (
        patch.object(sidecar, "_service", fake_service),
        patch.object(sidecar, "probe", AsyncMock(return_value=OK)),
    ):
        asyncio_run(sidecar.start())

    assert ("set", "port", "48123") in calls
    assert ("start",) in calls


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
