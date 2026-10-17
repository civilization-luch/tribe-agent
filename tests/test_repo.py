from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from tribe_agent.core.config import load_config
from tribe_agent.core.repo import RepoManager


def _manager(config, tmp_path: Path) -> RepoManager:
    config.repo.root = str(tmp_path / "repos")
    rm = RepoManager(config)

    async def _clone(url: str, branch: str, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / ".git").mkdir(exist_ok=True)

    rm._clone = _clone  # type: ignore[method-assign]
    return rm


async def test_lazy_update_fetches_each_ensure(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.update = "lazy"
    rm = _manager(config, tmp_path)
    calls: list[str] = []

    async def _fetch(path: Path, branch: str) -> None:
        calls.append(str(path))

    rm._fetch = _fetch  # type: ignore[method-assign]
    await rm.ensure("https://example.com/r", "main")
    await rm.ensure("https://example.com/r", "main")
    await rm.ensure("https://example.com/r", "main")
    assert len(calls) == 2


async def test_interval_update_skips_fetch_after_fresh_clone(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.update = "interval"
    rm = _manager(config, tmp_path)
    calls: list[str] = []

    async def _fetch(path: Path, branch: str) -> None:
        calls.append(str(path))

    rm._fetch = _fetch  # type: ignore[method-assign]
    await rm.ensure("https://example.com/r", "main")
    await rm.ensure("https://example.com/r", "main")
    assert len(calls) == 0


async def test_interval_update_fetches_when_window_elapsed(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.update = "interval"
    rm = _manager(config, tmp_path)
    calls: list[str] = []

    async def _fetch(path: Path, branch: str) -> None:
        calls.append(str(path))

    rm._fetch = _fetch  # type: ignore[method-assign]
    await rm.ensure("https://example.com/r", "main")
    rm._last_fetch.pop(str(rm.path_for("https://example.com/r")))
    await rm.ensure("https://example.com/r", "main")
    assert len(calls) == 1


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_bare_repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("x")
    _git(src, "add", "-A")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=tribe", "commit", "-qm", "init")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=tribe", "commit", "-qm", message)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    ).stdout


async def test_push_direct_to_bound_branch(tmp_path: Path):
    bare = _make_bare_repo(tmp_path)
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "clones")
    rm = RepoManager(config)
    path = await rm.ensure(str(bare), "main")
    assert rm.stored_branch(path) == "main"
    (path / "b.txt").write_text("y")
    _commit(path, "add b")
    await rm.push(path, "main")
    log = await asyncio.to_thread(_git_out, bare, "log", "--oneline", "-2")
    assert "add b" in log


async def test_checkout_branch_and_push_feature(tmp_path: Path):
    bare = _make_bare_repo(tmp_path)
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "clones")
    rm = RepoManager(config)
    path = await rm.ensure(str(bare), "main")
    await rm.checkout_branch(path, "tribe/w1-i1", "main")
    assert rm.head_branch(path) == "tribe/w1-i1"
    (path / "c.txt").write_text("z")
    _commit(path, "add c")
    await rm.push(path, "tribe/w1-i1")
    branches = await asyncio.to_thread(_git_out, bare, "branch")
    assert "tribe/w1-i1" in branches

def test_path_for_is_keyed_by_repository_not_workspace(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    rm = RepoManager(config)
    url = "https://github.com/acme/tribe.git"
    assert rm.path_for(url) == rm.path_for(url)
    assert rm.path_for(url).name == "github.com-acme-tribe"
    assert rm.path_for("https://github.com/acme/other.git") != rm.path_for(url)


def test_path_for_strips_credentials_and_stays_filesystem_safe(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    rm = RepoManager(config)
    slug = rm.path_for("https://x-access-token:s3cret@github.com/acme/tribe.git").name
    assert slug == "github.com-acme-tribe"
    assert "s3cret" not in slug
    deep = rm.path_for("https://example.com/" + "/".join(f"seg{i}" for i in range(40))).name
    assert len(deep) <= 80


async def test_ensure_ignores_workspace_for_clone_path(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    rm = _manager(config, tmp_path)

    async def _fetch(path: Path, branch: str) -> None:
        return None

    rm._fetch = _fetch  # type: ignore[method-assign]
    first = await rm.ensure("https://example.com/r", "main")
    second = await rm.ensure("https://example.com/r", "main")
    assert first == second


def test_resolve_base_branch_prefers_platform_then_config():
    from tribe_agent.core.repo import FALLBACK_BASE_BRANCH, resolve_base_branch

    assert resolve_base_branch("release", "develop") == "release"
    assert resolve_base_branch("", "develop") == "develop"
    assert resolve_base_branch("", "") == FALLBACK_BASE_BRANCH


def test_lock_for_is_stable_per_directory(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.root = str(tmp_path / "repos")
    rm = RepoManager(config)
    assert rm.lock_for(tmp_path / "a") is rm.lock_for(tmp_path / "a")
    assert rm.lock_for(tmp_path / "a") is not rm.lock_for(tmp_path / "b")
