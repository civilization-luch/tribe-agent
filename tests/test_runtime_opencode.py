"""Tests for the opencode runtime's command line and session lookup.

opencode v2 removed the `--dir` flag (the project comes from the working
directory), requires lowercase `--log-level` values, moved sessions to the
`session_v2` table and ignores `OPENCODE_CONFIG_DIR`/`OPENCODE_CONFIG` in favour
of a server that owns its configuration. Each of these silently broke the
executor once already.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tribe_agent.core.config import AppConfig
from tribe_agent.core.runtime.base import RunResult
from tribe_agent.core.runtime.opencode import OpencodeRuntime, _last_session_id, _run


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.data_dir = str(tmp_path)
    config.opencode.model = "myrouter/some-model"
    return config


async def test_command_targets_the_sidecar(tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    async def fake_run(cmd, cwd, env, timeout):
        captured["cmd"] = cmd
        captured["env"] = env
        return 0, "", ""

    runtime = OpencodeRuntime(_config(tmp_path))
    with (
        patch("tribe_agent.core.runtime.opencode._run", fake_run),
        patch("tribe_agent.core.runtime.opencode._last_session_id", return_value=None),
        patch.object(runtime.sidecar, "ensure", AsyncMock()),
    ):
        result = await runtime.run(
            title="t", prompt="p", directory=tmp_path, agent="tribe-executor"
        )

    cmd = captured["cmd"]
    assert "--dir" not in cmd
    assert "--server" not in cmd
    assert cmd[cmd.index("--log-level") + 1] == "info"
    assert cmd[cmd.index("--agent") + 1] == "tribe-executor"
    assert cmd[cmd.index("--model") + 1] == "myrouter/some-model"
    assert cmd[cmd.index("--title") + 1] == "t"
    assert cmd[-1] == "p"
    assert isinstance(result, RunResult)


async def test_command_omits_missing_agent_and_model(tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    async def fake_run(cmd, cwd, env, timeout):
        captured["cmd"] = cmd
        return 0, "", ""

    config = _config(tmp_path)
    config.opencode.model = ""
    runtime = OpencodeRuntime(config)
    with (
        patch("tribe_agent.core.runtime.opencode._run", fake_run),
        patch("tribe_agent.core.runtime.opencode._last_session_id", return_value=None),
        patch.object(runtime.sidecar, "ensure", AsyncMock()),
    ):
        await runtime.run(title="t", prompt="p", directory=tmp_path, agent="")

    assert "--agent" not in captured["cmd"]
    assert "--model" not in captured["cmd"]


async def test_sidecar_is_started_before_a_run(tmp_path: Path) -> None:
    async def fake_run(cmd, cwd, env, timeout):
        return 0, "", ""

    runtime = OpencodeRuntime(_config(tmp_path))
    ensure = AsyncMock()
    with (
        patch("tribe_agent.core.runtime.opencode._run", fake_run),
        patch("tribe_agent.core.runtime.opencode._last_session_id", return_value=None),
        patch.object(runtime.sidecar, "ensure", ensure),
    ):
        await runtime.run(title="t", prompt="p", directory=tmp_path, agent="a")

    ensure.assert_awaited_once()


async def test_run_points_pwd_at_the_target_directory(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

        def kill(self) -> None:
            pass

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    with patch("tribe_agent.core.runtime.opencode.asyncio.create_subprocess_exec", fake_exec):
        await _run(["opencode", "run"], tmp_path, {"PWD": "/elsewhere"}, 5)

    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["PWD"] == str(tmp_path)


def _db_with_session(table: str) -> Path:
    import tempfile

    db = Path(tempfile.mkdtemp()) / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL)")
    conn.execute(
        f"CREATE TABLE {table} (id TEXT PRIMARY KEY, project_id TEXT, time_created INTEGER)"
    )
    conn.execute("INSERT INTO project VALUES ('p1', '/repo')")
    conn.execute(f"INSERT INTO {table} VALUES ('ses_new', 'p1', 100)")
    conn.commit()
    conn.close()
    return db


def test_session_lookup_prefers_session_v2() -> None:
    db = _db_with_session("session_v2")
    assert _last_session_id(db, Path("/repo")) == "ses_new"


def test_session_lookup_falls_back_to_legacy_table() -> None:
    db = _db_with_session("session")
    assert _last_session_id(db, Path("/repo")) == "ses_new"


def test_session_lookup_returns_none_for_other_worktree() -> None:
    db = _db_with_session("session_v2")
    assert _last_session_id(db, Path("/elsewhere")) is None


def test_session_lookup_survives_missing_db(tmp_path: Path) -> None:
    assert _last_session_id(tmp_path / "nope.db", Path("/repo")) is None
