"""Isolated opencode home for the sidecar: agents, config, paths.

opencode v2 resolves agents, MCP servers and providers when a server starts and
ignores the environment of the process running `opencode run`, so the agent
cannot configure a shared background service. Instead it materialises its own
config home and runs a private server against it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from tribe_agent.core.config import OPENCODE_DIR, AppConfig

logger = logging.getLogger("tribe_agent")

USER_DATA_DIR = Path.home() / ".local" / "share" / "opencode"
USER_CONFIG_FILE = Path.home() / ".config" / "opencode" / "opencode.jsonc"
DEFAULT_PORT = 47391


def home_path(config: AppConfig) -> Path:
    return config.data_path / "opencode-home"


def config_home(config: AppConfig) -> Path:
    return home_path(config) / "config"


def sidecar_data_path(config: AppConfig) -> Path:
    return home_path(config) / "data"


def sidecar_state_path(config: AppConfig) -> Path:
    return home_path(config) / "state"


def sidecar_db_path(config: AppConfig) -> Path:
    return sidecar_data_path(config) / "opencode" / "opencode.db"


def agents_path(config: AppConfig) -> Path:
    return config_home(config) / "opencode" / "agents"


def state_file(config: AppConfig) -> Path:
    return home_path(config) / "sidecar.json"


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r",\s*([}\]])", r"\1", text)


def _load_jsonc(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        logger.warning("cannot parse %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _merged_config(config: AppConfig) -> dict:
    """Sidecar config: the operator's providers, our MCP and permissions."""
    base = _load_jsonc(USER_CONFIG_FILE)
    packaged = _load_jsonc(OPENCODE_DIR / "opencode.jsonc")

    merged = dict(base)
    merged.pop("mcp", None)
    if packaged.get("mcp"):
        merged["mcp"] = packaged["mcp"]
    if packaged.get("permission"):
        merged["permission"] = packaged["permission"]
    if config.opencode.providers:
        merged["provider"] = {**base.get("provider", {}), **config.opencode.providers}
    if config.opencode.model:
        merged["model"] = config.opencode.model
    merged["$schema"] = "https://opencode.ai/config.json"
    return merged


def ensure_config_home(config: AppConfig) -> Path:
    """(Re)build the sidecar config home and return its root."""
    agents_src = OPENCODE_DIR / "agents"
    agents_dst = agents_path(config)
    agents_dst.mkdir(parents=True, exist_ok=True)

    expected = {p.name for p in agents_src.glob("*.md")} if agents_src.is_dir() else set()
    for stale in {p.name for p in agents_dst.glob("*.md")} - expected:
        (agents_dst / stale).unlink()
    for source in agents_src.glob("*.md"):
        shutil.copy2(source, agents_dst / source.name)

    config_dir = config_home(config) / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.jsonc").write_text(
        json.dumps(_merged_config(config), indent=2), encoding="utf-8"
    )
    return config_home(config)


def missing_agents(config: AppConfig) -> list[str]:
    """Packaged agents absent from the sidecar home."""
    src = {p.stem for p in (OPENCODE_DIR / "agents").glob("*.md")}
    have = {p.stem for p in agents_path(config).glob("*.md")}
    return sorted(src - have)
