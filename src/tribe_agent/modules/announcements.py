"""Announcements module — turn feature commits into announcement discussions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tribe_agent.core.client.graphql import WORKSPACE_REPO
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
                "@tribe-announcements",
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
                agent="tribe-announcements",
                build_prompt=build_prompt,
            )
        }

    def _sha_file(self, ctx: ScanContext, key: str) -> Path:
        if key == "project":
            return ctx.config.data_path / "announcements_sha.txt"
        return ctx.config.data_path / f"announcements_sha_{key}.txt"

    def _recent_feat_commits(self, repo_path: str, sha_file: Path) -> list[dict[str, str]]:
        last_sha = sha_file.read_text().strip() if sha_file.is_file() else ""
        range_arg = f"{last_sha}..HEAD" if last_sha else "--since=7.days"
        result = subprocess.run(
            ["git", "-C", repo_path, "log", range_arg, "--format=%H||%s"],
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

    async def _resolve_repo(
        self, ctx: ScanContext, workspace_id: str, cache: dict[str, tuple[str, str]]
    ) -> tuple[str, str]:
        if workspace_id in cache:
            return cache[workspace_id]
        url, branch = "", ""
        try:
            data = await ctx.client.gql(WORKSPACE_REPO, {"id": workspace_id})
            workspace = data.get("workspace")
            workspace = workspace if isinstance(workspace, dict) else {}
            url = workspace.get("repoUrl") or ""
            branch = workspace.get("repoBranch") or ""
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("workspace repo lookup failed for %s: %s", workspace_id, exc)
        if not url and ctx.config.repo.default_url:
            url = ctx.config.repo.default_url
            branch = ctx.config.repo.default_branch or branch
        cache[workspace_id] = (url, branch)
        return url, branch

    async def _repo_sources(self, ctx: ScanContext, settings: dict[str, Any]) -> list[tuple[str, Path]]:
        sights: list[tuple[str, Path]] = []
        workspace_ids = settings.get("workspaces") or []
        cache: dict[str, tuple[str, str]] = {}
        for ws_id in workspace_ids:
            url, branch = await self._resolve_repo(ctx, ws_id, cache)
            if url:
                try:
                    path = await ctx.repo.ensure(ws_id, url, branch)
                    sights.append((ws_id, path))
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning("announcements repo failed for %s: %s", ws_id, exc)
            elif ctx.config.project_path:
                sights.append((f"pp:{ws_id}", Path(ctx.config.project_path).expanduser().resolve()))
        if sights:
            return sights
        if ctx.config.repo.default_url:
            try:
                path = await ctx.repo.ensure("", ctx.config.repo.default_url, ctx.config.repo.default_branch or "")
                return [("default", path)]
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("announcements default repo failed: %s", exc)
                return []
        if ctx.config.project_path:
            return [("project", Path(ctx.config.project_path).expanduser().resolve())]
        return []

    async def _scan(self, ctx: ScanContext) -> int:
        settings = ctx.config.module_config("announcements")
        enqueued = 0
        for key, path in await self._repo_sources(ctx, settings):
            if not (path / ".git").is_dir():
                continue
            commits = self._recent_feat_commits(str(path), self._sha_file(ctx, key))
            for commit in commits:
                task_id = await ctx.queue.enqueue(
                    "announcement",
                    {"sha": commit["sha"], "message": commit["message"], "repo": key},
                    dedup_key=f"{key}:{commit['sha']}",
                )
                if task_id:
                    enqueued += 1
        return enqueued