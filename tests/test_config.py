from __future__ import annotations

from pathlib import Path

from tribe_agent.core.config import load_config, load_profile


def test_community_profile_defaults():
    config = load_config()
    assert config.profile == "community"
    assert config.enabled_modules == [
        "notifications",
        "announcements",
        "executor",
        "self_heal",
        "support",
    ]
    assert config.opencode.config_dir.endswith("tribe_agent/opencode")


def test_platform_profile_modules():
    config = load_config(profile="platform")
    assert "classify" in config.enabled_modules
    assert "assistant" in config.enabled_modules
    assert config.batch_limits["classify"] == 30


def test_profiles_files_exist():
    for name in ("community", "platform"):
        data = load_profile(name)
        assert isinstance(data.get("modules"), list)


def test_config_module_settings_merge(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
profile: community
module_settings:
  executor:
    commands:
      test: "make test"
""",
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.module_config("executor")["commands"]["test"] == "make test"


def test_config_modules_list_override(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
profile: platform
modules: [classify, notifications, executor]
""",
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.enabled_modules == ["classify", "notifications", "executor"]
    assert "announcements" not in config.enabled_modules
    assert "assistant" not in config.enabled_modules


def test_config_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TRIBE_AGENT_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TRIBE_AGENT_PROJECT_PATH", "/tmp/project")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profile: community\n", encoding="utf-8")
    config = load_config(cfg)
    assert config.community.bot_token == "secret-token"
    assert config.project_path == "/tmp/project"
