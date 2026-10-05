"""Executor module — implement issues assigned to the agent in its repository."""

from __future__ import annotations

from typing import Any

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.queue import Task

LIST_ASSIGNED = """
query($assigneeId: String!) {
  issues(assigneeId: $assigneeId, status: "open", limit: 50) {
    id title description status workspaceId communityId priority labels
  }
}
"""


class ExecutorModule(Module):
    id = "executor"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("executor", 120)
        return [Scanner("executor", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        limit = config.batch_limits.get("execute", 5)
        settings = config.module_config("executor")

        def build_prompt(batch: list[Task]) -> str:
            lines = ["@tribe-task-executor", ""]
            for index, task in enumerate(batch, 1):
                payload = task.payload
                lines.append(
                    f"{index}. issue_id={payload.get('issue_id', '?')}"
                    f" community={payload.get('community_id', '?')}"
                    f" workspace={payload.get('workspace_id', '?')}"
                    f'\n   title: "{payload.get("title", "")}"'
                    f'\n   description: "{payload.get("description", "")[:500]}"'
                )
            if settings.get("commands"):
                commands = settings["commands"]
                lines.append("")
                lines.append(f"Project commands: lint='{commands.get('lint', '')}' test='{commands.get('test', '')}'")
            return "\n".join(lines)

        return {
            "execute": TaskSpec(
                task_type="execute",
                batch_limit=limit,
                agent="tribe-task-executor",
                build_prompt=build_prompt,
            )
        }

    def _repo_for(self, ctx: ScanContext, workspace_id: str) -> tuple[str, str]:
        repos: dict[str, Any] = ctx.config.module_config("executor").get("repos", {}) or {}
        entry = repos.get(workspace_id)
        if isinstance(entry, dict):
            return entry.get("url", ""), entry.get("branch", "")
        if isinstance(entry, str):
            return entry, ""
        return "", ""

    async def _scan(self, ctx: ScanContext) -> int:
        user_id = ctx.config.community.user_id
        if not user_id:
            return 0
        data = await ctx.client.gql(LIST_ASSIGNED, {"assigneeId": user_id})
        issues = data.get("issues", []) or []
        enqueued = 0
        for issue in issues:
            issue_id = issue.get("id", "")
            if not issue_id:
                continue
            workspace_id = issue.get("workspaceId", "")
            url, branch = self._repo_for(ctx, workspace_id)
            repo_path = ""
            if url:
                try:
                    path = await ctx.repo.ensure(workspace_id, url, branch)
                    repo_path = str(path)
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning("repo resolve failed for %s: %s", workspace_id, exc)
            task_id = await ctx.queue.enqueue(
                "execute",
                {
                    "issue_id": issue_id,
                    "title": issue.get("title", ""),
                    "description": issue.get("description", ""),
                    "community_id": issue.get("communityId", ""),
                    "workspace_id": workspace_id,
                },
                community_id=issue.get("communityId", ""),
                workspace_id=workspace_id,
                repo_path=repo_path,
                dedup_key=issue_id,
            )
            if task_id:
                enqueued += 1
        return enqueued
