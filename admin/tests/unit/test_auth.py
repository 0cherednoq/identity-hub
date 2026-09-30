from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher
from litestar import Litestar, get
from litestar.exceptions import HTTPException, NotAuthorizedException
from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.api.routes import route_meta
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    TRUSTED_ACTOR_HEADER,
    TRUSTED_ROLE_HEADER,
    TRUSTED_SPACES_HEADER,
    AdminPrincipal,
    AdminRole,
    AuthService,
)
from identity_hub_admin.settings import AdminSettings, AuthMode, Environment


@get(
    "/api/v1/probe/viewer",
    operation_id="test_auth_probe_viewer",
    opt=route_meta("viewer", "operator", "admin"),
    sync_to_thread=False,
)
def viewer_probe() -> dict[str, bool]:
    return {"ok": True}


@get(
    "/api/v1/probe/operator",
    operation_id="test_auth_probe_operator",
    opt=route_meta("operator", "admin"),
    sync_to_thread=False,
)
def operator_probe() -> dict[str, bool]:
    return {"ok": True}


@get(
    "/api/v1/probe/admin",
    operation_id="test_auth_probe_admin",
    opt=route_meta("admin"),
    sync_to_thread=False,
)
def admin_probe() -> dict[str, bool]:
    return {"ok": True}


def token_settings() -> AdminSettings:
    tokens = {
        f"token-{role}": {
            "actor": f"test:{role}",
            "role": role,
            "allowed_spaces": ["alpha"],
        }
        for role in ("viewer", "operator", "admin")
    }
    return AdminSettings(
        environment=Environment.TEST,
        auth_mode=AuthMode.TOKEN,
        bearer_tokens=SecretStr(json.dumps(tokens)),
    )


def password_settings(*, login_rate_limit: int = 5) -> AdminSettings:
    password_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("correct horse")
    return AdminSettings(
        environment=Environment.TEST,
        auth_mode=AuthMode.PASSWORD,
        password_hash=SecretStr(password_hash),
        session_secret=SecretStr("test-session-secret-with-at-least-32-bytes"),
        password_allowed_spaces=("alpha", "beta"),
        login_rate_limit=login_rate_limit,
    )


@pytest.fixture
def role_client() -> Iterator[TestClient[Litestar]]:
    app = create_admin_app(
        token_settings(),
        route_handlers=(viewer_probe, operator_probe, admin_probe),
    )
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("viewer", (200, 403, 403)),
        ("operator", (200, 200, 403)),
        ("admin", (200, 200, 200)),
    ],
)
def test_role_endpoint_matrix(
    role_client: TestClient[Litestar], role: str, expected: tuple[int, int, int]
) -> None:
    headers = {"Authorization": f"Bearer token-{role}"}
    actual = tuple(
        role_client.get(f"/api/v1/probe/{endpoint}", headers=headers).status_code
        for endpoint in ("viewer", "operator", "admin")
    )

    assert actual == expected


def test_bearer_principal_and_invalid_token(role_client: TestClient[Litestar]) -> None:
    response = role_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer token-viewer"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "actor": "test:viewer",
        "role": "viewer",
        "allowed_spaces": ["alpha"],
    }
    assert role_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        role_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Bearer token-viewer"},
        ).status_code
        == 200
    )


def test_password_login_cookie_flags_csrf_and_logout() -> None:
    app = create_admin_app(password_settings())
    with TestClient(app, base_url="https://admin.test") as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct horse"},
        )
        cookies = response.headers.get_list("set-cookie")

        assert response.status_code == 200
        session_cookie = next(value for value in cookies if value.startswith(f"{SESSION_COOKIE}="))
        csrf_cookie = next(value for value in cookies if value.startswith(f"{CSRF_COOKIE}="))
        assert "HttpOnly" in session_cookie
        assert "Secure" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert "Secure" in csrf_cookie
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 403

        csrf = client.cookies.get(CSRF_COOKIE)
        assert csrf is not None
        logged_out = client.post("/api/v1/auth/logout", headers={CSRF_HEADER: csrf})
        assert logged_out.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401


def test_wrong_username_and_password_have_identical_errors() -> None:
    app = create_admin_app(password_settings())
    with TestClient(app, base_url="https://admin.test") as client:
        wrong_user = client.post(
            "/api/v1/auth/login",
            json={"username": "missing", "password": "correct horse"},
        )
        wrong_password = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
        )

    assert wrong_user.status_code == wrong_password.status_code == 401
    wrong_user_error = wrong_user.json()["error"]
    wrong_password_error = wrong_password.json()["error"]
    assert {key: value for key, value in wrong_user_error.items() if key != "request_id"} == {
        key: value for key, value in wrong_password_error.items() if key != "request_id"
    }
    assert wrong_user_error["request_id"] != wrong_password_error["request_id"]


def test_login_rate_limit_is_stable() -> None:
    app = create_admin_app(password_settings(login_rate_limit=2))
    with TestClient(app, base_url="https://admin.test") as client:
        for _ in range(2):
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong"},
                ).status_code
                == 401
            )
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


def test_signed_session_rejects_tampering_and_reveal_is_rate_limited() -> None:
    settings = password_settings().model_copy(update={"reveal_rate_limit": 1})
    service = AuthService(settings)
    principal = AdminPrincipal(
        actor="admin:admin",
        role=AdminRole.ADMIN,
        allowed_spaces=frozenset({"alpha"}),
    )
    session = service.sessions.dumps(principal, "csrf-token")

    with pytest.raises(NotAuthorizedException):
        service.sessions.loads(f"{session[:-1]}x")
    service.check_reveal_rate(principal.actor)
    with pytest.raises(HTTPException) as error:
        service.check_reveal_rate(principal.actor)
    assert error.value.status_code == 429


def test_spoofed_proxy_headers_are_rejected_but_trusted_source_is_accepted() -> None:
    headers = {
        TRUSTED_ACTOR_HEADER: "oidc:alice",
        TRUSTED_ROLE_HEADER: "operator",
        TRUSTED_SPACES_HEADER: "alpha,beta",
    }
    settings = AdminSettings(
        environment=Environment.TEST,
        auth_mode=AuthMode.TRUSTED_PROXY,
        trusted_proxy_cidrs=("10.10.0.0/16",),
    )
    service = AuthService(settings)

    with pytest.raises(NotAuthorizedException):
        service.trusted_proxy_principal("203.0.113.10", headers)
    principal = service.trusted_proxy_principal("10.10.2.4", headers)
    assert principal.actor == "oidc:alice"
    assert principal.role == "operator"
    assert principal.allowed_spaces == {"alpha", "beta"}

    with TestClient(create_admin_app(settings)) as client:
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_security_headers_are_present_on_public_response() -> None:
    with TestClient(create_admin_app(AdminSettings(environment=Environment.TEST))) as client:
        response = client.get("/health/live")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
