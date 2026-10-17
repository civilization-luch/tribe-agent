"""Coding runtime abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    session_id: str | None = None
    status: str = "ok"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.status == "ok"


class CodingRuntime(ABC):
    name: str = "runtime"

    @abstractmethod
    async def run(
        self, *, title: str, prompt: str, directory: Path, agent: str
    ) -> RunResult:
        raise NotImplementedError

    async def close(self) -> None:
        """Release anything the runtime kept alive across runs."""
