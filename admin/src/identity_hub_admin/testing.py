"""Deterministic application used only by the browser quality harness.

The production and development entry points never import this module.  It gives Playwright
stable principals for all three roles and one operator-only endpoint with which to verify that
the backend, rather than the UI, enforces RBAC.
"""

from __future__ import annotations

import json

from litestar import post
from pydantic import SecretStr

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


class E2EOperatorProbe(ContractModel):
    allowed: bool = True


@post(
    "/api/v1/e2e/operator-probe",
    operation_id="admin_v1_e2e_operator_probe_post",
    tags=["e2e"],
    opt=route_meta("operator", "admin"),
    responses=error_responses(401, 403),
    sync_to_thread=False,
)
def operator_probe() -> ResourceEnvelope[E2EOperatorProbe]:
    """Return a harmless response after the regular RBAC guard succeeds."""
    return ResourceEnvelope(data=E2EOperatorProbe())


_TOKENS = {
    f"e2e-{role}-token": {
        "actor": f"e2e:{role}",
        "role": role,
        # Mutation scenarios use demo-loading so parallel read and visual tests for
        # demo-large cannot observe an intermediate lifecycle state.
        "allowed_spaces": ["demo-large", "demo-empty"]
        + (["demo-loading"] if role in {"operator", "admin"} else []),
    }
    for role in ("viewer", "operator", "admin")
}


app = create_admin_app(
    AdminSettings(
        environment=Environment.TEST,
        demo_mode=True,
        run_dispatcher=False,
        bearer_tokens=SecretStr(json.dumps(_TOKENS)),
        password_username="e2e-admin",
        password_hash=SecretStr(
            "$argon2id$v=19$m=65536,t=3,p=4$9LA/mESZG+ol+uhjqjrgEg$"
            "FWcKZWeMVPNwLXAa3rmU0Q90bMGoKRgBFPylmU/wGCs"
        ),
        password_role="admin",
        password_allowed_spaces=("demo-large", "demo-empty"),
        session_secret=SecretStr("identity-hub-admin-e2e-session-secret"),
    ),
    hub=create_demo_hub(),
    route_handlers=(operator_probe,),
)
