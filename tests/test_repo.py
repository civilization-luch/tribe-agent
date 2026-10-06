from __future__ import annotations

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
    await rm.ensure("w1", "https://example.com/r", "main")
    await rm.ensure("w1", "https://example.com/r", "main")
    await rm.ensure("w1", "https://example.com/r", "main")
    assert len(calls) == 2


async def test_interval_update_skips_fetch_after_fresh_clone(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.update = "interval"
    rm = _manager(config, tmp_path)
    calls: list[str] = []

    async def _fetch(path: Path, branch: str) -> None:
        calls.append(str(path))

    rm._fetch = _fetch  # type: ignore[method-assign]
    await rm.ensure("w1", "https://example.com/r", "main")
    await rm.ensure("w1", "https://example.com/r", "main")
    assert len(calls) == 0


async def test_interval_update_fetches_when_window_elapsed(tmp_path: Path):
    config = load_config(profile="community")
    config.repo.update = "interval"
    rm = _manager(config, tmp_path)
    calls: list[str] = []

    async def _fetch(path: Path, branch: str) -> None:
        calls.append(str(path))

    rm._fetch = _fetch  # type: ignore[method-assign]
    await rm.ensure("w1", "https://example.com/r", "main")
    rm._last_fetch.pop(str(rm.path_for("w1")))
    await rm.ensure("w1", "https://example.com/r", "main")
    assert len(calls) == 1