import hashlib
import importlib
import importlib.resources
import json
import subprocess
import sys
from pathlib import Path

import pytest

from litellm_agent_rpc.cli import main
from litellm_agent_rpc.v2.answer_contract import (
    CONTRACT_HASH as ANSWER_ONLY_CONTRACT_HASH,
)
from litellm_agent_rpc.v2.answer_contract import validate as validate_answer_only
from litellm_agent_rpc.v2.contract import CONTRACT_HASH, validate
from litellm_agent_rpc.v2.errors import ProtocolError
from litellm_agent_rpc.v2.registry import (
    DRIVERS,
    create_driver,
    inspect_binding,
    production_bindings,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not all((ROOT / f"litellm-{backend}-rpc/src").is_dir() for backend in DRIVERS),
    reason="Root-only CI checkout lacks independent adapter sources; bundled validation requires populated workspace",
)


@pytest.mark.parametrize("backend", DRIVERS)
async def test_bundled_driver_matches_contract_and_cannot_be_activated(backend):
    profile = {
        "profile_id": "inspection",
        "profile_revision": "1",
        "transport": "stdio",
        "models": {"test": "fixture"},
    }
    manifest = await inspect_binding(backend, profile, "fixture")
    validate("CapabilityManifest", manifest)
    assert manifest["contract_hash"] == CONTRACT_HASH
    assert manifest["backend"] == backend
    assert all(
        not feature["live_verified"] for feature in manifest["features"].values()
    )
    with pytest.raises(ProtocolError) as caught:
        await production_bindings(
            {"test": {"backend": backend, "profile": profile, "model": "fixture"}}
        )
    assert caught.value.status == 503


@pytest.mark.parametrize("backend", DRIVERS)
def test_independent_contract_snapshot_matches_root(backend):
    module_name, _ = DRIVERS[backend]
    package = importlib.import_module(module_name.rpartition(".")[0])
    root = importlib.resources.files(package)
    candidates = [
        root.joinpath("contract_v0_1"),
        root.joinpath("contracts", "v0.1"),
        root.joinpath("contracts", "agent-rpc", "v0.1"),
    ]
    selected = next(
        (
            directory
            for directory in candidates
            if directory.joinpath("manifest.json").is_file()
        ),
        None,
    )
    assert selected is not None, backend
    manifest = json.loads(selected.joinpath("manifest.json").read_text("utf-8"))
    assert manifest["contract_hash"] == CONTRACT_HASH
    for name, value in manifest["files"].items():
        assert hashlib.sha256(selected.joinpath(name).read_bytes()).hexdigest() == value
        assert (
            selected.joinpath(name).read_bytes()
            == (ROOT / "contracts/agent-rpc/v0.1" / name).read_bytes()
        )


@pytest.mark.parametrize("backend", DRIVERS)
async def test_answer_only_driver_is_bound_and_blocked_without_starting_cli(backend):
    profile = {
        "profile_id": "answer-inspection",
        "profile_revision": "1",
        "transport": "stdio-acp" if backend == "antigravity" else "stdio",
        "execution_mode": "answer_only",
    }
    driver = create_driver(backend, "answer_only")
    module_name = "litellm_" + backend + "_rpc.answer_only"
    adapter = importlib.import_module(module_name)
    assert adapter.ACCEPTED_RECEIPTS == ()
    manifest = await driver.capabilities(profile, "fixture")
    validate_answer_only("CapabilityManifest", manifest)
    assert manifest["contract_hash"] == ANSWER_ONLY_CONTRACT_HASH
    assert manifest["profile_id"] == profile["profile_id"]
    assert manifest["profile_revision"] == profile["profile_revision"]
    assert all(not item["live_verified"] for item in manifest["features"].values())

    with pytest.raises(Exception) as caught:
        await driver.start({}, {"config": profile, "model": "fixture", "prompt": "x"})
    assert caught.value.status == 503


