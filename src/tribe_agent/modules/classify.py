"""Classify module (platform) — turn feedback issues into classified triage tasks."""

from __future__ import annotations

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.queue import Task

LIST_ISSUES = """
query($workspaceId: String!) {
  issues(workspaceId: $workspaceId, status: "open", limit: 100) {
    id title description authorId communityId workspaceId
  }
}
"""


class ClassifyModule(Module):
    id = "classify"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("classify", 300)
        return [Scanner("classify", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        limit = config.batch_limits.get("classify", 30)
        return {
            "classify": TaskSpec(
                task_type="classify",
                batch_limit=limit,
                agent="tribe-classifier",
                build_prompt=self._build_prompt,
            )
        }

    async def _scan(self, ctx: ScanContext) -> int:
        workspace_id = ctx.config.module_config("classify").get("feedback_workspace_id") or ""
        if not workspace_id:
            return 0
        data = await ctx.client.gql(LIST_ISSUES, {"workspaceId": workspace_id})
        issues = data.get("issues", []) or []
        enqueued = 0
        for issue in issues:
            issue_id = issue.get("id", "")
            if not issue_id:
                continue
            task_id = await ctx.queue.enqueue(
                "classify",
                {
                    "issue_id": issue_id,
                    "title": issue.get("title", ""),
                    "description": issue.get("description", ""),
                    "author_id": issue.get("authorId", ""),
                    "community_id": issue.get("communityId", ""),
                    "workspace_id": issue.get("workspaceId", workspace_id),
                },
                community_id=issue.get("communityId", ""),
                workspace_id=issue.get("workspaceId", workspace_id),
                dedup_key=issue_id,
            )
            if task_id:
                enqueued += 1
        return enqueued

    def _build_prompt(self, batch: list[Task]) -> str:
        lines = ["@tribe-classifier", ""]
        for index, task in enumerate(batch, 1):
            payload = task.payload
            description = (payload.get("description", "") or "")[:300]
            lines.append(
                f"{index}. issue_id={payload.get('issue_id', '?')}"
                f" workspace={payload.get('workspace_id', '?')}"
                f" community={payload.get('community_id', '?')}"
                f'\n   title: "{payload.get("title", "")[:200]}"'
                f'\n   description: "{description}"'
                f"\n   by author_id={payload.get('author_id', '?')}"
            )
        return "\n".join(lines)
