from __future__ import annotations

import logging

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
from tribe_agent.core.queue import Task
from tribe_agent.modules import load_modules
from tribe_agent.modules.announcements import AnnouncementsModule
from tribe_agent.modules.classify import ClassifyModule
from tribe_agent.modules.executor import ExecutorModule
from tribe_agent.modules.notifications import NotificationsModule
from tribe_agent.modules.self_heal import SelfHealModule


def _task(task_type: str, payload: dict, **kwargs) -> Task:
    return Task(
        id=kwargs.pop("id", 1),
        task_type=task_type,
        status="pending",
        community_id=kwargs.pop("community_id", "c1"),
        workspace_id=kwargs.pop("workspace_id", "w1"),
        repo_path=kwargs.pop("repo_path", ""),
        payload=payload,
    )


def test_community_modules_register_scanners_and_tasks():
    config = load_config(profile="community")
    modules = load_modules(config)
    ids = [m.id for m in modules]
    assert ids == ["notifications", "announcements", "executor", "self_heal"]
    for module in modules:
        assert module.scanners(config)
        assert module.tasks(config)


def test_agent_names_are_namespaced():
    config = load_config(profile="platform")
    agents = {}
    for module in load_modules(config):
        for spec in module.tasks(config).values():
            agents[spec.task_type] = spec.agent
    assert agents["notification"] == "tribe-notifications"
    assert agents["announcement"] == "tribe-announcements"
    assert agents["classify"] == "tribe-classifier"
    assert agents["execute"] == "tribe-task-executor"
    assert agents["self_heal"] == "tribe-self-healer"


def test_notifications_prompt_contains_events():
    module = NotificationsModule()
    prompt = module._build_prompt(
        [_task("notification", {"event_type": "UserMentioned", "title": "Hi", "link": "/x", "body": "yo"})]
    )
    assert "@tribe-notifications" in prompt
    assert "UserMentioned" in prompt
    assert "Link: /x" in prompt


def test_classify_prompt_contains_issue():
    module = ClassifyModule()
    prompt = module._build_prompt(
        [_task("classify", {"issue_id": "i1", "title": "Bug", "description": "broken"})]
    )
    assert "@tribe-classifier" in prompt
    assert "issue_id=i1" in prompt
    assert "Bug" in prompt


def test_executor_prompt_contains_issue_and_commands():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"commands": {"test": "pytest"}}
    spec = ExecutorModule().tasks(config)["execute"]
    prompt = spec.build_prompt(
        [_task("execute", {"issue_id": "i2", "title": "Fix", "description": "do it"})]
    )
    assert "@tribe-task-executor" in prompt
    assert "issue_id=i2" in prompt
    assert "test='pytest'" in prompt


def test_announcements_prompt_contains_config():
    config = load_config(profile="community")
    config.module_settings["announcements"] = {"frequency": "daily", "telegram_chat_id": "42"}
    spec = AnnouncementsModule().tasks(config)["announcement"]
    prompt = spec.build_prompt([_task("announcement", {"sha": "abcdef12", "message": "feat: x"})])
    assert "Frequency: daily" in prompt
    assert "abcdef12" in prompt


class StubScalarClient:
    def __init__(self, total: int, open_issues: list[dict] | None = None) -> None:
        self._total = total
        self._open = open_issues or []

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        if "totalIssues" in query:
            return {"totalIssues": self._total}
        if "issues(" in query:
            return {"issues": self._open}
        return {}


class StubScalarQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple] = []

    async def enqueue(self, task_type: str, payload: dict, **kwargs) -> int:
        self.enqueued.append((task_type, payload, kwargs))
        return len(self.enqueued)


def _scan_ctx(client, queue) -> ScanContext:
    config = load_config(profile="community")
    config.community.communities = ["platform"]
    return ScanContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
    )


def test_self_heal_prompt_contains_no_duplicate_guidance():
    module = SelfHealModule()
    spec = module.tasks(load_config(profile="community"))["self_heal"]
    prompt = spec.build_prompt([_task("self_heal", {"community_id": "c1", "open_issues": 39})])
    assert "Do NOT create a new" in prompt
    assert "Issue backlog growing" in prompt


async def test_self_heal_skips_when_open_health_issue_exists():
    module = SelfHealModule()
    client = StubScalarClient(
        39, [{"id": "h1", "title": "Issue backlog growing", "labels": ["auto", "health"]}]
    )
    queue = StubScalarQueue()
    assert await module._scan(_scan_ctx(client, queue)) == 0
    assert queue.enqueued == []


async def test_self_heal_enqueues_when_no_open_health_issue():
    module = SelfHealModule()
    client = StubScalarClient(39, [{"id": "x", "title": "Other", "labels": []}])
    queue = StubScalarQueue()
    assert await module._scan(_scan_ctx(client, queue)) == 1
    assert queue.enqueued[0][0] == "self_heal"
    assert queue.enqueued[0][1]["open_issues"] == 39


async def test_self_heal_skips_below_threshold():
    module = SelfHealModule()
    client = StubScalarClient(5, [])
    queue = StubScalarQueue()
    assert await module._scan(_scan_ctx(client, queue)) == 0
    assert queue.enqueued == []
