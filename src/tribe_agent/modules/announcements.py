"""Announcements module — turn feature commits into announcement discussions."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner, TaskSpec
from tribe_agent.core.queue import Task


class AnnouncementsModule(Module):
    id = "announcements"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("announcements", 3600)
        return [Scanner("announcements", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        limit = config.batch_limits.get("announcement", 10)
        settings = config.module_config("announcements")

        def build_prompt(batch: list[Task]) -> str:
            lines = [
                "@announcements",
                "",
                "ANNOUNCEMENT CONFIG:",
                f"- Frequency: {settings.get('frequency', 'weekly')}",
                f"- Notification chat: {settings.get('telegram_chat_id') or '(not set)'}",
                "",
                "COMMITS:",
            ]
            for index, task in enumerate(batch, 1):
                payload = task.payload
                lines.append(
                    f"{index}. [{payload.get('sha', '?')[:8]}] {payload.get('message', '')[:120]}"
                )
            return "\n".join(lines)

        return {
            "announcement": TaskSpec(
                task_type="announcement",
                batch_limit=limit,
                agent="announcements",
                build_prompt=build_prompt,
            )
        }

    def _sha_file(self, ctx: ScanContext) -> Path:
        return ctx.config.data_path / "announcements_sha.txt"

    def _recent_feat_commits(self, project_path: str, sha_file: Path) -> list[dict[str, str]]:
        last_sha = sha_file.read_text().strip() if sha_file.is_file() else ""
        range_arg = f"{last_sha}..HEAD" if last_sha else "--since=7.days"
        result = subprocess.run(
            ["git", "-C", project_path, "log", range_arg, "--format=%H||%s"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return []
        commits: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if "||" not in line:
                continue
            sha, message = line.split("||", 1)
            if message.startswith(("feat(", "feat:")):
                commits.append({"sha": sha, "message": message})
        if commits:
            sha_file.write_text(commits[0]["sha"])
        return commits

    async def _scan(self, ctx: ScanContext) -> int:
        project_path = ctx.config.project_path
        if not project_path or not (Path(project_path) / ".git").is_dir():
            return 0
        commits = self._recent_feat_commits(project_path, self._sha_file(ctx))
        enqueued = 0
        for commit in commits:
            task_id = await ctx.queue.enqueue(
                "announcement",
                {"sha": commit["sha"], "message": commit["message"]},
                dedup_key=commit["sha"],
            )
            if task_id:
                enqueued += 1
        return enqueued
