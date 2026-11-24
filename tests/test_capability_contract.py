import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/agent-rpc/v0.1"


def test_contract_manifest_and_structural_fixtures():
    manifest = json.loads((CONTRACT / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((CONTRACT / name).read_bytes()).hexdigest() == digest
    canonical = (
        json.dumps(manifest["files"], sort_keys=True, indent=2) + "\n"
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == manifest["contract_hash"]
    schema = json.loads((CONTRACT / "schema.json").read_text())
    packaged = ROOT / "src/litellm_agent_rpc/v2/schema.json"
    assert packaged.read_bytes() == (CONTRACT / "schema.json").read_bytes()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    cases = json.loads((CONTRACT / "fixtures.json").read_text())
    assert len({case["name"] for case in cases}) == len(cases)
    for case in cases:
        assert validator.is_valid(case["document"]) == case["valid"], case["name"]
