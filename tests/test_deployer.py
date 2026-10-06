from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
from tribe_agent.core.queue import Task
from tribe_agent.core.scheduler import _pr_info
from tribe_agent.modules.deployer import DeployerModule, _same_repo


def _task(result: dict | None = None, issue_id: str = "i1") -> Task:
    return Task(
        id=7,
        task_type="execute",
        status="completed",
        community_id="platform",
        workspace_id="w1",
        repo_path="",
        payload={"issue_id": issue_id},
        created_at="",
        result=result or {},
    )


class StubQueue:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks
        self.updated: list[tuple[int, dict]] = []

    async def list_completed(self, task_type: str) -> list[Task]:
        return [t for t in self.tasks if t.task_type == task_type]

    async def update_result(self, task_id: int, **patches: object) -> None:
        self.updated.append((task_id, dict(patches)))


def _ctx(queue: StubQueue | None = None) -> ScanContext:
    config = load_config(profile="community")
    config.module_settings["deployer"] = {
        "ssh_host": "host",
        "ssh_user": "deploy",
        "ssh_key": str(config.data_path / "keys" / "deploy_rsa"),
        "repo": "civilization-luch/tribe",
    }
    config.repo.git_token = "tok"
    return ScanContext(
        config=config,
        client=AsyncMock(),
        queue=queue or StubQueue([]),  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )


def _module() -> DeployerModule:
    module = DeployerModule()
    module._pr_merged = AsyncMock(return_value=True)
    module._approved = AsyncMock(return_value=True)
    module._run_deploy = AsyncMock(return_value=(True, "abc1234"))
    module._after = AsyncMock()
    module._remind = AsyncMock()
    return module


async def test_scan_deploys_when_merged_and_approved():
    module = _module()
    queue = StubQueue([_task(result={"pr_number": 9, "repo": "civilization-luch/tribe"})])
    assert await module._scan(_ctx(queue)) == 1
    module._run_deploy.assert_awaited_once()
    module._after.assert_awaited_once()
    assert queue.updated[0][1]["deployed"] is True
    assert queue.updated[0][1]["deployed_sha"] == "abc1234"


async def test_scan_skips_when_pr_not_merged():
    module = _module()
    module._pr_merged = AsyncMock(return_value=False)
    queue = StubQueue([_task(result={"pr_number": 9, "repo": "civilization-luch/tribe"})])
    assert await module._scan(_ctx(queue)) == 0
    module._run_deploy.assert_not_awaited()


async def test_scan_skips_when_not_approved():
    module = _module()
    module._approved = AsyncMock(return_value=False)
    queue = StubQueue([_task(result={"pr_number": 9, "repo": "civilization-luch/tribe"})])
    assert await module._scan(_ctx(queue)) == 0
    module._run_deploy.assert_not_awaited()


async def test_scan_reminds_once_when_approved_but_not_merged():
    module = _module()
    module._pr_merged = AsyncMock(return_value=False)
    queue = StubQueue([_task(result={"pr_number": 9, "repo": "civilization-luch/tribe"})])
    assert await module._scan(_ctx(queue)) == 0
    module._remind.assert_awaited_once()
    assert queue.updated == [(7, {"reminded": True})]


async def test_scan_does_not_remind_twice():
    module = _module()
    module._pr_merged = AsyncMock(return_value=False)
    queue = StubQueue(
        [
            _task(
                result={
                    "pr_number": 9,
                    "repo": "civilization-luch/tribe",
                    "reminded": True,
                }
            )
        ]
    )
    assert await module._scan(_ctx(queue)) == 0
    module._remind.assert_not_awaited()
    assert queue.updated == []


async def test_scan_skips_when_already_deployed():
    module = _module()
    queue = StubQueue(
        [_task(result={"pr_number": 9, "repo": "civilization-luch/tribe", "deployed": True})]
    )
    assert await module._scan(_ctx(queue)) == 0
    module._run_deploy.assert_not_awaited()


async def test_scan_skips_when_repo_does_not_match():
    module = _module()
    queue = StubQueue([_task(result={"pr_number": 9, "repo": "other/repo"})])
    assert await module._scan(_ctx(queue)) == 0
    module._run_deploy.assert_not_awaited()


async def test_scan_skips_when_no_pr_number():
    module = _module()
    queue = StubQueue([_task(result={"repo": "civilization-luch/tribe"})])
    assert await module._scan(_ctx(queue)) == 0


def _issue(
    status: str = "open", labels: list[str] | None = None, comments: list[dict] | None = None
) -> dict:
    return {"id": "i1", "status": status, "labels": labels or [], "comments": comments or []}


async def _approved(issue: dict, keywords_override: list[str] | None = None) -> bool:
    module = DeployerModule()
    config = load_config(profile="community")
    config.community.user_id = "BOT"
    if keywords_override:
        config.module_settings["deployer"] = {"keywords": keywords_override}
    client = AsyncMock()

    async def gql(query: str, variables: dict):
        return {"issue": issue}

    client.gql = gql
    ctx = ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=StubQueue([]),  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    return await module._approved(ctx, "i1")


async def test_approved_by_label():
    assert await _approved(_issue(labels=["deploy:approved"])) is True


async def test_approved_by_human_freeform_comment():
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "задеплой"}]
    assert await _approved(_issue(comments=human)) is True


async def test_approved_by_human_freeform_variants():
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "деплой, ок"}]
    assert await _approved(_issue(comments=human)) is True
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "deploy it now"}]
    assert await _approved(_issue(comments=human)) is True
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "покати на прод"}]
    assert await _approved(_issue(comments=human)) is True


async def test_approved_rejects_negation():
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "не деплой"}]
    assert await _approved(_issue(comments=human)) is False
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "отмена деплоя"}]
    assert await _approved(_issue(comments=human)) is False


async def test_approved_ignores_bot_comment():
    bot = [{"id": "c1", "author": {"id": "BOT"}, "body": "деплой"}]
    assert await _approved(_issue(comments=bot)) is False


async def test_approved_no_marker_comment():
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "спасибо"}]
    assert await _approved(_issue(comments=human)) is False


async def test_approved_closed_issue_rejected():
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "деплой"}]
    assert await _approved(_issue(status="closed", comments=human)) is False


async def test_approved_uses_configured_keywords():
    human = [{"id": "c1", "author": {"id": "HUMAN"}, "body": "деплой"}]
    assert await _approved(_issue(comments=human), keywords_override=["погнали"]) is False
    human = [{"id": "c2", "author": {"id": "HUMAN"}, "body": "погнали"}]
    assert await _approved(_issue(comments=human), keywords_override=["погнали"]) is True


async def test_run_deploy_returns_false_when_key_missing():
    module = DeployerModule()
    config = load_config(profile="community")
    config.module_settings["deployer"] = {
        "ssh_key": str(config.data_path / "keys" / "nope_rsa")
    }
    ctx = ScanContext(
        config=config,
        client=AsyncMock(),
        queue=StubQueue([]),  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )
    ok, sha = await module._run_deploy(ctx)
    assert ok is False
    assert sha == ""


def test_same_repo_normalizes_suffix_and_org():
    assert _same_repo("civilization-luch/tribe", "civilization-luch/tribe.git")
    assert _same_repo("civilization-luch/tribe.git", "civilization-luch/tribe")
    assert not _same_repo("a/repo", "b/repo")


def test_pr_info_parses_url():
    assert _pr_info("") == ("", 0)
    assert _pr_info("https://github.com/civilization-luch/tribe/pull/9") == (
        "civilization-luch/tribe",
        9,
    )
    assert _pr_info("not a url") == ("", 0)