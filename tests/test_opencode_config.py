from __future__ import annotations

import json
from pathlib import Path

from tribe_agent.core.config import load_config
from tribe_agent.core.opencode_config import (
    default_db_path,
    ensure_runtime_config,
    runtime_config_path,
)


def test_no_runtime_config_without_overrides(tmp_path: Path):
    config = load_config()
    config.data_dir = str(tmp_path)
    assert ensure_runtime_config(config) is None


def test_runtime_config_with_providers(tmp_path: Path):
    config = load_config()
    config.data_dir = str(tmp_path)
    config.opencode.providers = {
        "myrouter": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "MyRouter",
            "options": {"baseURL": "https://api.example.com/v1", "apiKey": "{env:MYROUTER_API_KEY}"},
            "models": {"some/model": {"name": "some-model"}},
        }
    }
    path = ensure_runtime_config(config)
    assert path == runtime_config_path(config)
    payload = json.loads(path.read_text())
    assert payload["provider"]["myrouter"]["options"]["apiKey"] == "{env:MYROUTER_API_KEY}"


def test_runtime_config_with_model(tmp_path: Path):
    config = load_config()
    config.data_dir = str(tmp_path)
    config.opencode.model = "myrouter/some-model"
    path = ensure_runtime_config(config)
    assert path is not None
    assert json.loads(path.read_text())["model"] == "myrouter/some-model"


def test_default_db_path():
    assert str(default_db_path()).endswith(".local/share/opencode/opencode.db")
