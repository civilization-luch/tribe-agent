from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext, TaskSpec
from tribe_agent.core.queue import Task
from tribe_agent.core.runtime.base import RunResult
from tribe_agent.core.scheduler import Runner, _backpressure_exceeded


class FakeQueue:
    def __init__(self, pending: int) -> None:
        self._pending = pending

    async def count_pending(self, task_type: str | None = None) -> int:
        return self._pending


class RecordingQueue:
    def __init__(self) -> None:
        self.reset_calls: list[int] = []

    async def reset_stale(self, timeout_minutes: int = 30) -> int:
        self.reset_calls.append(timeout_minutes)
        return 0

    async def count_pending(self, task_type: str | None = None) -> int:
        return 0

    async def dequeue_batch(self, task_type: str, limit: int):
        return []


class DrainQueue:
    def __init__(self) -> None:
        self.completed: list[tuple[int, dict | None]] = []
        self.failed: list[tuple[int, str]] = []

    async def complete(self, task_id: int, result: dict | None = None) -> None:
        self.completed.append((task_id, result))

    async def fail(self, task_id: int, error: str) -> None:
        self.failed.append((task_id, error))

    async def count_pending(self, task_type: str | None = None) -> int:
        return 0

    async def reset_stale(self, timeout_minutes: int = 30) -> int:
        return 0


class FakeRuntime:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple] = []
        self._ok = ok

    async def run(self, *, title: str, prompt: str, directory: Path, agent: str) -> RunResult:
        self.calls.append((str(directory), agent))
        if self._ok:
            return RunResult(exit_code=0, stdout="", stderr="", session_id="s1", status="ok")
        return RunResult(exit_code=1, stdout="", stderr="boom", status="error")


class StubRepo:
    def __init__(self, fail_push: bool = False) -> None:
        self.checkouts: list[tuple] = []
        self.pushes: list[tuple] = []
        self._fail_push = fail_push

    async def checkout_branch(self, path: Path, name: str, base: str = "") -> None:
        self.checkouts.append((str(path), name, base))

    async def push(self, path: Path, target: str) -> None:
        if self._fail_push:
            raise RuntimeError("push failed")
        self.pushes.append((str(path), target))

    def head_branch(self, path: Path) -> str:
        return "main"


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        self.messages.append(variables or {})
        return {"createComment": {"id": "c1"}}


def _ctx(config, pending: int) -> ScanContext:
    return ScanContext(
        config=config,
        client=None,  # type: ignore[arg-type]
        queue=FakeQueue(pending),  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )


async def test_backpressure_blocks_at_capacity():
    config = load_config(profile="community")
    config.concurrency.max_queue = 1
    assert await _backpressure_exceeded(_ctx(config, 1)) is True


async def test_backpressure_allows_under_capacity():
    config = load_config(profile="community")
    config.concurrency.max_queue = 5
    assert await _backpressure_exceeded(_ctx(config, 3)) is False


async def test_backpressure_disabled_when_max_queue_zero():
    config = load_config(profile="community")
    config.concurrency.max_queue = 0
    assert await _backpressure_exceeded(_ctx(config, 100)) is False


async def test_recover_stale_uses_configured_timeout():
    config = load_config(profile="community")
    config.intervals["stale_reset_minutes"] = 7
    runner = Runner(config, [])
    queue = RecordingQueue()
    ctx = ScanContext(
        config=config,
        client=None,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    await runner._recover_stale(ctx)  # type: ignore[arg-type]
    assert queue.reset_calls == [7]


async def test_recover_stale_defaults_to_30_minutes():
    config = load_config(profile="community")
    runner = Runner(config, [])
    queue = RecordingQueue()
    ctx = ScanContext(
        config=config,
        client=None,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    await runner._recover_stale(ctx)  # type: ignore[arg-type]
    assert queue.reset_calls == [30]


def _item(payload: dict, repo_path: str = "") -> Task:
    return Task(
        id=1,
        task_type="execute",
        status="pending",
        community_id="",
        workspace_id="",
        repo_path=repo_path,
        payload=payload,
    )


def _spec() -> TaskSpec:
    return TaskSpec(
        task_type="execute",
        batch_limit=1,
        agent="tribe-task-executor",
        build_prompt=lambda batch: "do it",
    )


async def _execute(config, items: list[Task], runtime, repo, client) -> DrainQueue:
    runner = Runner(config, [])
    queue = DrainQueue()
    ctx = ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    await runner._execute(  # type: ignore[arg-type]
        ctx, runtime, _spec(), items, items[0].repo_path or "/tmp/nonrepo", asyncio.Semaphore(1)
    )
    return queue


async def test_execute_direct_push_pushes_to_bound_branch():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"push": True, "push_mode": "direct"}
    repo = StubRepo()
    client = FakeClient()
    item = _item({"issue_id": "i1", "workspace_id": "w1", "branch": "main"}, repo_path="/tmp/clone")
    queue = await _execute(config, [item], FakeRuntime(), repo, client)
    assert repo.pushes == [("/tmp/clone", "main")]
    assert queue.completed and not queue.failed
    assert client.messages and client.messages[0]["parentId"] == "i1"


async def test_execute_branch_mode_checks_out_and_pushes_feature():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"push": True, "push_mode": "branch"}
    repo = StubRepo()
    client = FakeClient()
    item = _item({"issue_id": "i1", "workspace_id": "w1", "branch": "main"}, repo_path="/tmp/clone")
    queue = await _execute(config, [item], FakeRuntime(), repo, client)
    assert repo.checkouts and repo.checkouts[0][2] == "main"
    assert repo.checkouts[0][1].startswith("tribe/")
    assert repo.pushes and repo.pushes[0][1] == repo.checkouts[0][1]
    assert not queue.failed


async def test_execute_skips_push_when_disabled():
    config = load_config(profile="community")
    repo = StubRepo()
    client = FakeClient()
    item = _item({"issue_id": "i1", "branch": "main"}, repo_path="/tmp/clone")
    queue = await _execute(config, [item], FakeRuntime(), repo, client)
    assert repo.pushes == []
    assert client.messages == []
    assert queue.completed


async def test_execute_skips_push_for_unmanaged_directory():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"push": True}
    repo = StubRepo()
    client = FakeClient()
    item = _item({"issue_id": "i1", "branch": "main"})
    queue = await _execute(config, [item], FakeRuntime(), repo, client)
    assert repo.pushes == []
    assert queue.completed


async def test_execute_push_failure_keeps_task_completed():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"push": True}
    repo = StubRepo(fail_push=True)
    client = FakeClient()
    item = _item({"issue_id": "i1", "branch": "main"}, repo_path="/tmp/clone")
    queue = await _execute(config, [item], FakeRuntime(), repo, client)
    assert queue.completed and not queue.failed


async def test_execute_runtime_failure_marks_failed_and_skips_push():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"push": True}
    repo = StubRepo()
    client = FakeClient()
    item = _item({"issue_id": "i1", "branch": "main"}, repo_path="/tmp/clone")
    queue = await _execute(config, [item], FakeRuntime(ok=False), repo, client)
    assert queue.failed and not queue.completed
    assert repo.pushes == []