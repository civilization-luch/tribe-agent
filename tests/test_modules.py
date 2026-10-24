from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tribe_agent.core.config import load_config
from tribe_agent.core.module import ScanContext
from tribe_agent.core.queue import Task
from tribe_agent.modules import load_modules
from tribe_agent.modules.announcements import AnnouncementsModule
from tribe_agent.modules.classify import ClassifyModule
from tribe_agent.modules.executor import ExecutorModule
from tribe_agent.modules.notifications import NotificationsModule
from tribe_agent.modules.self_heal import SelfHealModule
from tribe_agent.modules.support import SupportModule

_PKG = Path(__file__).resolve().parents[1] / "src" / "tribe_agent"


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
    assert ids == ["notifications", "announcements", "executor", "self_heal", "support"]
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
    assert agents["support"] == "tribe-support"


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


def _scan_ctx(client, queue, config=None) -> ScanContext:
    config = config or load_config(profile="community")
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


class StubSupportClient:
    def __init__(self, settings: dict | None = None, requests: list[dict] | None = None) -> None:
        self._settings = settings or {}
        self._requests = requests or []

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        if "community(" in query:
            return {"community": {"id": "c1", "settings": self._settings}}
        if "supportRequests" in query:
            return {"supportRequests": self._requests}
        return {}


def _support_request() -> dict:
    return {
        "id": "r1",
        "communityId": "c1",
        "sourceId": "s1",
        "targetGroupId": "g1",
        "title": "Broken button",
        "body": "The button does not work",
        "status": "new",
        "labels": ["kind:problem"],
        "createdAt": "2026-10-24T10:00:00Z",
    }


async def test_support_scanner_skips_when_agent_disabled():
    module = SupportModule()
    config = load_config(profile="community")
    config.module_settings["support"] = {"communities": ["platform"]}
    client = StubSupportClient(requests=[_support_request()])
    queue = StubScalarQueue()
    assert await module._scan(_scan_ctx(client, queue, config)) == 0
    assert queue.enqueued == []


async def test_support_scanner_honors_community_settings():
    module = SupportModule()
    config = load_config(profile="community")
    config.module_settings["support"] = {"communities": ["platform"], "agent_enabled": True}
    client = StubSupportClient(
        settings={"support.agent_enabled": False},
        requests=[_support_request()],
    )
    queue = StubScalarQueue()
    assert await module._scan(_scan_ctx(client, queue, config)) == 0
    assert queue.enqueued == []


async def test_support_scanner_enqueues_enabled_request():
    module = SupportModule()
    config = load_config(profile="community")
    config.module_settings["support"] = {
        "communities": ["platform"],
        "agent_enabled": True,
        "auto_convert": True,
        "workspace_id": "w1",
    }
    client = StubSupportClient(
        settings={"support.agent_claim_ttl_seconds": 600},
        requests=[_support_request()],
    )
    queue = StubScalarQueue()
    assert await module._scan(_scan_ctx(client, queue, config)) == 1
    task_type, payload, kwargs = queue.enqueued[0]
    assert task_type == "support"
    assert payload["support_request_id"] == "r1"
    assert payload["community_id"] == "c1"
    assert payload["auto_convert"] is True
    assert payload["workspace_id"] == "w1"
    assert payload["claim_ttl_seconds"] == 600
    assert kwargs["dedup_key"] == "r1"


async def test_support_scanner_skips_active_claim():
    module = SupportModule()
    config = load_config(profile="community")
    config.module_settings["support"] = {"communities": ["platform"], "agent_enabled": True}
    request = _support_request()
    request["claimedBy"] = "human-1"
    request["claimExpiresAt"] = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    client = StubSupportClient(requests=[request])
    queue = StubScalarQueue()

    assert await module._scan(_scan_ctx(client, queue, config)) == 0
    assert queue.enqueued == []


def test_support_prompt_contains_claim_and_dry_run():
    module = SupportModule()
    spec = module.tasks(load_config(profile="platform"))["support"]
    prompt = spec.build_prompt(
        [
            _task(
                "support",
                {
                    "support_request_id": "r1",
                    "community_id": "c1",
                    "title": "Broken",
                    "body": "It fails",
                    "labels": ["kind:problem"],
                    "dry_run": True,
                    "auto_convert": False,
                    "claim_ttl_seconds": 900,
                },
            )
        ]
    )
    assert "@tribe-support" in prompt
    assert "claim_support_request" in prompt
    assert "support_request_id=r1" in prompt
    assert "DRY RUN" in prompt


def test_support_agent_and_skill_are_packaged():
    agent = _read_packaged("opencode/agents/tribe-support.md")
    skill = _read_packaged("opencode/skills/tribe-support-triage/SKILL.md")
    assert "claim_support_request" in agent
    assert "DRY RUN" in agent
    assert "release_support_request_claim" in skill


def _read_packaged(rel: str) -> str:
    return (_PKG / rel).read_text()


def test_classifier_agent_self_assigns_accepted():
    text = _read_packaged("opencode/agents/tribe-classifier.md")
    assert "assign_issue" in text
    assert "whoami" in text
    assert "MUST assign the issue to yourself" in text


def test_classifier_never_reports_fixes():
    text = _read_packaged("opencode/agents/tribe-classifier.md")
    assert "NEVER edit code" in text
    assert "NEVER" in text and "commit" in text.lower()


def test_executor_agent_must_not_invent_shas():
    text = _read_packaged("opencode/agents/tribe-task-executor.md")
    assert "NEVER invent" in text
    assert "ONLY agent who implements code and reports fixes" in text
    assert "no commit found yet" in text


def test_executor_agent_does_not_resolve_with_open_pr():
    text = _read_packaged("opencode/agents/tribe-task-executor.md")
    assert 'UNLESS a Pull Request was opened' in text
    assert 'leave the issue\n   `open`' in text or "leave the issue" in text
    assert "awaiting merge + deploy" in text


def test_executor_agent_never_deploys():
    text = _read_packaged("opencode/agents/tribe-task-executor.md")
    assert "Deployment is OUTSIDE your scope" in text
    assert "NEVER attempt to deploy" in text
    assert "in prod" in text or '"deployed"' in text


def test_classifier_skill_self_assigns_on_accepted():
    text = _read_packaged("opencode/skills/tribe-classifier/SKILL.md")
    assert "assign the issue to yourself" in text
    assert "whoami" in text
    assert "NEVER reports commits/SHAs" in text


def test_notifications_agent_never_proposes_plans():
    text = _read_packaged("opencode/agents/tribe-notifications.md")
    assert "do NOT post an implementation plan" in text
    assert "never propose a plan" in text
    assert "NOT the implementer" in text


def test_notifications_skill_never_proposes_plans():
    text = _read_packaged("opencode/skills/tribe-notifications/SKILL.md")
    assert "never post solution plans or ask for approval" in text
