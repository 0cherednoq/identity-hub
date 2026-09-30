"""Authentication, signed sessions, CSRF context, and role authorization."""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address, ip_network
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from litestar.connection import ASGIConnection
from litestar.exceptions import (
    HTTPException,
    NotAuthorizedException,
    NotFoundException,
    PermissionDeniedException,
)
from litestar.handlers import BaseRouteHandler
from litestar.middleware.authentication import AbstractAuthenticationMiddleware, AuthenticationResult
from pydantic import BaseModel, ConfigDict

from identity_hub_admin.settings import AdminSettings, AuthMode

SESSION_COOKIE = "ih_admin_session"
CSRF_COOKIE = "ih_admin_csrf"
CSRF_HEADER = "x-csrf-token"
TRUSTED_ACTOR_HEADER = "x-admin-actor"
TRUSTED_ROLE_HEADER = "x-admin-role"
TRUSTED_SPACES_HEADER = "x-admin-spaces"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$iAQrG+Baa7A31wOjxjgUog$k2E5Phnls9FojIm1LbfxQh8+VhHDkn8+mQcyFnoDuYk"
)


class AdminRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class AdminPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str
    role: AdminRole
    allowed_spaces: frozenset[str]

    def can_access_space(self, slug: str) -> bool:
        return "*" in self.allowed_spaces or slug in self.allowed_spaces


class TokenPrincipalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    role: AdminRole
    allowed_spaces: frozenset[str]


@dataclass(frozen=True, slots=True)
class AdminAuthContext:
    scheme: Literal["bearer", "session", "trusted_proxy"]
    csrf_token: str | None = None


class SlidingWindowRateLimiter:
    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        with self._lock:
            now = float(self._clock())
            attempts = self._attempts[key]
            cutoff = now - window_seconds
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= limit:
                return False
            attempts.append(now)
            return True


