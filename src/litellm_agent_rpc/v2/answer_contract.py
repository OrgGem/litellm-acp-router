"""Frozen answer-only contract, packaged independently of the native contract."""

import hashlib
import json
import platform
from importlib.resources import files

from jsonschema import Draft202012Validator

from .contract import canonical
from .errors import ProtocolError

_resources = files(__package__)
_schema = _resources.joinpath("answer_only_schema.json").read_bytes()
_semantics = _resources.joinpath("answer_only_semantics.md").read_bytes()
CONTRACT_HASH = hashlib.sha256(_schema + b"\n" + _semantics).hexdigest()
VALIDATOR = Draft202012Validator(json.loads(_schema))


def validate(dto, value):
    canonical(value)
    if not VALIDATOR.is_valid({"dto": dto, "value": value}):
        raise ProtocolError("invalid_" + dto.lower(), 502)
    return value


def check_binding(manifest, profile, model, *, fixture=False):
    if (
        manifest["profile_id"] != profile["profile_id"]
        or manifest["profile_revision"] != profile["profile_revision"]
        or manifest["transport"] != profile["transport"]
        or manifest["model"] != model
    ):
        raise ProtocolError("capability_binding_mismatch", 503)


async def verify_live_binding(driver, profile, model, manifest, *, fixture=False):
    """The adapter must bind the receipt to the executable it will actually run."""
    if fixture:
        return
    try:
        verified = await driver.verify_live_binding(profile, model, manifest)
    except Exception:
        raise ProtocolError("answer_only_profile_unverified", 503) from None
    if verified is not True:
        raise ProtocolError("answer_only_profile_unverified", 503)
    live = any(feature["live_verified"] for feature in manifest["features"].values())
    if live and (
        manifest["platform"] != platform.system() or not manifest["executable_sha256"]
    ):
        raise ProtocolError("capability_binding_mismatch", 503)
