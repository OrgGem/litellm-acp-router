"""Strict wire validation against the reviewed contract, without remote refs."""

import hashlib
import json
from importlib.resources import files

from jsonschema import Draft202012Validator

from .errors import ProtocolError

CONTRACT_HASH = "749182d456eb363be2d63b2a98d66d0f1bf203b93d8a16ed4a8bdd90834e5fc1"
SCHEMA = json.loads(files(__package__).joinpath("schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def canonical(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_json") from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate(dto, value):
    # JSON serialization rejects nonfinite values and arbitrary native objects.
    canonical(value)
    if not VALIDATOR.is_valid({"dto": dto, "value": value}):
        raise ProtocolError("invalid_" + dto.lower())
    return value


def parse(data, limit=4 * 1024 * 1024):
    if len(data) > limit:
        raise ProtocolError("request_too_large", 413)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid(_):
        raise ValueError()

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_json") from None
