"""Scheduler — wires scanners, the task executor, and presence together."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from tribe_agent.core.client.graphql import GraphQLClient
from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.presence import presence_loop
from tribe_agent.core.queue import Task, TaskQueue
from tribe_agent.core.repo import RepoManager
from tribe_agent.core.runtime.base import CodingRuntime, RunResult
from tribe_agent.core.runtime.codex import CodexRuntime
from tribe_agent.core.runtime.opencode import OpencodeRuntime

logger = logging.getLogger("tribe_agent")


def build_runtime(config: AppConfig) -> CodingRuntime:
    if config.runtime == "codex":
        return CodexRuntime(config)
    return OpencodeRuntime(config)


async def scanner_loop(scanner: Scanner, ctx: ScanContext) -> None:
    while True:
        if await _backpressure_exceeded(ctx):
            ctx.logger.warning(
                "queue at capacity (>=%d pending), skipping scanner %s",
                ctx.config.concurrency.max_queue,
                scanner.name,
            )
            await asyncio.sleep(scanner.interval)
            continue
        try:
            count = await scanner.run(ctx)
            if count:
                ctx.logger.info("scanner %s enqueued %d task(s)", scanner.name, count)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("scanner %s failed: %s", scanner.name, exc)
        await asyncio.sleep(scanner.interval)


async def _backpressure_exceeded(ctx: ScanContext) -> bool:
    limit = ctx.config.concurrency.max_queue
    if limit <= 0:
        return False
    pending = await ctx.queue.count_pending()
    return pending >= limit


class Runner:
    def __init__(self, config: AppConfig, modules: list[Module]) -> None:
        self.config = config
        self.modules = modules
        self._scanner_index: dict[str, Scanner] = {}

    def _prepare_dirs(self) -> None:
        self.config.data_path.mkdir(parents=True, exist_ok=True)
        if self.config.opencode.db:
            Path(self.config.opencode.db).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _collect(self) -> tuple[dict[str, TaskSpec], list[Scanner]]:
        specs: dict[str, TaskSpec] = {}
        scanners: list[Scanner] = []
        for module in self.modules:
            specs.update(module.tasks(self.config))
            scanners.extend(module.scanners(self.config))
        for scanner in scanners:
            self._scanner_index[scanner.name] = scanner
        return specs, scanners

    def _make_context(self, client: GraphQLClient, queue: TaskQueue) -> ScanContext:
        return ScanContext(
            config=self.config,
            client=client,
            queue=queue,
            repo=RepoManager(self.config),
            logger=logger,
        )

    async def run(self) -> None:
        self._prepare_dirs()
        client = GraphQLClient(self.config.community.api_url, self.config.community.bot_token)
        queue = TaskQueue(self.config.data_path / "agent.db")
        await queue.connect()
        ctx = self._make_context(client, queue)
        specs, scanners = self._collect()
        runtime = build_runtime(self.config)

        logger.info(
            "tribe-agent profile=%s runtime=%s modules=%s",
            self.config.profile,
            runtime.name,
            [m.id for m in self.modules],
        )

        coros = [scanner_loop(scanner, ctx) for scanner in scanners]
        coros.append(self._executor_loop(ctx, specs, runtime))
        coros.append(presence_loop(ctx))
        try:
            await asyncio.gather(*coros)
        finally:
            await client.close()
            await queue.close()

    async def scan_once(self, name: str) -> None:
        self._prepare_dirs()
        client = GraphQLClient(self.config.community.api_url, self.config.community.bot_token)
        queue = TaskQueue(self.config.data_path / "agent.db")
        await queue.connect()
        ctx = self._make_context(client, queue)
        _, scanners = self._collect()
        try:
            for scanner in scanners:
                if name in ("all", scanner.name):
                    count = await scanner.run(ctx)
                    logger.info("scanner %s enqueued %d task(s)", scanner.name, count)
        finally:
            await client.close()
            await queue.close()

    async def run_queue_once(self) -> None:
        self._prepare_dirs()
        client = GraphQLClient(self.config.community.api_url, self.config.community.bot_token)
        queue = TaskQueue(self.config.data_path / "agent.db")
        await queue.connect()
        ctx = self._make_context(client, queue)
        specs, _ = self._collect()
        runtime = build_runtime(self.config)
        try:
            await self._drain_once(ctx, specs, runtime)
        finally:
            await client.close()
            await queue.close()

    async def _executor_loop(
        self, ctx: ScanContext, specs: dict[str, TaskSpec], runtime: CodingRuntime
    ) -> None:
        sem = asyncio.Semaphore(ctx.config.concurrency.max_runs)
        running: set[asyncio.Task[None]] = set()
        interval = ctx.config.intervals.get("run_agent", 60)
        while True:
            ran = await self._drain_once(ctx, specs, runtime, sem=sem, running=running)
            if not ran:
                await asyncio.sleep(interval)

    async def _drain_once(
        self,
        ctx: ScanContext,
        specs: dict[str, TaskSpec],
        runtime: CodingRuntime,
        sem: asyncio.Semaphore | None = None,
        running: set[asyncio.Task[None]] | None = None,
    ) -> bool:
        sem = sem or asyncio.Semaphore(ctx.config.concurrency.max_runs)
        ran = False
        for task_type, spec in specs.items():
            if await ctx.queue.count_pending(task_type) == 0:
                continue
            batch = await ctx.queue.dequeue_batch(task_type, spec.batch_limit)
            if not batch:
                continue
            ran = True
            for key, items in self._group(batch).items():
                coro = self._execute(ctx, runtime, spec, items, key, sem)
                if running is None:
                    await coro
                else:
                    task = asyncio.create_task(coro)
                    running.add(task)
                    task.add_done_callback(running.discard)
        return ran

    def _group(self, batch: list[Task]) -> dict[str, list[Task]]:
        groups: dict[str, list[Task]] = {}
        for task in batch:
            key = task.repo_path or self.config.project_path or str(self.config.data_path)
            groups.setdefault(key, []).append(task)
        return groups

    async def _execute(
        self,
        ctx: ScanContext,
        runtime: CodingRuntime,
        spec: TaskSpec,
        items: list[Task],
        directory_key: str,
        sem: asyncio.Semaphore,
    ) -> None:
        prompt = spec.build_prompt(items)
        title = f"{spec.task_type}-{datetime.now(UTC).isoformat()}"
        directory = Path(directory_key).expanduser()
        async with sem:
            try:
                result = await runtime.run(
                    title=title, prompt=prompt, directory=directory, agent=spec.agent
                )
            except Exception as exc:  # noqa: BLE001
                result = RunResult(exit_code=1, stderr=str(exc), status="error")
        if result.ok:
            for task in items:
                await ctx.queue.complete(task.id, {"session_id": result.session_id})
            if spec.on_success:
                await spec.on_success(items, ctx)
            logger.info("%s: OK %d task(s) session=%s", spec.task_type, len(items), result.session_id)
        else:
            error = (result.stderr or result.status)[:500]
            for task in items:
                await ctx.queue.fail(task.id, error)
            logger.warning("%s: FAIL %d task(s) (%s)", spec.task_type, len(items), error[:160])
