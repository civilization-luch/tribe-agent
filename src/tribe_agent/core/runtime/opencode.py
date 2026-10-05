"""opencode runtime — drives `opencode run` against a project directory."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

from tribe_agent.core.config import AppConfig
from tribe_agent.core.opencode_config import default_db_path, ensure_runtime_config
from tribe_agent.core.runtime.base import CodingRuntime, RunResult

DEFAULT_TIMEOUT = 600


async def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Timeout after {timeout} seconds"


def _classify(exit_code: int, stderr: str) -> str:
    low = stderr.lower()
    if exit_code == -1:
        return "timeout"
    if any(x in low for x in ("429", "limit exceeded", "rate limit", "too many requests")):
        return "rate_limit"
    if any(x in low for x in ("402", "payment required", "insufficient balance")):
        return "subscription"
    return "ok" if exit_code == 0 else "error"


def _last_session_id(db_path: Path, directory: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT s.id FROM session s
            JOIN project p ON s.project_id = p.id
            WHERE p.worktree = ?
            ORDER BY s.time_created DESC LIMIT 1
            """,
            (str(directory),),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


class OpencodeRuntime(CodingRuntime):
    name = "opencode"

    def __init__(self, config: AppConfig, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.config.opencode.config_dir:
            env["OPENCODE_CONFIG_DIR"] = self.config.opencode.config_dir
        if self.config.opencode.db:
            env["OPENCODE_DB"] = self.config.opencode.db
        runtime_config = ensure_runtime_config(self.config)
        if runtime_config:
            env["OPENCODE_CONFIG"] = str(runtime_config)
        if self.config.community.mcp_url:
            env["TRIBE_MCP_URL"] = self.config.community.mcp_url
        if self.config.community.bot_token:
            env["TRIBE_BOT_TOKEN"] = self.config.community.bot_token
        return env

    async def run(
        self, *, title: str, prompt: str, directory: Path, agent: str
    ) -> RunResult:
        cmd = [
            "opencode",
            "run",
            "--auto",
            "--dir",
            str(directory),
            "--title",
            title,
            "--log-level",
            "INFO",
        ]
        if agent:
            cmd += ["--agent", agent]
        if self.config.opencode.model:
            cmd += ["--model", self.config.opencode.model]
        cmd.append(prompt)

        exit_code, stdout, stderr = await _run(cmd, directory, self._env(), self.timeout)
        db = Path(self.config.opencode.db) if self.config.opencode.db else default_db_path()
        session_id = await asyncio.to_thread(_last_session_id, db, directory)
        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            session_id=session_id,
            status=_classify(exit_code, stderr),
        )
