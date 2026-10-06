from __future__ import annotations

import logging
from pathlib import Path

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
from tribe_agent.core.queue import TaskQueue
from tribe_agent.modules.executor import ExecutorModule


class FakeClient:
    def __init__(self, workspace: dict, issues: list | None = None) -> None:
        self._workspace = workspace
        self._issues = issues
        self.workspace_queries = 0

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        if "issues(assigneeId" in query:
            if self._issues is not None:
                return {"issues": self._issues}
            return {
                "issues": [
                    {
                        "id": "i1",
                        "title": "T",
                        "description": "D",
                        "workspaceId": "w1",
                        "communityId": "c1",
                    }
                ]
            }
        if "workspace(id" in query:
            self.workspace_queries += 1
            return {"workspace": self._workspace}
        return {}


class FakeRepo:
    def __init__(self) -> None:
        self.ensure_calls: list[tuple] = []

    async def ensure(self, workspace_id: str, url: str, branch: str = "") -> Path:
        self.ensure_calls.append((workspace_id, url, branch))
        return Path("/tmp/repo")


async def _run_scan(tmp_path: Path, workspace: dict, module_settings: dict | None = None):
    config = load_config(profile="community")
    config.community.user_id = "bot1"
    config.data_dir = str(tmp_path)
    if module_settings:
        config.module_settings.update(module_settings)
    queue = TaskQueue(tmp_path / "agent.db")
    await queue.connect()
    client = FakeClient(workspace)
    repo = FakeRepo()
    ctx = ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=queue,
        repo=repo,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    count = await ExecutorModule()._scan(ctx)
    tasks = await queue.dequeue_batch("execute", 10)
    await queue.close()
    return count, tasks, client, repo


async def test_executor_uses_workspace_repo_from_graphql(tmp_path: Path):
    count, tasks, _client, repo = await _run_scan(
        tmp_path, {"id": "w1", "repoUrl": "https://github.com/o/r", "repoBranch": "dev"}
    )
    assert count == 1
    assert repo.ensure_calls == [("w1", "https://github.com/o/r", "dev")]
    assert tasks[0].repo_path == "/tmp/repo"


async def test_executor_local_override_wins(tmp_path: Path):
    count, _tasks, client, repo = await _run_scan(
        tmp_path,
        {"id": "w1", "repoUrl": "https://graphql.example/r", "repoBranch": "main"},
        module_settings={"executor": {"repos": {"w1": {"url": "https://override/r", "branch": "x"}}}},
    )
    assert count == 1
    assert repo.ensure_calls == [("w1", "https://override/r", "x")]
    assert client.workspace_queries == 0


async def test_executor_filters_to_configured_communities(tmp_path: Path):
    issues = [
        {"id": "i1", "title": "A", "description": "", "workspaceId": "w1", "communityId": "c1"},
        {"id": "i2", "title": "B", "description": "", "workspaceId": "w9", "communityId": "c9"},
    ]
    config = load_config(profile="community")
    config.community.user_id = "bot1"
    config.community.communities = ["c1"]
    config.data_dir = str(tmp_path)
    queue = TaskQueue(tmp_path / "agent.db")
    await queue.connect()
    client = FakeClient({}, issues)
    repo = FakeRepo()
    ctx = ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=queue,
        repo=repo,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    count = await ExecutorModule()._scan(ctx)
    tasks = await queue.dequeue_batch("execute", 10)
    await queue.close()
    assert count == 1
    assert [t.payload.get("issue_id") for t in tasks] == ["i1"]


async def test_executor_ignores_all_communities_when_not_configured(tmp_path: Path):
    issues = [
        {"id": "i1", "title": "A", "description": "", "workspaceId": "w1", "communityId": "c1"},
        {"id": "i2", "title": "B", "description": "", "workspaceId": "w9", "communityId": "c9"},
    ]
    config = load_config(profile="community")
    config.community.user_id = "bot1"
    config.community.communities = []
    config.data_dir = str(tmp_path)
    queue = TaskQueue(tmp_path / "agent.db")
    await queue.connect()
    client = FakeClient({}, issues)
    repo = FakeRepo()
    ctx = ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=queue,
        repo=repo,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    count = await ExecutorModule()._scan(ctx)
    tasks = await queue.dequeue_batch("execute", 10)
    await queue.close()
    assert count == 2
    assert {t.payload.get("issue_id") for t in tasks} == {"i1", "i2"}
