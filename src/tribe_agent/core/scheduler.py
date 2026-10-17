"""Scheduler — wires scanners, the task executor, and presence together."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from tribe_agent.core.client.graphql import GraphQLClient
from tribe_agent.core.config import AppConfig
from tribe_agent.core.git_host import open_pull_request
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.presence import presence_loop
from tribe_agent.core.queue import Task, TaskQueue
from tribe_agent.core.repo import RepoManager, resolve_base_branch
from tribe_agent.core.runtime.base import CodingRuntime, RunResult
from tribe_agent.core.runtime.codex import CodexRuntime
from tribe_agent.core.runtime.opencode import OpencodeRuntime

logger = logging.getLogger("tribe_agent")


def build_runtime(config: AppConfig) -> CodingRuntime:
    if config.runtime == "codex":
        return CodexRuntime(config)
    return OpencodeRuntime(config)


def _pr_info(pr_url: str) -> tuple[str, int]:
    if not pr_url:
        return "", 0
    match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not match:
        return "", 0
    return match.group(1), int(match.group(2))


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
        queue = TaskQueue(
            self.config.data_path / "agent.db",
            max_attempts=self.config.concurrency.max_attempts,
        )
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
            await runtime.close()
            await client.close()
            await queue.close()

    async def scan_once(self, name: str) -> None:
        self._prepare_dirs()
        client = GraphQLClient(self.config.community.api_url, self.config.community.bot_token)
        queue = TaskQueue(
            self.config.data_path / "agent.db",
            max_attempts=self.config.concurrency.max_attempts,
        )
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
        queue = TaskQueue(
            self.config.data_path / "agent.db",
            max_attempts=self.config.concurrency.max_attempts,
        )
        await queue.connect()
        ctx = self._make_context(client, queue)
        specs, _ = self._collect()
        runtime = build_runtime(self.config)
        try:
            await self._recover_stale(ctx)
            await self._drain_once(ctx, specs, runtime)
        finally:
            await client.close()
            await queue.close()

    async def _recover_stale(self, ctx: ScanContext) -> None:
        stale_minutes = ctx.config.intervals.get("stale_reset_minutes", 30)
        recovered = await ctx.queue.reset_stale(timeout_minutes=stale_minutes)
        if recovered:
            ctx.logger.info("recovered %d stale running task(s)", recovered)

    async def _executor_loop(
        self, ctx: ScanContext, specs: dict[str, TaskSpec], runtime: CodingRuntime
    ) -> None:
        sem = asyncio.Semaphore(ctx.config.concurrency.max_runs)
        running: set[asyncio.Task[None]] = set()
        interval = ctx.config.intervals.get("run_agent", 60)
        while True:
            await self._recover_stale(ctx)
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

        executor_settings = ctx.config.module_config("executor")
        push_enabled = bool(executor_settings.get("push"))
        push_mode = executor_settings.get("push_mode", "direct")
        branch_prefix = executor_settings.get("branch_prefix", "tribe")
        open_pr = bool(executor_settings.get("open_pr"))
        push_target = ""
        base_branch = ""
        base_sha = ""
        pr_url = ""
        async with ctx.repo.lock_for(directory):
            if push_enabled and items and items[0].repo_path:
                payload = items[0].payload
                if push_mode == "branch":
                    workspace_id = payload.get("workspace_id", "")
                    issue_id = payload.get("issue_id", "")
                    base_branch = resolve_base_branch(
                        payload.get("branch", ""), ctx.config.repo.default_branch
                    )
                    push_target = f"{branch_prefix}/{workspace_id[:8]}-i{issue_id[:8]}"
                    await ctx.repo.create_branch_from(directory, push_target, base_branch)
                else:
                    base_branch = payload.get("branch", "") or ctx.repo.stored_branch(directory)
                    push_target = base_branch or ctx.repo.head_branch(directory)
                base_sha = ctx.repo.head_sha(directory)
                if open_pr and push_mode != "branch":
                    ctx.logger.warning("open_pr requires push_mode: branch, skipping PR")

            async with sem:
                try:
                    result = await runtime.run(
                        title=title, prompt=prompt, directory=directory, agent=spec.agent
                    )
                except Exception as exc:  # noqa: BLE001
                    result = RunResult(exit_code=1, stderr=str(exc), status="error")

            if result.ok and push_enabled and push_target:
                head_sha = ctx.repo.head_sha(directory)
                if head_sha and head_sha == base_sha:
                    ctx.logger.info("no changes in %s, skipping push/PR", directory)
                else:
                    try:
                        await ctx.repo.push(directory, push_target)
                        if open_pr and push_mode == "branch":
                            pr_url = await self._open_pr(ctx, items, push_target, base_branch)
                        await self._comment_pushed(ctx, items, push_target, pr_url)
                    except Exception as exc:  # noqa: BLE001
                        ctx.logger.warning("push failed for %s: %s", directory, exc)

        if result.ok:
            if pr_url:
                await self._keep_issue_open(ctx, items)
            result_data = {"session_id": result.session_id}
            pr_repo, pr_number = _pr_info(pr_url)
            if pr_repo and pr_number:
                result_data.update(
                    {"repo": pr_repo, "pr_number": pr_number, "pr_url": pr_url}
                )
            for task in items:
                await ctx.queue.complete(task.id, result_data)
            if spec.on_success:
                await spec.on_success(items, ctx)
            logger.info("%s: OK %d task(s) session=%s", spec.task_type, len(items), result.session_id)
        else:
            error = (result.stderr or result.status)[:500]
            for task in items:
                await ctx.queue.fail(task.id, error)
            logger.warning("%s: FAIL %d task(s) (%s)", spec.task_type, len(items), error[:160])

    async def _comment_pushed(
        self, ctx: ScanContext, items: list[Task], target: str, pr_url: str = ""
    ) -> None:
        if not ctx.config.module_config("executor").get("push_comment", True):
            return
        body = f"Pushed commit(s) to `{target}`."
        if pr_url:
            body = f"{body}\nPR: {pr_url}"
        for task in items:
            issue_id = task.payload.get("issue_id")
            if not issue_id:
                continue
            try:
                await ctx.client.gql(
                    "mutation($parentId:String!,$body:String!){"
                    " createComment(parentId:$parentId, body:$body){ id } }",
                    {"parentId": issue_id, "body": body},
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("push comment failed for issue %s: %s", issue_id, exc)

    async def _keep_issue_open(self, ctx: ScanContext, items: list[Task]) -> None:
        for task in items:
            issue_id = task.payload.get("issue_id")
            if not issue_id:
                continue
            try:
                await ctx.client.gql(
                    "mutation($id:String!){"
                    " setIssueStatus(input:{issueId:$id,status:\"open\"}){ id status } }",
                    {"id": issue_id},
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("keep-open after PR failed for issue %s: %s", issue_id, exc)

    async def _open_pr(
        self, ctx: ScanContext, items: list[Task], head: str, base: str
    ) -> str:
        payload = items[0].payload
        repo_url = payload.get("repo_url", "")
        token = ctx.config.repo.git_token
        if not repo_url:
            return ""
        if not token:
            ctx.logger.warning("open_pr set but no repo.git_token, skipping PR")
            return ""
        try:
            return await open_pull_request(
                url=repo_url,
                head=head,
                base=base or "main",
                title=payload.get("title", "") or "Automated change",
                body=f"Implements issue {payload.get('issue_id', '')}.",
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("open PR failed: %s", exc)
            return ""