@pytest.mark.parametrize("backend", DRIVERS)
@pytest.mark.parametrize(
    "tamper", ["profile", "model", "platform", "manifest", "binary"]
)
async def test_live_receipt_binding_rejects_drift_without_spawning(
    backend, tamper, tmp_path, monkeypatch
):
    module = importlib.import_module("litellm_" + backend + "_rpc.answer_only")
    profile = {
        "profile_id": "synthetic-receipt-profile",
        "profile_revision": "7",
        "transport": "stdio-acp" if backend == "antigravity" else "stdio",
    }
    model = "synthetic-receipt-model"
    executable = tmp_path / "fake-cli.bin"
    executable.write_bytes(b"synthetic inert executable fixture")
    expected_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    receipt = {
        "backend": backend,
        "profile_id": profile["profile_id"],
        "profile_revision": profile["profile_revision"],
        "transport": profile["transport"],
        "model": model,
        "platform": module.platform.system(),
        "executable_sha256": expected_digest,
        "cli_version": "fixture-version-1",
        "sdk_version": None,
        "features": ["text", "profile_isolation"],
        "tool_choices": [],
        "evidence": "synthetic test receipt only",
    }
    monkeypatch.setattr(module, "ACCEPTED_RECEIPTS", (receipt,))
    monkeypatch.setattr(
        module, "_resolve_executable", lambda selected_profile: str(executable)
    )
    driver = create_driver(backend, "answer_only")
    manifest = await driver.capabilities(profile, model)
    assert await driver.verify_live_binding(profile, model, manifest) is True

    selected_profile = dict(profile)
    selected_model = model
    selected_manifest = json.loads(json.dumps(manifest))
    if tamper == "profile":
        selected_profile["profile_revision"] = "8"
    elif tamper == "model":
        selected_model = "different-model"
    elif tamper == "platform":
        monkeypatch.setattr(module.platform, "system", lambda: "DifferentPlatform")
    elif tamper == "manifest":
        selected_manifest["cli_version"] = "different-version"
    else:
        executable.write_bytes(b"changed inert fixture")

    with pytest.raises(Exception) as caught:
        await driver.verify_live_binding(
            selected_profile, selected_model, selected_manifest
        )
    assert caught.value.status == 503


@pytest.mark.parametrize("backend", DRIVERS)
def test_answer_only_contract_snapshot_matches_root(backend):
    module_name = "litellm_" + backend + "_rpc.answer_only"
    package = importlib.import_module(module_name)
    contract = importlib.resources.files(package).joinpath("answer_only_contract_v0_1")
    manifest = json.loads(contract.joinpath("manifest.json").read_text("utf-8"))
    assert manifest["contract_hash"] == ANSWER_ONLY_CONTRACT_HASH
    for name, digest in manifest["files"].items():
        assert (
            hashlib.sha256(contract.joinpath(name).read_bytes()).hexdigest() == digest
        )
        assert (
            contract.joinpath(name).read_bytes()
            == (ROOT / "contracts/agent-rpc-answer-only/v0.1" / name).read_bytes()
        )


def test_capabilities_cli_is_read_only_and_does_not_spawn(
    tmp_path, monkeypatch, capsys
):
    config = {
        "keys": {"router": "synthetic-key-12345678901234567890"},
        "enabled": dict.fromkeys(DRIVERS, True),
        "routes": {
            backend: {"backend": backend, "model": "fixture"} for backend in DRIVERS
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(
        sys, "argv", ["agent-rpc", "capabilities", "--config", str(path)]
    )

    class NoSpawn:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Capability inspection must not start any process")

        def __class_getitem__(cls, item):
            # The pinned MCP SDK annotates subprocess.Popen[bytes] at import.
            return cls

    monkeypatch.setattr(subprocess, "Popen", NoSpawn)
    assert main() == 0
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert {entry["manifest"]["backend"] for entry in records} == set(DRIVERS)
    assert all(entry["manifest"]["contract_hash"] == CONTRACT_HASH for entry in records)
    assert config["keys"]["router"] not in output and path.read_bytes() == before
