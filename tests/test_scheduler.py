from __future__ import annotations

import logging

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
from tribe_agent.core.scheduler import _backpressure_exceeded


class FakeQueue:
    def __init__(self, pending: int) -> None:
        self._pending = pending

    async def count_pending(self, task_type: str | None = None) -> int:
        return self._pending


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