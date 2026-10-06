"""Deployer module — deploy merged PRs to production after human approval.

Scans executed executor tasks that opened a Pull Request. Deploys only when the PR
is merged on GitHub AND the human confirmed the deploy: either the `deploy:approved`
label or a free-form comment (keywords, configurable) from a non-bot author. If the
PR is not merged yet, posts a one-time reminder instead of deploying.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module, ScanContext, Scanner

DEPLOY_LABEL = "deploy:approved"

DEFAULT_KEYWORDS = ["задеплой", "задеплойте", "деплой", "деплойте", "покати", "deploy"]
DEFAULT_NEGATIONS = ["не", "отмена", "отменить", "отбой", "стоп", "stop", "cancel"]

ISSUE_GATE = """
query($id: String!) {
  issue(id: $id) { id status labels comments { id author { id } body } }
}
"""
ADD_COMMENT = """
mutation($id: String!, $body: String!) {
  createComment(parentId: $id, parentType: "issue", body: $body) { id }
}
"""
SET_STATUS = """
mutation($id: String!, $status: String!) {
  setIssueStatus(input: {issueId: $id, status: $status}) { id status }
}
"""


class DeployerModule(Module):
    id = "deployer"

    def scanners(self, config: AppConfig) -> list[Scanner]:
        interval = config.intervals.get("deployer", 300)
        return [Scanner("deployer", interval, self._scan)]

    def tasks(self, config: AppConfig) -> dict[str, Any]:
        return {}

    def _settings(self, ctx: ScanContext) -> dict[str, Any]:
        return ctx.config.module_config("deployer") or {}

    def _ssh_key(self, ctx: ScanContext) -> Path:
        configured = self._settings(ctx).get("ssh_key", "")
        if configured:
            return Path(configured).expanduser()
        return ctx.config.data_path / "keys" / "deploy_rsa"

    async def _scan(self, ctx: ScanContext) -> int:
        settings = self._settings(ctx)
        target_repo = settings.get("repo", "civilization-luch/tribe")
        branch_prefix = (ctx.config.module_config("executor") or {}).get("branch_prefix", "tribe")
        deployed = 0
        for task in await ctx.queue.list_tasks("execute", ("completed", "failed")):
            result = task.result or {}
            if result.get("deployed"):
                continue
            issue_id = task.payload.get("issue_id", "")
            if not issue_id:
                continue
            repo = (result.get("repo") or "").strip() or target_repo
            if target_repo and repo and not _same_repo(repo, target_repo):
                continue
            pr_number = result.get("pr_number") or 0
            if not pr_number:
                branch = _branch_name(branch_prefix, task.payload)
                pr_number = await self._find_pr(ctx, repo, branch)
            if not pr_number:
                continue
            merged = await self._pr_merged(ctx, repo, pr_number)
            approved = await self._approved(ctx, issue_id)
            if not merged:
                if approved and not result.get("reminded"):
                    await self._remind(ctx, issue_id, repo, pr_number)
                    await ctx.queue.update_result(task.id, reminded=True)
                continue
            if not approved:
                continue
            ok, sha = await self._run_deploy(ctx)
            if not ok:
                ctx.logger.warning("deploy failed for issue %s", issue_id)
                continue
            await self._after(ctx, issue_id, sha)
            await ctx.queue.update_result(
                task.id,
                deployed=True,
                deployed_sha=sha,
                deployed_at=datetime.now(UTC).isoformat(),
            )
            deployed += 1
        return deployed

    async def _find_pr(self, ctx: ScanContext, repo: str, branch: str) -> int:
        if not branch:
            return 0
        owner = repo.split("/")[0]
        token = ctx.config.repo.git_token
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"token {token}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"https://api.github.com/repos/{repo}/pulls",
                    params={"head": f"{owner}:{branch}", "state": "all"},
                    headers=headers,
                )
            if response.status_code != 200:
                return 0
            data = response.json()
            if isinstance(data, list) and data:
                return int(data[0].get("number") or 0)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("PR lookup failed for %s %s: %s", repo, branch, exc)
        return 0

    async def _pr_merged(self, ctx: ScanContext, repo: str, pr_number: int) -> bool:
        token = ctx.config.repo.git_token
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"token {token}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
                    headers=headers,
                )
            if response.status_code != 200:
                return False
            data = response.json()
            return bool(data.get("merged")) and data.get("state") == "closed"
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("PR check failed for %s#%s: %s", repo, pr_number, exc)
            return False

    async def _approved(self, ctx: ScanContext, issue_id: str) -> bool:
        try:
            data = await ctx.client.gql(ISSUE_GATE, {"id": issue_id})
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("issue gate check failed for %s: %s", issue_id, exc)
            return False
        issue = (data or {}).get("issue") or {}
        if not issue:
            return False
        if issue.get("status") != "open":
            return False
        labels = issue.get("labels") or []
        if DEPLOY_LABEL in labels:
            return True
        settings = self._settings(ctx)
        keywords = [str(k).lower() for k in settings.get("keywords", DEFAULT_KEYWORDS)]
        negations = [str(k).lower() for k in settings.get("negations", DEFAULT_NEGATIONS)]
        bot_id = ctx.config.community.user_id or ""
        for comment in issue.get("comments") or []:
            author = (comment.get("author") or {})
            author_id = author.get("id") or ""
            if author_id and author_id == bot_id:
                continue
            text = (comment.get("body") or "").lower()
            if any(kw in text for kw in keywords) and not any(
                ng in text for ng in negations
            ):
                return True
        return False

    async def _remind(self, ctx: ScanContext, issue_id: str, repo: str, pr_number: int) -> None:
        body = (
            f"Deploy approval received, but PR {repo}#{pr_number} is not merged "
            "into main yet — merge it and the deploy will run automatically."
        )
        try:
            await ctx.client.gql(ADD_COMMENT, {"id": issue_id, "body": body})
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("deploy reminder failed for %s: %s", issue_id, exc)

    async def _run_deploy(self, ctx: ScanContext) -> tuple[bool, str]:
        settings = self._settings(ctx)
        host = settings.get("ssh_host", "64.226.102.165")
        user = settings.get("ssh_user", "deploy")
        key_path = self._ssh_key(ctx)
        if not key_path.is_file():
            ctx.logger.warning("deploy key missing: %s", key_path)
            return False, ""
        command = [
            "ssh",
            "-i", str(key_path),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            f"{user}@{host}",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), timeout=240)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("deploy ssh failed: %s", exc)
            return False, ""
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        match = re.search(r"deployed\s+([0-9a-f]{7,40})", stdout)
        if process.returncode == 0 and match:
            return True, match.group(1)
        ctx.logger.warning(
            "deploy ssh rc=%s out=%r err=%r",
            process.returncode,
            stdout[-300:],
            stderr[-300:],
        )
        return False, ""

    async def _after(self, ctx: ScanContext, issue_id: str, sha: str) -> None:
        body = f"Deployed to prod: main `{sha}`."
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await ctx.client.gql(ADD_COMMENT, {"id": issue_id, "body": body})
                await ctx.client.gql(SET_STATUS, {"id": issue_id, "status": "resolved"})
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(3)
        ctx.logger.warning("deploy follow-up failed for %s: %s", issue_id, last_error)


def _same_repo(a: str, b: str) -> bool:
    def normalized(repo: str) -> str:
        value = repo.strip().removesuffix(".git")
        parts = [part for part in value.split("/") if part]
        return "/".join(parts[-2:])

    return normalized(a) == normalized(b)


def _branch_name(prefix: str, payload: dict[str, Any]) -> str:
    workspace_id = payload.get("workspace_id", "") or ""
    issue_id = payload.get("issue_id", "") or ""
    if not issue_id:
        return ""
    return f"{prefix}/{workspace_id[:8]}-i{issue_id[:8]}"