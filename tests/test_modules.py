from __future__ import annotations

from tribe_agent.core.config import load_config
from tribe_agent.core.queue import Task
from tribe_agent.modules import load_modules
from tribe_agent.modules.announcements import AnnouncementsModule
from tribe_agent.modules.classify import ClassifyModule
from tribe_agent.modules.executor import ExecutorModule
from tribe_agent.modules.notifications import NotificationsModule


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


def test_notifications_prompt_contains_events():
    module = NotificationsModule()
    prompt = module._build_prompt(
        [_task("notification", {"event_type": "UserMentioned", "title": "Hi", "link": "/x", "body": "yo"})]
    )
    assert "@notifications" in prompt
    assert "UserMentioned" in prompt
    assert "Link: /x" in prompt


def test_classify_prompt_contains_issue():
    module = ClassifyModule()
    prompt = module._build_prompt(
        [_task("classify", {"issue_id": "i1", "title": "Bug", "description": "broken", "author_id": "u1"})]
    )
    assert "@classifier" in prompt
    assert "issue_id=i1" in prompt
    assert "Bug" in prompt


def test_executor_prompt_contains_issue_and_commands():
    config = load_config(profile="community")
    config.module_settings["executor"] = {"commands": {"test": "pytest"}}
    spec = ExecutorModule().tasks(config)["execute"]
    prompt = spec.build_prompt(
        [_task("execute", {"issue_id": "i2", "title": "Fix", "description": "do it"})]
    )
    assert "@task-executor" in prompt
    assert "issue_id=i2" in prompt
    assert "test='pytest'" in prompt


def test_announcements_prompt_contains_config():
    config = load_config(profile="community")
    config.module_settings["announcements"] = {"frequency": "daily", "telegram_chat_id": "42"}
    spec = AnnouncementsModule().tasks(config)["announcement"]
    prompt = spec.build_prompt([_task("announcement", {"sha": "abcdef12", "message": "feat: x"})])
    assert "Frequency: daily" in prompt
    assert "abcdef12" in prompt
