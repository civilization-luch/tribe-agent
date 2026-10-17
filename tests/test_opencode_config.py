from __future__ import annotations

import json
from pathlib import Path

from tribe_agent.core.config import load_config
from tribe_agent.core.opencode_config import (
    agents_path,
    config_home,
    ensure_config_home,
    missing_agents,
    sidecar_db_path,
    sidecar_state_path,
)


def _config(tmp_path: Path):
    config = load_config()
    config.data_dir = str(tmp_path)
    return config


def test_config_home_holds_every_packaged_agent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    home = ensure_config_home(config)
    assert home == config_home(config)
    assert agents_path(config).is_dir()
    assert missing_agents(config) == []


def test_stale_agents_are_removed_on_rebuild(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ensure_config_home(config)
    stale = agents_path(config) / "tribe-gone.md"
    stale.write_text("---\ndescription: x\n---\nbody\n", encoding="utf-8")
    ensure_config_home(config)
    assert not stale.exists()


def test_config_carries_mcp_permissions_and_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.opencode.model = "myrouter/some-model"
    config.opencode.providers = {
        "myrouter": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "MyRouter",
            "options": {"baseURL": "https://api.example.com/v1", "apiKey": "{env:MYROUTER_API_KEY}"},
            "models": {"some/model": {"name": "some-model"}},
        }
    }
    ensure_config_home(config)
    payload = json.loads((config_home(config) / "opencode" / "opencode.jsonc").read_text())
    assert payload["mcp"]["community"]["type"] == "remote"
    assert payload["permission"]["bash"] == "allow"
    assert payload["model"] == "myrouter/some-model"
    assert payload["provider"]["myrouter"]["options"]["apiKey"] == "{env:MYROUTER_API_KEY}"


def test_sidecar_paths_stay_inside_the_data_dir(tmp_path: Path) -> None:
    config = _config(tmp_path)
    home = tmp_path / "opencode-home"
    assert sidecar_db_path(config) == home / "data" / "opencode" / "opencode.db"
    assert sidecar_state_path(config) == home / "state"
