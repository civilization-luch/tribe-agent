from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import pytest

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
from tribe_agent.core.queue import TaskQueue
from tribe_agent.core.repo import RepoManager
from tribe_agent.modules.announcements import AnnouncementsModule


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def feat_bare(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("x")
    _git(src, "add", "-A")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=tribe", "commit", "-qm", "chore: init")
    (src / "b.txt").write_text("y")
    _git(src, "add", "-A")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=tribe", "commit", "-qm", "feat: cool feature")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


class FakeClient:
    def __init__(self, workspace: dict) -> None:
        self._workspace = workspace

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        if "workspace(id" in query:
            return {"workspace": self._workspace}
        return {}


async def _scan(tmp_path: Path, config, client) -> int:
    config.data_dir = str(tmp_path / "data")
    queue = TaskQueue(tmp_path / "data" / "agent.db")
    await queue.connect()
    ctx = ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=queue,
        repo=RepoManager(config),
        logger=logging.getLogger("test"),
    )
    count1 = await AnnouncementsModule()._scan(ctx)
    pending1 = await queue.count_pending("announcement")
    count2 = await AnnouncementsModule()._scan(ctx)
    pending2 = await queue.count_pending("announcement")
    await queue.close()
    return count1, pending1, count2, pending2


async def test_announcements_workspace_repos(tmp_path: Path, feat_bare: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    config.module_settings["announcements"] = {"workspaces": ["w1"]}
    client = FakeClient({"id": "w1", "repoUrl": str(feat_bare), "repoBranch": "main"})
    c1, p1, c2, p2 = await _scan(tmp_path, config, client)
    assert c1 >= 1 and p1 >= 1
    assert c2 == 0 and p2 == p1  # sha file advanced


async def test_announcements_default_repo(tmp_path: Path, feat_bare: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    config.repo.default_url = str(feat_bare)
    config.repo.default_branch = "main"
    client = FakeClient({})
    c1, p1, _, _ = await _scan(tmp_path, config, client)
    assert c1 >= 1 and p1 >= 1


async def test_announcements_project_path_fallback(tmp_path: Path, feat_bare: Path):
    clone = tmp_path / "work"
    await asyncio.to_thread(
        lambda: subprocess.run(["git", "clone", "-q", str(feat_bare), str(clone)], check=True)
    )
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    config.project_path = str(clone)
    client = FakeClient({})
    c1, p1, c2, p2 = await _scan(tmp_path, config, client)
    assert c1 >= 1 and p1 >= 1
    assert c2 == 0 and p2 == p1


async def test_announcements_no_source_is_noop(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    client = FakeClient({})
    c1, p1, _, _ = await _scan(tmp_path, config, client)
    assert c1 == 0 and p1 == 0