class SessionSigner:
    def __init__(self, secret: bytes, ttl_seconds: int) -> None:
        self._secret = secret
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def dumps(self, principal: AdminPrincipal, csrf_token: str) -> str:
        now = int(time.time())
        payload = json.dumps(
            {
                "actor": principal.actor,
                "role": principal.role,
                "allowed_spaces": sorted(principal.allowed_spaces),
                "csrf": csrf_token,
                "iat": now,
                "exp": now + self.ttl_seconds,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = self._encode(payload)
        signature = self._encode(hmac.digest(self._secret, encoded.encode(), "sha256"))
        return f"{encoded}.{signature}"

    def loads(self, value: str) -> tuple[AdminPrincipal, str]:
        try:
            encoded, signature = value.split(".", 1)
            expected = self._encode(hmac.digest(self._secret, encoded.encode(), "sha256"))
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            payload = json.loads(self._decode(encoded))
            if not isinstance(payload, dict) or int(payload["exp"]) <= int(time.time()):
                raise ValueError("expired session")
            principal = AdminPrincipal(
                actor=str(payload["actor"]),
                role=AdminRole(str(payload["role"])),
                allowed_spaces=frozenset(str(item) for item in payload["allowed_spaces"]),
            )
            return principal, str(payload["csrf"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise NotAuthorizedException("authentication required") from None


class AuthService:
    def __init__(self, settings: AdminSettings) -> None:
        self.settings = settings
        secret = (
            settings.session_secret.get_secret_value().encode()
            if settings.session_secret is not None
            else secrets.token_bytes(32)
        )
        self.sessions = SessionSigner(secret, settings.session_ttl_seconds)
        self.password_hasher = PasswordHasher()
        self.rate_limiter = SlidingWindowRateLimiter()
        raw_tokens = settings.bearer_tokens.get_secret_value() if settings.bearer_tokens else None
        self.tokens = self._parse_tokens(raw_tokens)

    @staticmethod
    def _parse_tokens(raw: str | None) -> dict[str, AdminPrincipal]:
        if raw is None:
            return {}
        try:
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError
            return {
                token: AdminPrincipal.model_validate(TokenPrincipalSpec.model_validate(spec).model_dump())
                for token, spec in document.items()
                if isinstance(token, str) and token
            }
        except (TypeError, ValueError):
            raise ValueError("IH_ADMIN_BEARER_TOKENS must be a JSON object of principal records") from None

    def authenticate_bearer(self, token: str) -> AdminPrincipal:
        for candidate, principal in self.tokens.items():
            if hmac.compare_digest(token, candidate):
                return principal
        raise NotAuthorizedException("authentication required")

    def authenticate_session(self, cookie: str) -> tuple[AdminPrincipal, str]:
        return self.sessions.loads(cookie)

    def authenticate_password(self, username: str, password: str) -> AdminPrincipal:
        configured_hash = (
            self.settings.password_hash.get_secret_value()
            if self.settings.password_hash is not None
            else _DUMMY_HASH
        )
        valid_password = False
        try:
            valid_password = self.password_hasher.verify(configured_hash, password)
        except (InvalidHashError, VerificationError):
            valid_password = False
        valid_username = hmac.compare_digest(username.encode(), self.settings.password_username.encode())
        if not (valid_password and valid_username and self.settings.password_hash is not None):
            raise NotAuthorizedException("invalid credentials")
        return AdminPrincipal(
            actor=f"admin:{self.settings.password_username}",
            role=AdminRole(self.settings.password_role),
            allowed_spaces=frozenset(self.settings.password_allowed_spaces),
        )

    def trusted_proxy_principal(self, source: str, headers: Any) -> AdminPrincipal:
        try:
            remote = ip_address(source)
        except ValueError:
            raise NotAuthorizedException("authentication required") from None
        if not any(remote in ip_network(cidr, strict=False) for cidr in self.settings.trusted_proxy_cidrs):
            raise NotAuthorizedException("authentication required")
        actor = headers.get(TRUSTED_ACTOR_HEADER)
        role = headers.get(TRUSTED_ROLE_HEADER)
        spaces = headers.get(TRUSTED_SPACES_HEADER)
        if not actor or not role or not spaces:
            raise NotAuthorizedException("authentication required")
        try:
            return AdminPrincipal(
                actor=str(actor),
                role=AdminRole(str(role)),
                allowed_spaces=frozenset(item.strip() for item in str(spaces).split(",") if item.strip()),
            )
        except ValueError:
            raise NotAuthorizedException("authentication required") from None

    def check_login_rate(self, source: str) -> None:
        if not self.rate_limiter.allow(
            f"login:{source}",
            limit=self.settings.login_rate_limit,
            window_seconds=60,
        ):
            raise HTTPException(
                "login rate limit exceeded",
                status_code=429,
                headers={"Retry-After": "60"},
            )

    def check_reveal_rate(self, actor: str) -> None:
        if not self.rate_limiter.allow(
            f"reveal:{actor}",
            limit=self.settings.reveal_rate_limit,
            window_seconds=60,
        ):
            raise HTTPException(
                "reveal rate limit exceeded",
                status_code=429,
                headers={"Retry-After": "60"},
            )

    def verify_reauthentication(self, principal: AdminPrincipal, proof: str) -> None:
        """Require a freshly submitted password or the principal's bearer token."""
        for token, candidate in self.tokens.items():
            if candidate == principal and hmac.compare_digest(token, proof):
                return
        if self.settings.password_hash is not None:
            try:
                verified = self.authenticate_password(self.settings.password_username, proof)
            except NotAuthorizedException:
                verified = None
            if verified is not None and verified.role == AdminRole.ADMIN:
                return
        raise NotAuthorizedException("fresh reauthentication failed")


class AdminAuthenticationMiddleware(AbstractAuthenticationMiddleware):
    def __init__(self, app: Any, service: AuthService) -> None:
        super().__init__(app, exclude_from_auth_key="exclude_from_auth")
        self.service = service

    async def authenticate_request(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> AuthenticationResult:
        if not connection.url.path.startswith("/api/v1/"):
            return AuthenticationResult(user=None, auth=None)

        authorization = connection.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            principal = self.service.authenticate_bearer(authorization.removeprefix("Bearer "))
            return AuthenticationResult(principal, AdminAuthContext("bearer"))

        session = connection.cookies.get(SESSION_COOKIE)
        if session:
            principal, csrf_token = self.service.authenticate_session(session)
            return AuthenticationResult(principal, AdminAuthContext("session", csrf_token))

        proxy_headers_present = any(
            connection.headers.get(header)
            for header in (TRUSTED_ACTOR_HEADER, TRUSTED_ROLE_HEADER, TRUSTED_SPACES_HEADER)
        )
        if proxy_headers_present and self.service.settings.auth_mode != AuthMode.TRUSTED_PROXY:
            raise NotAuthorizedException("authentication required")
        if self.service.settings.auth_mode == AuthMode.TRUSTED_PROXY:
            client = connection.client
            source = client.host if client is not None else ""
            principal = self.service.trusted_proxy_principal(source, connection.headers)
            return AuthenticationResult(principal, AdminAuthContext("trusted_proxy"))

        raise NotAuthorizedException("authentication required")


async def admin_auth_guard(
    connection: ASGIConnection[Any, Any, Any, Any], route_handler: BaseRouteHandler
) -> None:
    if not route_handler.opt.get("auth_required", False):
        return
    principal = connection.user
    auth = connection.auth
    if not isinstance(principal, AdminPrincipal) or not isinstance(auth, AdminAuthContext):
        raise NotAuthorizedException("authentication required")
    roles = route_handler.opt.get("admin_roles", ())
    if principal.role not in roles:
        raise PermissionDeniedException("access denied")
    if route_handler.opt.get("space_scoped", False):
        space_slug = connection.path_params.get("space_slug")
        if not isinstance(space_slug, str) or not principal.can_access_space(space_slug):
            raise NotFoundException("space not found")
        runtime = connection.app.state.runtime
        if runtime.hub is None:
            raise HTTPException("Identity Hub is unavailable", status_code=503)
        space = await runtime.hub.get_space(space_slug)
        if space is None:
            raise NotFoundException("space not found")
        connection.state.space_handle = space
    if connection.scope.get("method") in MUTATING_METHODS and auth.scheme == "session":
        cookie = connection.cookies.get(CSRF_COOKIE, "")
        header = connection.headers.get(CSRF_HEADER, "")
        expected = auth.csrf_token or ""
        if (
            not expected
            or not hmac.compare_digest(cookie, expected)
            or not hmac.compare_digest(header, expected)
        ):
            raise PermissionDeniedException("CSRF validation failed")
