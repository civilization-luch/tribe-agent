"""Support module — triage new SupportRequests for a community."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.queue import Task

COMMUNITY_SETTINGS = """
query($id: String!) {
  community(idOrSlug: $id) { id settings }
}
"""

LIST_SUPPORT_REQUESTS = """
query($communityId: String!, $status: String!, $limit: Int!) {
  supportRequests(communityId: $communityId, status: $status, limit: $limit) {
    id communityId sourceId targetGroupId title body status labels createdAt
    claimedBy claimExpiresAt
  }
}
"""


def _has_active_claim(request: dict[str, Any]) -> bool:
    if not request.get("claimedBy") or not request.get("claimExpiresAt"):
        return False
    try:
        expires = datetime.fromisoformat(str(request["claimExpiresAt"]))
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC)


class SupportModule(Module):
    id = "support"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("support", 120)
        return [Scanner("support", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        limit = config.batch_limits.get("support", 10)
        return {
            "support": TaskSpec(
                task_type="support",
                batch_limit=limit,
                agent="tribe-support",
                build_prompt=self._build_prompt,
            )
        }

    async def _scan(self, ctx: ScanContext) -> int:
        module_settings = ctx.config.module_config("support")
        communities = module_settings.get("communities") or ctx.config.community.communities
        if not communities and module_settings.get("community_id"):
            communities = [module_settings["community_id"]]
        if not communities:
            return 0

        status = str(module_settings.get("status", "new"))
        scan_limit = int(module_settings.get("scan_limit", 50))
        enqueued = 0

        for community_ref in communities:
            community = await self._load_community(ctx, community_ref)
            if community is None:
                continue
            community_id = community.get("id") or community_ref
            effective = self._effective_settings(module_settings, community.get("settings") or {})
            if not effective["agent_enabled"]:
                continue

            try:
                data = await ctx.client.gql(
                    LIST_SUPPORT_REQUESTS,
                    {
                        "communityId": community_id,
                        "status": status,
                        "limit": max(1, min(scan_limit, 500)),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("support scan failed for %s: %s", community_id, exc)
                continue

            requests = data.get("supportRequests", []) or []
            for request in requests:
                request_id = request.get("id", "")
                if not request_id:
                    continue
                if _has_active_claim(request):
                    continue
                task_id = await ctx.queue.enqueue(
                    "support",
                    {
                        "support_request_id": request_id,
                        "community_id": community_id,
                        "source_id": request.get("sourceId", ""),
                        "target_group_id": request.get("targetGroupId", ""),
                        "title": request.get("title", ""),
                        "body": (request.get("body", "") or "")[:500],
                        "labels": request.get("labels", []) or [],
                        "created_at": request.get("createdAt", ""),
                        "agent_user_id": effective["agent_user_id"],
                        "auto_convert": effective["auto_convert"],
                        "workspace_id": effective["workspace_id"],
                        "dry_run": effective["dry_run"],
                        "claim_ttl_seconds": effective["claim_ttl_seconds"],
                    },
                    community_id=community_id,
                    workspace_id=effective["workspace_id"],
                    dedup_key=request_id,
                )
                if task_id:
                    enqueued += 1

            ctx.logger.info(
                "support scan: community=%s status=%s scanned=%d enabled=%s dry_run=%s",
                community_id,
                status,
                len(requests),
                effective["agent_enabled"],
                effective["dry_run"],
            )

        return enqueued

    async def _load_community(self, ctx: ScanContext, community_ref: str) -> dict[str, Any] | None:
        try:
            data = await ctx.client.gql(COMMUNITY_SETTINGS, {"id": community_ref})
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("support community lookup failed for %s: %s", community_ref, exc)
            return None
        community = data.get("community")
        if not community:
            ctx.logger.warning("support community not found: %s", community_ref)
            return None
        return community

    def _effective_settings(
        self, module_settings: dict[str, Any], community_settings: dict[str, Any]
    ) -> dict[str, Any]:
        def pick(key: str, fallback: Any) -> Any:
            namespaced = community_settings.get(f"support.{key}")
            return fallback if namespaced is None else namespaced

        agent_user_id = pick(
            "agent_user_id",
            module_settings.get("agent_user_id", ""),
        )
        try:
            claim_ttl = int(pick("agent_claim_ttl_seconds", module_settings.get("claim_ttl_seconds", 900)))
        except (TypeError, ValueError):
            claim_ttl = 900
        return {
            "agent_enabled": bool(pick("agent_enabled", module_settings.get("agent_enabled", False))),
            "agent_user_id": str(agent_user_id or ""),
            "auto_convert": bool(pick("agent_auto_convert", module_settings.get("auto_convert", False))),
            "workspace_id": str(pick("agent_workspace_id", module_settings.get("workspace_id", "")) or ""),
            "dry_run": bool(pick("agent_dry_run", module_settings.get("dry_run", False))),
            "claim_ttl_seconds": max(60, min(claim_ttl, 3600)),
        }

    def _build_prompt(self, batch: list[Task]) -> str:
        lines = [
            "@tribe-support",
            "",
            "Process the new SupportRequests below.",
            "",
            "Rules:",
            "- Read each request with `get_support_request` before acting.",
            "- If the request is no longer `new`, or is claimed by another actor, stop and report it; do not change anything.",
            "- Claim it with `claim_support_request` and the given claim_ttl_seconds.",
            "- If the claim fails because another actor holds it, stop; do not retry or take over.",
            "- Public replies go to the author. Internal notes are for the team only.",
            "- Never expose internal notes, source tokens, or private issue details.",
            "- Use `needs_info` only with a concrete question.",
            "- If auto_convert is false, do NOT call `convert_support_request`; set status `accepted` and leave an internal note.",
            "- If auto_convert is true, convert only a clearly accepted and actionable request into a workspace.",
            "- Use target_group_id to choose a workspace owned by that group when possible; otherwise use workspace_id.",
            "- Escalate to another group instead of converting when the request belongs to that group.",
            "- Release the claim when finished, or let the TTL expire.",
            "- Never claim to have fixed or deployed anything; you only triage.",
            "",
            "Requests:",
        ]
        for index, task in enumerate(batch, 1):
            payload = task.payload
            labels = ", ".join(payload.get("labels", []) or []) or "(none)"
            lines.append(
                f"{index}. support_request_id={payload.get('support_request_id', '?')}"
                f" community={payload.get('community_id', '?')}"
                f" source={payload.get('source_id', '?')}"
                f" target_group={payload.get('target_group_id', '') or '(none)'}"
                f"\n   title: \"{payload.get('title', '')[:200]}\""
                f"\n   body: \"{payload.get('body', '')[:300]}\""
                f"\n   labels: {labels}"
                f"\n   dry_run: {payload.get('dry_run', False)}"
                f" auto_convert: {payload.get('auto_convert', False)}"
                f" workspace_id: {payload.get('workspace_id', '') or '(none)'}"
                f" claim_ttl_seconds: {payload.get('claim_ttl_seconds', 900)}"
                f" agent_user_id: {payload.get('agent_user_id', '') or '(self)'}"
            )
        if any(task.payload.get("dry_run") for task in batch):
            lines.append("")
            lines.append("DRY RUN: do not call any mutation. Report what you would do instead.")
        return "\n".join(lines)
