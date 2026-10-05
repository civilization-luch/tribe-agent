"""Self-heal module — watch community metrics and raise issues on anomalies."""

from __future__ import annotations

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.queue import Task

TOTAL_OPEN = """
query($communityId: String!) { totalIssues(communityId: $communityId, status: "open") }
"""


class SelfHealModule(Module):
    id = "self_heal"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("self_heal", 86400)
        return [Scanner("self_heal", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        threshold = int(config.module_config("self_heal").get("open_issues_threshold", 20))

        def build_prompt(batch: list[Task]) -> str:
            lines = ["@self-healer", ""]
            for index, task in enumerate(batch, 1):
                payload = task.payload
                lines.append(
                    f"{index}. community={payload.get('community_id', '?')}"
                    f" open_issues={payload.get('open_issues', '?')} (threshold {threshold})"
                )
                if payload.get("metric"):
                    lines.append(f"   metric: {payload['metric']}")
            return "\n".join(lines)

        return {
            "self_heal": TaskSpec(
                task_type="self_heal",
                batch_limit=config.batch_limits.get("self_heal", 10),
                agent="self-healer",
                build_prompt=build_prompt,
            )
        }

    async def _scan(self, ctx: ScanContext) -> int:
        threshold = int(ctx.config.module_config("self_heal").get("open_issues_threshold", 20))
        communities = ctx.config.community.communities
        enqueued = 0
        for community_id in communities:
            try:
                data = await ctx.client.gql(TOTAL_OPEN, {"communityId": community_id})
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("self_heal metric failed for %s: %s", community_id, exc)
                continue
            total = int(data.get("totalIssues", 0) or 0)
            if total < threshold:
                continue
            task_id = await ctx.queue.enqueue(
                "self_heal",
                {"community_id": community_id, "open_issues": total, "metric": "open_issues"},
                community_id=community_id,
                dedup_key=f"{community_id}:{total // threshold}",
            )
            if task_id:
                enqueued += 1
        return enqueued
