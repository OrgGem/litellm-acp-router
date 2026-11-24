"""Conservative UI capabilities; the adapters remain the readiness authority."""

BACKENDS = {
    "codex": {
        "name": "codex",
        "label": "Codex",
        "auth_actions": ("login", "logout", "check"),
        "gated_reason": None,
        "model_discovery": "manual",
        "session_reuse": False,
    },
    "antigravity": {
        "name": "antigravity",
        "label": "Antigravity",
        "auth_actions": (),
        "gated_reason": None,
        "model_discovery": "manual",
        "session_reuse": False,
    },
    "claude": {
        "name": "claude",
        "label": "Claude",
        "auth_actions": ("login", "logout", "check"),
        "gated_reason": None,
        "model_discovery": "manual",
        "session_reuse": True,
    },
    "kiro": {
        "name": "kiro",
        "label": "Kiro",
        "auth_actions": ("login", "logout", "check"),
        "gated_reason": "kiro_profile_isolation_not_verified",
        "model_discovery": "manual",
        "session_reuse": True,
    },
}
