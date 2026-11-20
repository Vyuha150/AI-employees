"""Authentication: Supabase JWT verification and server-side identity resolution.

Trust model
- The JWT proves only WHO the user is (`sub`). It is verified with an explicit algorithm
  allow-list, issuer, audience, expiry and not-before.
- Organization membership, roles and permissions are loaded SERVER-SIDE from the membership
  directory (PostgreSQL: organization_members / member_roles / roles / permissions). Claims such as
  app_metadata.roles, user_metadata, or `role` are ignored for authorization.
- A client may *select* one of its organizations (X-Organization header) when it belongs to
  several; the selection is accepted only if the server finds that membership.

Supabase signs access tokens either with the legacy JWT secret (HS256) or with asymmetric signing
keys (ES256/RS256) published at <project>/auth/v1/.well-known/jwks.json. Both are supported.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .common import ActorType, Principal
from .errors import CommandCenterError, ConfigurationError


class AuthenticationError(CommandCenterError):
    """401: missing, malformed, expired or untrusted credentials."""


class MembershipError(CommandCenterError):
    """403: the authenticated user has no (selected) membership in a served organization."""


class OrganizationSelectionRequired(CommandCenterError):
    """422: the user belongs to several organizations and did not select one."""


HMAC_ALGS = ("HS256",)
ASYM_ALGS = ("ES256", "RS256")


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    audience: str = "authenticated"
    hs256_secret: str | None = field(default=None, repr=False)
    jwks: Mapping[str, Any] | None = None          # static JWKS (tests, pinned keys)
    jwks_url: str | None = None                     # fetched and cached by PyJWKClient
    leeway_seconds: int = 30
    required_role_claim: str | None = "authenticated"

    def __post_init__(self) -> None:
        if not self.issuer:
            raise ConfigurationError("auth: issuer is required")
        if not (self.hs256_secret or self.jwks or self.jwks_url):
            raise ConfigurationError("auth: configure an HS256 secret or JWKS (no unsigned tokens are accepted)")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuthConfig":
        env = os.environ if env is None else env
        base = (env.get("CC_SUPABASE_URL") or "").rstrip("/")
        if not base:
            raise ConfigurationError("CC_SUPABASE_URL is not set")
        return cls(issuer=f"{base}/auth/v1", audience=env.get("CC_JWT_AUDIENCE", "authenticated"),
                   hs256_secret=env.get("CC_SUPABASE_JWT_SECRET") or None,
                   jwks_url=env.get("CC_SUPABASE_JWKS_URL") or f"{base}/auth/v1/.well-known/jwks.json")


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str | None
    session_id: str | None
    issued_at: int | None
    expires_at: int


class TokenVerifier:
    def __init__(self, config: AuthConfig):
        import jwt  # PyJWT (optional extra "api")
        self._jwt = jwt
        self.config = config
        self._jwks_client = None
        self._lock = threading.Lock()
        if config.jwks_url and not config.jwks:
            self._jwks_client = jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=300)

    def __repr__(self) -> str:
        return f"TokenVerifier(issuer={self.config.issuer!r})"

    def _key_for(self, token: str, alg: str) -> Any:
        jwt = self._jwt
        if alg in HMAC_ALGS:
            if not self.config.hs256_secret:
                raise AuthenticationError("HS256 tokens are not accepted by this deployment")
            return self.config.hs256_secret
        if alg not in ASYM_ALGS:
            raise AuthenticationError("token algorithm not allowed")
        kid = jwt.get_unverified_header(token).get("kid")
        if self.config.jwks:
            for k in self.config.jwks.get("keys", []):
                if k.get("kid") == kid:
                    return jwt.PyJWK(k).key
            raise AuthenticationError("signing key not found")
        if self._jwks_client is None:
            raise AuthenticationError("asymmetric tokens are not accepted by this deployment")
        with self._lock:
            try:
                return self._jwks_client.get_signing_key_from_jwt(token).key
            except Exception:
                raise AuthenticationError("signing key could not be resolved") from None

    def verify(self, token: str) -> Identity:
        jwt = self._jwt
        if not token or token.count(".") != 2:
            raise AuthenticationError("malformed token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthenticationError("malformed token") from None
        alg = header.get("alg")
        if alg not in HMAC_ALGS + ASYM_ALGS:                 # rejects "none" and anything unexpected
            raise AuthenticationError("token algorithm not allowed")
        key = self._key_for(token, alg)
        try:
            claims = jwt.decode(token, key, algorithms=[alg], audience=self.config.audience,
                                issuer=self.config.issuer, leeway=self.config.leeway_seconds,
                                options={"require": ["exp", "sub", "iss", "aud"]})
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("token expired") from None
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"invalid token ({type(exc).__name__})") from None
        if self.config.required_role_claim and claims.get("role") != self.config.required_role_claim:
            raise AuthenticationError("token is not an authenticated user session")
        try:
            sub = str(uuid.UUID(str(claims["sub"])))
        except ValueError:
            raise AuthenticationError("token subject is not a user id") from None
        return Identity(sub, claims.get("email"), claims.get("session_id"), claims.get("iat"), int(claims["exp"]))


# ---------------------------------------------------------------------------------------
# memberships (server-side source of truth)
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Membership:
    organization_id: str                 # organization slug (domain id)
    user_id: str
    roles: frozenset[str]
    permissions: frozenset[str]
    display_name: str | None = None


class MembershipDirectory(Protocol):
    def memberships(self, user_id: str) -> list[Membership]: ...


@dataclass
class InMemoryMembershipDirectory:
    rows: dict[str, list[Membership]] = field(default_factory=dict)

    def add(self, m: Membership) -> Membership:
        self.rows.setdefault(m.user_id, []).append(m)
        return m

    def memberships(self, user_id: str) -> list[Membership]:
        return list(self.rows.get(user_id, []))


def resolve_principal(identity: Identity, directory: MembershipDirectory, selected_org: str | None,
                      served: set[str]) -> Principal:
    """Map a verified identity to a Principal using server-side memberships only."""
    mships = [m for m in directory.memberships(identity.user_id) if m.organization_id in served]
    if selected_org:
        chosen = [m for m in mships if m.organization_id == selected_org]
        if not chosen:
            raise MembershipError("not a member of the selected organization")
    elif len(mships) == 1:
        chosen = mships
    elif not mships:
        raise MembershipError("no membership in an organization served here")
    else:
        raise OrganizationSelectionRequired("member of several organizations: send the X-Organization header")
    m = chosen[0]
    return Principal(identity.user_id, m.organization_id, ActorType.HUMAN, frozenset(m.roles),
                     frozenset(m.permissions))
