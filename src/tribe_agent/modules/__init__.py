"""Module registry."""

from __future__ import annotations

import logging

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module
from tribe_agent.modules.announcements import AnnouncementsModule
from tribe_agent.modules.assistant import AssistantModule
from tribe_agent.modules.classify import ClassifyModule
from tribe_agent.modules.deployer import DeployerModule
from tribe_agent.modules.executor import ExecutorModule
from tribe_agent.modules.notifications import NotificationsModule
from tribe_agent.modules.self_heal import SelfHealModule
from tribe_agent.modules.support import SupportModule

logger = logging.getLogger("tribe_agent")

ALL_MODULES: list[type[Module]] = [
    ClassifyModule,
    NotificationsModule,
    AnnouncementsModule,
    ExecutorModule,
    SelfHealModule,
    DeployerModule,
    AssistantModule,
    SupportModule,
]


def load_modules(config: AppConfig) -> list[Module]:
    registry: dict[str, Module] = {cls.id: cls() for cls in ALL_MODULES}
    selected: list[Module] = []
    for module_id in config.enabled_modules:
        module = registry.get(module_id)
        if module is None:
            logger.warning("unknown module in profile: %s", module_id)
            continue
        selected.append(module)
    return selected
