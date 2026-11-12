import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workspace  # noqa: E402


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / ".local/config.json"
    monkeypatch.setattr(workspace, "LOCAL", path)
    workspace.initialize()
    return workspace.read_config(path)


def test_init_preserves_keys_and_uses_distinct_secrets(config):
    before = workspace.LOCAL.read_bytes()
    workspace.initialize()
    assert workspace.LOCAL.read_bytes() == before
    assert len(set(config["keys"].values())) == 5


@pytest.mark.parametrize("fault", ["ports", "keys", "alias"])
def test_invalid_deployment_config_rejected(config, fault):
    if fault == "ports":
        config["ports"]["codex"] = config["ports"]["router"]
    elif fault == "keys":
        config["keys"]["codex"] = config["keys"]["router"]
    else:
        config["antigravity_models"] = {"wrong": {"model": "fixture"}}
    workspace.LOCAL.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        workspace.read_config(workspace.LOCAL)


def test_service_auth_and_loopback_routes(config):
    router = workspace.environment(config, "router")
    codex = workspace.environment(config, "codex")
    agy = workspace.environment(config, "antigravity")
    assert router["CODEX_API_KEY"] == codex["LITELLM_MASTER_KEY"]
    assert router["ANTIGRAVITY_RPC_API_KEY"] == agy["ANTIGRAVITY_RPC_API_KEY"]
    assert router["LITELLM_MASTER_KEY"] != codex["LITELLM_MASTER_KEY"]
    assert router["CODEX_API_BASE"] == "http://127.0.0.1:4101/v1"
    assert "127.0.0.1" in router["NO_PROXY"]
    assert codex["CODEX_RPC_ALLOW_PROMPT_FLATTENING"] == "false"


def test_commands_do_not_contain_keys(config):
    for service in workspace.PROJECTS:
        command = workspace.command(config, service)
        assert not any(key in " ".join(command) for key in config["keys"].values())
        assert str(config["ports"][service]) in command


def test_v1_migration_preserves_values_and_is_idempotent(tmp_path):
    path = tmp_path / "config.json"
    original = {
        "ports": {"router": 4000, "codex": 4103, "antigravity": 4104},
        "keys": {
            s: "sk-original-secret-123456789-" + s
            for s in ("router", "codex", "antigravity")
        },
        "executables": {"codex": "my-codex", "antigravity": "my-agy"},
        "codex_model": "custom",
        "antigravity_models": {"antigravity": {"model": "custom"}},
    }
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    migrated = workspace.read_config(path)
    for key in ("ports", "keys", "executables"):
        assert all(migrated[key][s] == value for s, value in original[key].items())
    assert path.with_suffix(".json.v1.bak").read_bytes() == before
    after = path.read_bytes()
    workspace.read_config(path)
    assert path.read_bytes() == after
    assert workspace.enabled_services(migrated) == ["router", "codex", "antigravity"]


def test_optional_services_do_not_require_cli(config):
    assert workspace.enabled_services(config) == ["router", "codex", "antigravity"]
    config["enabled"] = {s: s == "claude" for s in workspace.ALIASES}
    assert workspace.enabled_services(config) == ["router", "claude"]
    with pytest.raises(ValueError, match="disabled"):
        workspace.serve(config, "codex")


def test_agent_operational_limits_are_forwarded_and_validated(config):
    config["claude_limits"] = {"concurrency": 1, "timeout": 240, "idle_ttl": 60}
    assert workspace.validate_config(config) is config
    env = workspace.environment(config, "claude")
    assert json.loads(env["CLAUDE_RPC_LIMITS"]) == config["claude_limits"]
    config["claude_limits"]["concurrency"] = 0
    with pytest.raises(ValueError):
        workspace.validate_config(config)
