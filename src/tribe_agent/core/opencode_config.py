"""Runtime opencode config generation (providers, model) and default paths."""

from __future__ import annotations

import json
from pathlib import Path

from tribe_agent.core.config import AppConfig

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "opencode"


def default_db_path() -> Path:
    return DEFAULT_DATA_DIR / "opencode.db"


def runtime_config_path(config: AppConfig) -> Path:
    return config.data_path / "opencode.runtime.jsonc"


def ensure_runtime_config(config: AppConfig) -> Path | None:
    """Write a runtime opencode config with custom providers/model.

    Returns the path when there is something to override, else None.
    Values support opencode `{env:VAR}` interpolation for credentials.
    """
    override: dict[str, object] = {}
    if config.opencode.providers:
        override["provider"] = config.opencode.providers
    if config.opencode.model:
        override["model"] = config.opencode.model
    if not override:
        return None

    path = runtime_config_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"$schema": "https://opencode.ai/config.json", **override}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
