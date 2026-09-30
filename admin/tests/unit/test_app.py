from litestar.testing import TestClient

from identity_hub_admin import __version__
from identity_hub_admin.app import create_admin_app


def test_package_has_version() -> None:
    assert __version__


def test_liveness_smoke() -> None:
    with TestClient(create_admin_app()) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "test-live-001"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    assert response.headers["X-Request-ID"] == "test-live-001"


def test_invalid_request_id_is_not_reflected() -> None:
    with TestClient(create_admin_app()) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "unsafe value"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "unsafe value"
