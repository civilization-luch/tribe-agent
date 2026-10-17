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
    attempts     INTEGER NOT NULL DEFAULT 0,
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
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class TaskQueue:
    def __init__(self, path: str | Path, max_attempts: int = 3) -> None:
        self.path = Path(path).expanduser().resolve()
        self.max_attempts = max(1, max_attempts)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        cursor = await self.conn.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "attempts" not in columns:
            await self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )

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
        """Queue a task, reusing the row of a failed predecessor while it has retries left.

        A dedup key is the work item's identity, so a `failed` row would otherwise
        block that issue forever: the unique index ignores status.
        """
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (task_type, status, community_id, workspace_id, repo_path, dedup_key, payload, created_at)
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (task_type, community_id, workspace_id, repo_path, dedup_key, json.dumps(payload), _now()),
            )
            task_id: int | None = cursor.lastrowid if cursor.rowcount else None
            if task_id is None:
                task_id = await self._requeue_failed(
                    task_type, dedup_key, payload, community_id, workspace_id, repo_path
                )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return task_id

    async def _requeue_failed(
        self,
        task_type: str,
        dedup_key: str,
        payload: dict[str, Any],
        community_id: str,
        workspace_id: str,
        repo_path: str,
    ) -> int | None:
        if not dedup_key:
            return None
        cursor = await self.conn.execute(
            "SELECT id, attempts FROM tasks"
            " WHERE task_type = ? AND dedup_key = ? AND status = 'failed'",
            (task_type, dedup_key),
        )
        row = await cursor.fetchone()
        if row is None or int(row["attempts"]) >= self.max_attempts:
            return None
        await self.conn.execute(
            "UPDATE tasks SET status = 'pending', started_at = NULL, completed_at = NULL,"
            " result = NULL, error = NULL, payload = ?, community_id = ?, workspace_id = ?,"
            " repo_path = ? WHERE id = ?",
            (
                json.dumps(payload),
                community_id,
                workspace_id,
                repo_path,
                row["id"],
            ),
        )
        return int(row["id"])

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
                RETURNING id, task_type, status, community_id, workspace_id, repo_path, payload, created_at, attempts
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
                attempts=int(row["attempts"] or 0),
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
            "UPDATE tasks SET status='failed', completed_at=?, error=?, attempts=attempts+1 WHERE id=?",
            (_now(), error, task_id),
        )
        await self.conn.commit()

    async def reset_stale(self, timeout_minutes: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(minutes=timeout_minutes)).isoformat()
        cursor = await self.conn.execute(
            "UPDATE tasks SET status='pending', started_at=NULL, attempts=attempts+1"
            " WHERE status='running' AND started_at < ?",
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

    async def list_completed(self, task_type: str) -> list[Task]:
        return await self.list_tasks(task_type, ("completed",))

    async def list_tasks(
        self, task_type: str, statuses: tuple[str, ...] = ("completed",)
    ) -> list[Task]:
        placeholders = ",".join("?" for _ in statuses)
        cursor = await self.conn.execute(
            f"""
            SELECT id, task_type, status, community_id, workspace_id, repo_path,
                   payload, created_at, attempts, result, error
            FROM tasks WHERE task_type = ? AND status IN ({placeholders})
            ORDER BY id ASC
            """,
            (task_type, *statuses),
        )
        rows = await cursor.fetchall()
        return [Task(
            id=row["id"],
            task_type=row["task_type"],
            status=row["status"],
            community_id=row["community_id"],
            workspace_id=row["workspace_id"],
            repo_path=row["repo_path"],
            payload=_parse(row["payload"]),
            created_at=row["created_at"],
            attempts=int(row["attempts"] or 0),
            result=_parse(row["result"]) if row["result"] else None,
            error=row["error"],
        ) for row in rows]

    async def update_result(self, task_id: int, **patches: Any) -> None:
        cursor = await self.conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return
        result = _parse(row["result"]) or {}
        result.update(patches)
        await self.conn.execute(
            "UPDATE tasks SET result=? WHERE id=?", (json.dumps(result), task_id)
        )
        await self.conn.commit()
