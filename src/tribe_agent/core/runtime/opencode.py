"""opencode runtime — drives `opencode run` against a project directory."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from tribe_agent.core.config import AppConfig
from tribe_agent.core.runtime.base import CodingRuntime, RunResult
from tribe_agent.core.sidecar import OpencodeSidecar

logger = logging.getLogger("tribe_agent")

DEFAULT_TIMEOUT = 600


async def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str, str]:
    # opencode resolves the project from PWD, not from the actual working
    # directory, so a spawned run would otherwise bind to the agent's own repo.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**env, "PWD": str(cwd)},
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


SESSION_LOOKUP_RETRIES = 5
SESSION_LOOKUP_DELAY = 0.4


def _query(db_path: Path, queries: tuple[str, ...], value: str) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("session lookup failed for %s: %s", db_path, exc)
        return None
    found: str | None = None
    for query in queries:
        try:
            row = conn.execute(query, (value,)).fetchone()
        except sqlite3.Error:
            continue
        if row:
            found = row[0]
            break
    conn.close()
    return found


def _last_session_id(db_path: Path, directory: Path, title: str = "") -> str | None:
    """Newest session created by a run.

    opencode v2 stores sessions in ``session_v2``; older databases keep them in
    ``session``, so both are consulted. A session is committed shortly after the
    run returns, so the lookup is retried — the run title identifies the session
    exactly, and the project worktree is the fallback.
    """
    by_title = (
        "SELECT id FROM session_v2 WHERE title = ? ORDER BY time_created DESC LIMIT 1",
        "SELECT id FROM session WHERE title = ? ORDER BY time_created DESC LIMIT 1",
    )
    by_worktree = (
        (
            "SELECT s.id FROM session_v2 s JOIN project p ON s.project_id = p.id "
            "WHERE p.worktree = ? ORDER BY s.time_created DESC LIMIT 1"
        ),
        (
            "SELECT s.id FROM session s JOIN project p ON s.project_id = p.id "
            "WHERE p.worktree = ? ORDER BY s.time_created DESC LIMIT 1"
        ),
    )
    attempts = SESSION_LOOKUP_RETRIES if title else 1
    for attempt in range(attempts):
        found = _query(db_path, by_title, title) if title else None
        found = found or _query(db_path, by_worktree, str(directory))
        if found:
            return found
        if attempt + 1 < attempts:
            time.sleep(SESSION_LOOKUP_DELAY)
    return None


class OpencodeRuntime(CodingRuntime):
    name = "opencode"

    def __init__(self, config: AppConfig, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout
        self.sidecar = OpencodeSidecar(config)

    def _env(self) -> dict[str, str]:
        return self.sidecar.env()

    async def run(
        self, *, title: str, prompt: str, directory: Path, agent: str
    ) -> RunResult:
        await self.sidecar.ensure()
        cmd = [
            "opencode",
            "run",
            "--auto",
            "--title",
            title,
            # opencode v2 takes lowercase levels and resolves the project from
            # the working directory (the old `--dir` flag is gone).
            "--log-level",
            "info",
        ]
        if agent:
            cmd += ["--agent", agent]
        if self.config.opencode.model:
            cmd += ["--model", self.config.opencode.model]
        cmd.append(prompt)

        exit_code, stdout, stderr = await _run(cmd, directory, self._env(), self.timeout)
        session_id = await asyncio.to_thread(
            _last_session_id, self.sidecar.db_path, directory, title
        )
        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            session_id=session_id,
            status=_classify(exit_code, stderr),
        )

    async def close(self) -> None:
        await self.sidecar.stop()
