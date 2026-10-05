"""Notifications module — process a bot's unread notifications."""

from __future__ import annotations

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.queue import Task

LIST_NOTIFICATIONS = """
query {
  myNotifications(unreadOnly: true) {
    id title body link eventType communityId createdAt
  }
}
"""

MARK_READ = """
mutation($id: String!) { markNotificationRead(id: $id) { id read } }
"""


class NotificationsModule(Module):
    id = "notifications"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("notifications", 60)
        return [Scanner("notifications", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        limit = config.batch_limits.get("notification", 30)
        return {
            "notification": TaskSpec(
                task_type="notification",
                batch_limit=limit,
                agent="tribe-notifications",
                build_prompt=self._build_prompt,
                on_success=self._on_success,
            )
        }

    async def _scan(self, ctx: ScanContext) -> int:
        data = await ctx.client.gql(LIST_NOTIFICATIONS)
        notifications = data.get("myNotifications", []) or []
        enqueued = 0
        for item in notifications:
            nid = item.get("id", "")
            if not nid:
                continue
            task_id = await ctx.queue.enqueue(
                "notification",
                {
                    "notification_id": nid,
                    "title": item.get("title", ""),
                    "body": item.get("body", ""),
                    "link": item.get("link", ""),
                    "event_type": item.get("eventType", ""),
                    "community_id": item.get("communityId", ""),
                },
                community_id=item.get("communityId", ""),
                dedup_key=nid,
            )
            if task_id:
                enqueued += 1
        return enqueued

    def _build_prompt(self, batch: list[Task]) -> str:
        lines = ["@tribe-notifications", ""]
        for index, task in enumerate(batch, 1):
            payload = task.payload
            event = payload.get("event_type", "?")
            lines.append(f"{index}. [{event}] {payload.get('title', '')[:80]}")
            link = payload.get("link", "")
            if link:
                lines.append(f"   Link: {link}")
            if event == "UserMentioned":
                lines.append(f"   Body: {payload.get('body', '')[:200]}")
        return "\n".join(lines)

    async def _on_success(self, batch: list[Task], ctx: ScanContext) -> None:
        for task in batch:
            notification_id = task.payload.get("notification_id")
            if not notification_id:
                continue
            try:
                await ctx.client.gql(MARK_READ, {"id": notification_id})
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("failed to mark notification %s read: %s", notification_id, exc)
