"""Development-only endpoints that describe safe demo scenarios."""

from __future__ import annotations

from litestar import get
from litestar.datastructures import State

from identity_hub_admin.api.contracts import ContractModel, ResourceEnvelope
from identity_hub_admin.api.routes import route_meta
from identity_hub_admin.demo import DEMO_SPACES, DemoState, demo_counts
from identity_hub_admin.runtime import AdminRuntime


class DemoScenario(ContractModel):
    slug: str
    title: str
    state: DemoState
    description: str
    account_count: int


class DemoCatalog(ContractModel):
    scenarios: list[DemoScenario]
    credentials: str = "none"


@get(
    "/api/v1/demo/scenarios",
    operation_id="admin_v1_demo_scenarios_get",
    tags=["demo"],
    opt=route_meta(auth_required=False),
)
async def demo_scenarios(state: State) -> ResourceEnvelope[DemoCatalog]:
    runtime: AdminRuntime = state.runtime
    if runtime.hub is None:
        raise RuntimeError("demo hub is not configured")
    counts = await demo_counts(runtime.hub)
    scenarios = [
        DemoScenario(
            slug=spec.slug,
            title=spec.title,
            state=spec.state,
            description=spec.description,
            account_count=counts[spec.slug],
        )
        for spec in DEMO_SPACES
    ]
    return ResourceEnvelope(data=DemoCatalog(scenarios=scenarios))


DEMO_ROUTE_HANDLERS = [demo_scenarios]
