"""codex runtime — placeholder behind the CodingRuntime ABC."""

from __future__ import annotations

from pathlib import Path

from tribe_agent.core.config import AppConfig
from tribe_agent.core.runtime.base import CodingRuntime, RunResult


class CodexRuntime(CodingRuntime):
    name = "codex"

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def run(
        self, *, title: str, prompt: str, directory: Path, agent: str
    ) -> RunResult:
        raise NotImplementedError("codex runtime is not implemented yet")
