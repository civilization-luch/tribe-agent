"""Repository manager — shallow, lazy clones plus per-repository locking."""

from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
import time
from pathlib import Path

from tribe_agent.core.config import AppConfig

FALLBACK_BASE_BRANCH = "main"


def resolve_base_branch(platform_branch: str, configured_branch: str) -> str:
    """Platform binding wins, then configuration, then `main`.

    The order matters: a task whose base branch is silently wrong lands its
    commits on somebody else's branch, so every caller must agree on it.
    """
    return platform_branch or configured_branch or FALLBACK_BASE_BRANCH


def _slug(url: str) -> str:
    cleaned = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url)
    cleaned = re.sub(r"^[^/@]+@", "", cleaned)
    cleaned = re.sub(r"\.git$", "", cleaned.rstrip("/"))
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", cleaned).strip("-")
    if len(slug) > 80:
        digest = hashlib.sha1(cleaned.encode()).hexdigest()[:10]
        tail = cleaned.rsplit("/", 1)[-1]
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{tail}-{digest}")
    return slug or "repo"


def auth_url(url: str, token: str) -> str:
    if token and url.startswith("https://") and "@" not in url:
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


class RepoManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        root = Path(config.repo.root).expanduser()
        self.root = root if root.is_absolute() else (Path.cwd() / root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_fetch: dict[str, float] = {}
        self._branches: dict[str, str] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def lock_for(self, target: str | Path) -> asyncio.Lock:
        """Lock the working tree at `target` for the whole task, not just the clone.

        Branch preparation, the agent run and the push all mutate the same
        checkout; holding only one of them lets two tasks reset each other's
        HEAD mid-run.
        """
        return self._lock(str(Path(target).expanduser()))

    def path_for(self, url: str) -> Path:
        return self.root / _slug(url)

    def _auth_url(self, url: str) -> str:
        return auth_url(url, self.config.repo.git_token)

    async def ensure(self, url: str, branch: str = "") -> Path:
        if not url:
            if self.config.project_path:
                return Path(self.config.project_path).expanduser().resolve()
            raise ValueError("No repository bound to workspace and no project_path configured")

        path = self.path_for(url)
        async with self.lock_for(path):
            if not (path / ".git").is_dir():
                await self._clone(url, branch, path)
                self._last_fetch[str(path)] = time.monotonic()
            elif self.config.repo.update == "interval":
                if self._should_refetch(str(path)):
                    await self._fetch(path, branch)
            elif self.config.repo.update == "lazy":
                await self._fetch(path, branch)
            if branch:
                self._branches[str(path)] = branch
        return path

    def stored_branch(self, path: str | Path) -> str:
        return self._branches.get(str(path), "")

    async def checkout_branch(self, path: Path, name: str, base: str = "") -> None:
        cmd = ["git", "-C", str(path), "switch", "-c", name]
        if base:
            cmd.append(base)
        await asyncio.to_thread(self._git, cmd, path)

    async def create_branch_from(self, path: Path, name: str, base: str) -> None:
        ref = base or "main"
        await asyncio.to_thread(
            self._git, ["git", "-C", str(path), "fetch", "--prune", "origin", ref], path
        )
        await asyncio.to_thread(self._git, ["git", "-C", str(path), "reset", "--hard"], path)
        await asyncio.to_thread(
            self._git, ["git", "-C", str(path), "checkout", "-B", name, f"origin/{ref}"], path
        )

    async def push(self, path: Path, target: str) -> None:
        await asyncio.to_thread(
            self._git,
            ["git", "-C", str(path), "push", "origin", f"HEAD:refs/heads/{target}"],
            path,
        )

    def head_branch(self, path: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def head_sha(self, path: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _should_refetch(self, path: str) -> bool:
        now = time.monotonic()
        interval = self.config.repo.refetch_interval_minutes * 60
        if now - self._last_fetch.get(path, 0.0) < interval:
            return False
        self._last_fetch[path] = now
        return True

    async def _clone(self, url: str, branch: str, path: Path) -> None:
        cmd = ["git", "clone"]
        if self.config.repo.strategy == "shallow":
            cmd += ["--depth", "1"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [self._auth_url(url), str(path)]
        await asyncio.to_thread(self._git, cmd, None)

    async def _fetch(self, path: Path, branch: str) -> None:
        await asyncio.to_thread(self._git, ["git", "-C", str(path), "fetch", "--prune", "origin"], path)
        if branch:
            await asyncio.to_thread(
                self._git, ["git", "-C", str(path), "checkout", branch], path
            )

    def _git(self, cmd: list[str], cwd: Path | None) -> None:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            safe = [c.split("@")[-1] if "@" in c and c.startswith("https://") else c for c in cmd]
            raise RuntimeError(f"git failed: {' '.join(safe)}: {result.stderr.strip()[:300]}")
