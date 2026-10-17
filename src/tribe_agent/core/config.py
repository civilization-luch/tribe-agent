"""Configuration model and loading (YAML + env)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = PACKAGE_DIR / "profiles"
OPENCODE_DIR = PACKAGE_DIR / "opencode"

ENV_OVERRIDES = {
    "TRIBE_AGENT_BOT_TOKEN": ("community", "bot_token"),
    "TRIBE_AGENT_USER_ID": ("community", "user_id"),
    "TRIBE_AGENT_API_URL": ("community", "api_url"),
    "TRIBE_AGENT_MCP_URL": ("community", "mcp_url"),
    "TRIBE_AGENT_AUTH_URL": ("community", "auth_url"),
    "TRIBE_AGENT_JWKS_URL": ("community", "jwks_url"),
    "TRIBE_AGENT_AUTH_CLIENT_ID": ("community", "auth_client_id"),
    "TRIBE_AGENT_PROJECT_PATH": ("__root__", "project_path"),
    "TRIBE_AGENT_DATA_DIR": ("__root__", "data_dir"),
    "TRIBE_AGENT_OPENCODE_DB": ("opencode", "db"),
    "TRIBE_AGENT_OPENCODE_URL": ("opencode", "url"),
    "TRIBE_AGENT_OPENCODE_PASSWORD": ("opencode", "password"),
    "TRIBE_AGENT_MODEL": ("opencode", "model"),
}


class CommunityConfig(BaseModel):
    api_url: str = "http://localhost:8000/graphql"
    mcp_url: str = ""
    bot_token: str = ""
    user_id: str = ""
    communities: list[str] = Field(default_factory=list)
    auth_url: str = ""            # tribe-auth base URL; RS256 verified via its JWKS
    jwks_url: str = ""            # default: {auth_url}/.well-known/jwks.json
    auth_client_id: str = "tribe"  # expected `aud` of RS256 tokens


class RepoConfig(BaseModel):
    root: str = "./data/repos"
    strategy: str = "shallow"
    update: str = "lazy"
    git_token: str = ""
    refetch_interval_minutes: int = 60
    default_url: str = ""
    default_branch: str = ""


class ConcurrencyConfig(BaseModel):
    max_runs: int = 2
    max_queue: int = 200


class OpencodeConfig(BaseModel):
    config_dir: str = ""
    db: str = ""
    url: str = ""
    password: str = ""
    model: str = ""
    providers: dict[str, Any] = Field(default_factory=dict)


class AppConfig(BaseModel):
    profile: str = "community"
    data_dir: str = "./data"
    project_path: str = ""
    runtime: str = "opencode"
    community: CommunityConfig = Field(default_factory=CommunityConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    opencode: OpencodeConfig = Field(default_factory=OpencodeConfig)
    intervals: dict[str, int] = Field(default_factory=dict)
    batch_limits: dict[str, int] = Field(default_factory=dict)
    telegram: dict[str, str] = Field(default_factory=dict)
    enabled_modules: list[str] = Field(default_factory=list)
    module_settings: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    def module_config(self, module_id: str) -> dict[str, Any]:
        return self.module_settings.get(module_id, {}) or {}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"Config must be a mapping: {path}")
    return data


def load_profile(name: str) -> dict[str, Any]:
    return _load_yaml(PROFILES_DIR / f"{name}.yaml")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env(data: dict[str, Any]) -> None:
    for env_key, (section, field) in ENV_OVERRIDES.items():
        value = os.environ.get(env_key)
        if not value:
            continue
        if section == "__root__":
            data[field] = value
        else:
            data.setdefault(section, {})[field] = value


def load_config(path: str | Path | None = None, profile: str | None = None) -> AppConfig:
    user_data = _load_yaml(Path(path).expanduser().resolve()) if path else {}
    profile_name = profile or user_data.get("profile") or "community"

    profile_data = dict(load_profile(profile_name))
    enabled = list(profile_data.pop("modules", []) or [])

    user_data = dict(user_data)
    user_modules = user_data.pop("modules", None)
    if isinstance(user_modules, list):
        enabled = list(user_modules)
    elif isinstance(user_modules, dict):
        settings = _deep_merge(user_data.get("module_settings", {}), user_modules)
        user_data["module_settings"] = settings

    merged = _deep_merge(profile_data, user_data)
    merged["profile"] = profile_name
    _apply_env(merged)

    config = AppConfig(**merged)
    config.enabled_modules = enabled
    if not config.opencode.config_dir:
        config.opencode.config_dir = str(OPENCODE_DIR)
    return config
