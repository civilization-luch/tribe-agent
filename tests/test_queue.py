from __future__ import annotations

from pathlib import Path

import pytest

from tribe_agent.core.queue import TaskQueue


@pytest.fixture
async def queue(tmp_path: Path):
    q = TaskQueue(tmp_path / "agent.db")
    await q.connect()
    yield q
    await q.close()


async def test_enqueue_and_dequeue(queue: TaskQueue):
    await queue.enqueue("notification", {"a": 1}, community_id="c1", dedup_key="n1")
    await queue.enqueue("notification", {"a": 2}, community_id="c1", dedup_key="n2")
    batch = await queue.dequeue_batch("notification", 10)
    assert len(batch) == 2
    assert batch[0].payload == {"a": 1}
    assert batch[0].community_id == "c1"
    assert await queue.count_pending("notification") == 0


async def test_dedup_key(queue: TaskQueue):
    first = await queue.enqueue("notification", {}, dedup_key="same")
    second = await queue.enqueue("notification", {}, dedup_key="same")
    assert first is not None
    assert second is None
    assert await queue.count_pending("notification") == 1


async def test_complete_and_fail(queue: TaskQueue):
    task_id = await queue.enqueue("execute", {"issue": "x"}, dedup_key="i1")
    batch = await queue.dequeue_batch("execute", 1)
    await queue.complete(batch[0].id, {"session_id": "s1"})
    assert await queue.count_pending("execute") == 0

    await queue.enqueue("execute", {"issue": "y"}, dedup_key="i2")
    batch = await queue.dequeue_batch("execute", 1)
    await queue.fail(batch[0].id, "boom")
    assert task_id is not None


async def test_reset_stale(queue: TaskQueue):
    await queue.enqueue("classify", {}, dedup_key="c1")
    await queue.dequeue_batch("classify", 1)
    reset = await queue.reset_stale(timeout_minutes=0)
    assert reset == 1
    assert await queue.count_pending("classify") == 1


async def test_list_completed_and_update_result(queue: TaskQueue):
    await queue.enqueue("execute", {"issue_id": "i1"}, dedup_key="i1")
    batch = await queue.dequeue_batch("execute", 1)
    await queue.complete(batch[0].id, {"pr_number": 9, "repo": "civilization-luch/tribe"})

    done = await queue.list_completed("execute")
    assert len(done) == 1
    assert done[0].result == {"pr_number": 9, "repo": "civilization-luch/tribe"}
    assert done[0].payload == {"issue_id": "i1"}

    await queue.update_result(done[0].id, deployed=True, deployed_sha="abc1234")
    again = await queue.list_completed("execute")
    assert again[0].result == {
        "pr_number": 9,
        "repo": "civilization-luch/tribe",
        "deployed": True,
        "deployed_sha": "abc1234",
    }

    assert await queue.list_completed("notification") == []


async def test_list_tasks_includes_failed(queue: TaskQueue):
    ok_id = await queue.enqueue("execute", {"issue_id": "ok"}, dedup_key="ok")
    fail_id = await queue.enqueue("execute", {"issue_id": "bad"}, dedup_key="bad")
    batch = await queue.dequeue_batch("execute", 10)
    by_id = {t.id: t for t in batch}
    await queue.complete(by_id[ok_id].id, {"pr_number": 1})
    await queue.fail(by_id[fail_id].id, "boom")

    completed = await queue.list_tasks("execute", ("completed",))
    assert [t.payload["issue_id"] for t in completed] == ["ok"]
    both = await queue.list_tasks("execute", ("completed", "failed"))
    assert sorted(t.payload["issue_id"] for t in both) == ["bad", "ok"]
