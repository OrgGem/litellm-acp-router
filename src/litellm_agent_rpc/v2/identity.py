"""Host-created principals and immutable pending locators; no raw API keys."""

import hmac
import secrets
from dataclasses import dataclass

from .contract import canonical
from .errors import ProtocolError


@dataclass(frozen=True)
class Principal:
    scope_id: str
    epoch: str
    grants: frozenset[str]
    max_sessions: int = 4


class Authority:
    """Host-only registry. HTTP callers cannot construct or supply principals.

    A deployment authentication adapter must register authenticated identities;
    the legacy master-key host is a single principal. External key stores must
    refresh this registry within 15 seconds and fail closed on unavailability.
    """

    def __init__(self, environment="local", secret=None):
        self.environment = environment
        self._secret = secret or secrets.token_bytes(32)
        self._principals = {}
        self.available = True

    def register(self, scope_id, grants, *, max_sessions=4):
        principal = Principal(
            scope_id, secrets.token_hex(16), frozenset(grants), max_sessions
        )
        self._principals[scope_id] = principal
        return principal

    def revoke(self, scope_id):
        self._principals.pop(scope_id, None)

    def check(self, principal, alias):
        if not self.available:
            raise ProtocolError("auth_unavailable", 503)
        if self._principals.get(principal.scope_id) is not principal:
            raise ProtocolError("key_revoked", 401)
        if alias not in principal.grants:
            raise ProtocolError("model_not_granted", 403)

    def locator(self, principal, cid):
        return hmac.digest(
            self._secret,
            canonical([principal.scope_id, principal.epoch, self.environment, cid]),
            "sha256",
        ).hex()

    def session_scope(self, locator, fingerprint):
        return hmac.digest(
            self._secret, canonical([locator, fingerprint]), "sha256"
        ).hex()

    def scope_tag(self, principal):
        return hmac.digest(
            self._secret,
            canonical(
                [
                    "management-scope",
                    principal.scope_id,
                    principal.epoch,
                    self.environment,
                ]
            ),
            "sha256",
        ).hex()
