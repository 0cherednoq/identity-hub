from __future__ import annotations

import pytest
from litestar import Litestar
from litestar.testing import TestClient

pytestmark = pytest.mark.contract


def test_openapi_is_available_with_request_id(http_client: TestClient[Litestar], request_id: str) -> None:
    response = http_client.get("/docs/openapi.json")

    assert response.status_code == 200, f"request_id={request_id} body={response.text}"
    assert response.headers["X-Request-ID"] == request_id
    assert "/health/live" in response.json()["paths"]
