"""Configuration and route registry. Loading never rewrites credentials."""

import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

ALIASES = {
    "codex": "codex-default",
    "antigravity": "antigravity",
    "claude": "claude-default",
    "kiro": "kiro-default",
}
_ALIAS = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_UNSET = object()
_V2_TRANSPORTS = {
    "codex": {"stdio", "unix", "websocket"},
    "antigravity": {"stdio-acp"},
    "claude": {"stdio"},
    "kiro": {"stdio"},
}
_V2_LIMITS = {
    "max_active": (int, 1, 64),
    "max_sessions": (int, 1, 256),
    "native_seconds": ((int, float), 0.01, 86400),
    "chain_seconds": ((int, float), 0.01, 86400),
    "tool_wait_seconds": ((int, float), 0.01, 86400),
    "cleanup_seconds": ((int, float), 0.01, 60),
    "max_handoffs": (int, 1, 128),
}


def route_specs(config):
    """Return a normalized snapshot, including disabled routes.

    Legacy configuration is interpreted in memory. A routes object, when present,
    is authoritative, so removing an entry cannot resurrect its legacy alias.
    """
    if "routes" not in config:
        agy_models = config.get("antigravity_models", {})
        if not isinstance(agy_models, dict) or any(
            not isinstance(config.get(name + "_models", {}), dict)
            for name in ("claude", "kiro")
        ):
            raise ValueError("Invalid legacy model mapping")
        agy = agy_models.get("antigravity", {})
        if not isinstance(agy, dict):
            raise ValueError("Invalid legacy model mapping")
        specs = {
            ALIASES["codex"]: {"backend": "codex", "model": config.get("codex_model")},
            ALIASES["antigravity"]: {
                "backend": "antigravity",
                "model": agy.get("model"),
                "settings": {"thought_level": agy.get("thought_level")},
            },
            **{
                ALIASES[name]: {
                    "backend": name,
                    "model": config.get(name + "_models", {}).get(ALIASES[name]),
                }
                for name in ("claude", "kiro")
            },
        }
        legacy = True
    else:
        specs = config["routes"]
        legacy = False
    if not isinstance(specs, dict):
        raise ValueError("Invalid routes")
    result = {}
    for alias, spec in specs.items():
        if (
            not isinstance(alias, str)
            or not _ALIAS.fullmatch(alias)
            or not isinstance(spec, dict)
            or set(spec) - {"backend", "model", "settings", "enabled"}
        ):
            raise ValueError("Invalid route")
        backend = spec.get("backend")
        model = spec.get("model")
        settings = spec.get("settings", {})
        enabled = spec.get("enabled", True)
        if (
            not isinstance(backend, str)
            or backend not in ALIASES
            or type(enabled) is not bool
        ):
            raise ValueError("Invalid route backend or enabled state")
        if model is not None and (
            not isinstance(model, str) or not model.strip() or len(model) > 200
        ):
            raise ValueError("Invalid route model")
        backend_enabled = config.get("enabled", {"codex": True, "antigravity": True})
        if (
            backend == "antigravity"
            and model is None
            and not legacy
            and enabled
            and backend_enabled.get(backend, False)
        ):
            raise ValueError("Antigravity requires an explicit model")
        allowed_settings = (
            {"thought_level", "flatten"}
            if backend == "antigravity"
            else {"flatten"}
            if backend in ("codex", "claude")
            else set()
        )
        if not isinstance(settings, dict) or set(settings) - allowed_settings:
            raise ValueError("Unsupported route settings")
        if "flatten" in settings and type(settings["flatten"]) is not bool:
            raise ValueError("Invalid flatten setting")
        thought = settings.get("thought_level")
        if thought is not None and (
            not isinstance(thought, str) or not thought.strip() or len(thought) > 200
        ):
            raise ValueError("Invalid thought level")
        result[alias] = {
            "backend": backend,
            "model": model,
            "settings": dict(settings),
            "enabled": enabled,
        }
    default = config.get("default_route")
    if default is not None and (
        not isinstance(default, str)
        or default not in result
        or not result[default]["enabled"]
        or not config.get("enabled", {"codex": True, "antigravity": True}).get(
            result[default]["backend"], False
        )
    ):
        raise ValueError("Invalid default route")
    return result


def routes(config):
    enabled = config.get("enabled", {"codex": True, "antigravity": True})
    return {
        alias: spec["backend"]
        for alias, spec in route_specs(config).items()
        if spec["enabled"] and enabled.get(spec["backend"], False)
    }


def backend_models(config, backend):
    selected = routes(config)
    specs = route_specs(config)
    return {
        alias: (
            {
                "model": specs[alias]["model"],
                **{
                    k: v
                    for k, v in specs[alias]["settings"].items()
                    if k == "thought_level"
                },
            }
            if backend == "antigravity"
            else specs[alias]["model"]
        )
        for alias, name in selected.items()
        if name == backend
    }


def route_flatten(config, alias):
    spec = route_specs(config)[alias]
    if spec["backend"] == "kiro":
        return False
    if "flatten" in spec["settings"]:
        return spec["settings"]["flatten"]
    return config.get("allow_prompt_flattening") is True


def flatten_routes(config):
    return {alias: route_flatten(config, alias) for alias in routes(config)}


def backend_flatten_map(config, backend):
    return {
        alias: route_flatten(config, alias)
        for alias, name in routes(config).items()
        if name == backend
    }


