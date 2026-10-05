"""Module contract and execution primitives."""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tribe_agent.core.client.graphql import GraphQLClient
from tribe_agent.core.config import AppConfig
from tribe_agent.core.queue import Task, TaskQueue
from tribe_agent.core.repo import RepoManager


@dataclass
class ScanContext:
    config: AppConfig
    client: GraphQLClient
    queue: TaskQueue
    repo: RepoManager
    logger: logging.Logger


@dataclass
class Scanner:
    name: str
    interval: int
    run: Callable[[ScanContext], Awaitable[int]]


@dataclass
class TaskSpec:
    task_type: str
    batch_limit: int
    agent: str
    build_prompt: Callable[[list[Task]], str]
    on_success: Callable[[list[Task], ScanContext], Awaitable[None]] | None = None


class Module(ABC):
    id: str = ""

    def scanners(self, config: AppConfig) -> list[Scanner]:
        return []

    def tasks(self, config: AppConfig) -> dict[str, TaskSpec]:
        return {}

    def service_app(self, config: AppConfig) -> Any | None:
        return None
