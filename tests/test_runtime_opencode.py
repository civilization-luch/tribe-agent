"""Tests for the opencode runtime's command line and session lookup.

opencode v2 removed the `--dir` flag (the project comes from the working
directory) and requires lowercase `--log-level` values; sessions moved to the
`session_v2` table. Each of these silently broke the executor once already.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from tribe_agent.core.config import AppConfig
from tribe_agent.core.runtime.base import RunResult
from tribe_agent.core.runtime.opencode import OpencodeRuntime, _last_session_id


def _config() -> AppConfig:
    return AppConfig()


async def test_command_has_no_dir_flag_and_lowercase_log_level(tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    async def fake_run(cmd, cwd, env, timeout):
        captured["cmd"] = cmd
        return 0, "", ""

    with patch("tribe_agent.core.runtime.opencode._run", fake_run), patch(
        "tribe_agent.core.runtime.opencode._last_session_id", return_value=None
    ):
        runtime = OpencodeRuntime(_config())
        result = await runtime.run(
            title="t", prompt="p", directory=tmp_path, agent="tribe-executor"
        )

    cmd = captured["cmd"]
    assert "--dir" not in cmd
    assert cmd[cmd.index("--log-level") + 1] == "info"
    assert cmd[cmd.index("--agent") + 1] == "tribe-executor"
    assert cmd[cmd.index("--title") + 1] == "t"
    assert cmd[-1] == "p"
    assert isinstance(result, RunResult)


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