def backend_flatten(config, backend):
    if backend == "kiro":
        return False
    per_alias = backend_flatten_map(config, backend)
    if per_alias:
        return any(per_alias.values())
    return config.get("allow_prompt_flattening") is True


def v2_config(config):
    """Validate opt-in route profiles without treating configuration as evidence."""
    options = config.get("v2", {})
    if not isinstance(options, dict) or set(options) - {
        "enabled",
        "routes",
        "limits",
        "auth_mode",
    }:
        raise ValueError("Invalid v2 configuration")
    enabled, profiles, limits = (
        options.get("enabled", False),
        options.get("routes", {}),
        options.get("limits", {}),
    )
    auth_mode = options.get("auth_mode", "owner")
    if (
        type(enabled) is not bool
        or not isinstance(profiles, dict)
        or not isinstance(limits, dict)
    ):
        raise ValueError("Invalid v2 configuration")
    if auth_mode not in ("owner", "virtual_keys"):
        raise ValueError("Invalid v2 authentication mode")
    if set(limits) - set(_V2_LIMITS):
        raise ValueError("Invalid v2 limits")
    for name, value in limits.items():
        expected, minimum, maximum = _V2_LIMITS[name]
        permitted = (expected,) if expected is int else expected
        if (
            type(value) not in permitted
            or not minimum <= value <= maximum
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid v2 limit")
    specs = route_specs(config)
    active = routes(config)
    result = {}
    for alias, profile in profiles.items():
        if (
            alias not in specs
            or not isinstance(profile, dict)
            or not {"profile_id", "profile_revision", "transport"} <= set(profile)
            or set(profile)
            - {"profile_id", "profile_revision", "transport", "execution_mode"}
            or (enabled and alias not in active)
        ):
            raise ValueError("Invalid v2 route profile")
        for field in ("profile_id", "profile_revision"):
            value = profile[field]
            if not isinstance(value, str) or not _PROFILE.fullmatch(value):
                raise ValueError("Invalid v2 route profile")
        if (
            not isinstance(profile["transport"], str)
            or profile["transport"] not in _V2_TRANSPORTS[specs[alias]["backend"]]
        ):
            raise ValueError("Invalid v2 route transport")
        mode = profile.get("execution_mode", "native")
        if mode not in ("native", "answer_only"):
            raise ValueError("Invalid v2 execution mode")
        if mode == "answer_only" and profile["transport"] != (
            "stdio-acp" if specs[alias]["backend"] == "antigravity" else "stdio"
        ):
            raise ValueError("Answer-only requires owned stdio transport")
        result[alias] = dict(profile)
    if enabled and not result:
        raise ValueError("V2 requires at least one enabled route")
    if auth_mode == "virtual_keys" and (not enabled or set(active) != set(result)):
        raise ValueError(
            "Virtual key authentication requires all active routes to use v2"
        )
    return {
        "enabled": enabled,
        "routes": result,
        "limits": dict(limits),
        "auth_mode": auth_mode,
    }


def v2_route_specs(config):
    options = v2_config(config)
    return options["routes"] if options["enabled"] else {}


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Invalid configuration")
    executables = config.get("executables", {})
    if not isinstance(executables, dict) or any(
        key not in ALIASES or not isinstance(value, str) or not value.strip()
        for key, value in executables.items()
    ):
        raise ValueError("Invalid executable configuration")
    enabled = config.get("enabled", {"codex": True, "antigravity": True})
    if (
        not isinstance(enabled, dict)
        or set(enabled) - set(ALIASES)
        or any(type(v) is not bool for v in enabled.values())
    ):
        raise ValueError("Invalid backend selection")
    keys = config.get("keys", {})
    key = keys.get("router", "") if isinstance(keys, dict) else ""
    if (
        not isinstance(key, str)
        or len(key) < 24
        or not key.isascii()
        or any(c.isspace() for c in key)
    ):
        raise ValueError("Invalid gateway key")
    ports = config.get("ports", {})
    port = ports.get("router", 4000) if isinstance(ports, dict) else None
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid gateway port")
    timeout = config.get("router_timeout", 350)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invalid gateway timeout")
    if (
        "allow_prompt_flattening" in config
        and type(config["allow_prompt_flattening"]) is not bool
    ):
        raise ValueError("Invalid prompt flattening setting")
    route_specs(config)
    v2_config(config)
    return config


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(config)
    config.setdefault("enabled", {"codex": True, "antigravity": True})
    return config


def revision(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@contextmanager
def _config_lock(path):
    """An OS-backed lock is released even if the editor process crashes."""
    with path.open("a+b") as file:
        if os.name == "nt":
            import msvcrt

            file.seek(0, os.SEEK_END)
            if not file.tell():
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Configuration is being edited") from None
            try:
                yield
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Configuration is being edited") from None
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def save_config(path, config, expected_revision=_UNSET):
    """Validate and atomically replace a complete config, with a cooperative lock."""
    validate_config(config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock(path.with_name(path.name + ".lock")):
        if expected_revision is not _UNSET and revision(path) != expected_revision:
            raise ValueError("Configuration changed since it was loaded")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".config-",
                delete=False,
            ) as file:
                temp = Path(file.name)
                json.dump(config, file, ensure_ascii=False, indent=2, allow_nan=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if expected_revision is not _UNSET and revision(path) != expected_revision:
                raise ValueError("Configuration changed since it was loaded")
            if path.exists():
                os.chmod(temp, path.stat().st_mode & 0o777)
            os.replace(temp, path)
            return revision(path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
