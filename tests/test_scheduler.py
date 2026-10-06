from __future__ import annotations

import logging

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
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