import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_unified import FakeProvider

from litellm_agent_rpc import cli, server
from litellm_agent_rpc.backends import Handle
from litellm_agent_rpc.provider import AgentRPC


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "enabled": {"codex": True},
                "keys": {"router": "sk-example-fixture-12345678901234567890"},
            }
        )
    )
    return path


@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_cli_defaults_to_loopback_and_allows_container_bind(
    config_path, monkeypatch, host
):
    calls = []
    monkeypatch.setattr(server, "run", lambda *args, **kwargs: calls.append(kwargs))
    args = ["agent-rpc", "serve", "--config", str(config_path)]
    if host is not None:
        args += ["--host", host]
    monkeypatch.setattr(sys, "argv", args)
    assert cli.main() == 0
    assert calls == [{"host": host or "127.0.0.1"}]


def test_litellm_sdk_example_dispatches_and_closes(config_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[1] / "example/litellm_sdk.py"
    spec = importlib.util.spec_from_file_location("sdk_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = FakeProvider()
    monkeypatch.setattr(
        module,
        "AgentRPC",
        lambda config: AgentRPC(
            config, lambda name, cfg: Handle(backend, backend.close)
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["example", "--config", str(config_path), "--prompt", "fixture"]
    )
    module.main()
    assert "codex-default" in capsys.readouterr().out
    assert backend.calls[0][0] == "codex-default"
    assert backend.closed
