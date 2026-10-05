"""SQLite-backed task queue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    community_id TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL DEFAULT '',
    repo_path    TEXT NOT NULL DEFAULT '',
    dedup_key    TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    result       TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks (task_type, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedup
    ON tasks (task_type, dedup_key) WHERE dedup_key <> '';
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


@dataclass
class Task:
    id: int
    task_type: str
    status: str
    community_id: str
    workspace_id: str
    repo_path: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None


class TaskQueue:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TaskQueue is not connected")
        return self._conn

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        community_id: str = "",
        workspace_id: str = "",
        repo_path: str = "",
        dedup_key: str = "",
    ) -> int | None:
        cursor = await self.conn.execute(
            """
            INSERT OR IGNORE INTO tasks
                (task_type, status, community_id, workspace_id, repo_path, dedup_key, payload, created_at)
            VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (task_type, community_id, workspace_id, repo_path, dedup_key, json.dumps(payload), _now()),
        )
        await self.conn.commit()
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid

    async def dequeue_batch(self, task_type: str, limit: int) -> list[Task]:
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.conn.execute(
                """
                UPDATE tasks
                SET status = 'running', started_at = ?
                WHERE id IN (
                    SELECT id FROM tasks
                    WHERE status = 'pending' AND task_type = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                RETURNING id, task_type, status, community_id, workspace_id, repo_path, payload, created_at
                """,
                (_now(), task_type, limit),
            )
            rows = await cursor.fetchall()
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return [
            Task(
                id=row["id"],
                task_type=row["task_type"],
                status=row["status"],
                community_id=row["community_id"],
                workspace_id=row["workspace_id"],
                repo_path=row["repo_path"],
                payload=_parse(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def complete(self, task_id: int, result: dict[str, Any] | None = None) -> None:
        await self.conn.execute(
            "UPDATE tasks SET status='completed', completed_at=?, result=? WHERE id=?",
            (_now(), json.dumps(result or {}), task_id),
        )
        await self.conn.commit()

    async def fail(self, task_id: int, error: str) -> None:
        await self.conn.execute(
            "UPDATE tasks SET status='failed', completed_at=?, error=? WHERE id=?",
            (_now(), error, task_id),
        )
        await self.conn.commit()

    async def reset_stale(self, timeout_minutes: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(minutes=timeout_minutes)).isoformat()
        cursor = await self.conn.execute(
            "UPDATE tasks SET status='pending', started_at=NULL WHERE status='running' AND started_at < ?",
            (cutoff,),
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    async def count_pending(self, task_type: str | None = None) -> int:
        if task_type:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='pending' AND task_type=?", (task_type,)
            )
        else:
            cursor = await self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
