"""Small deterministic service registry for admin tests."""

from identity_hub.service import Service
from pydantic import BaseModel


class TestProfile(BaseModel):
    country: str = "UA"
    display_name: str = "Test account"


ADMIN_TEST_SERVICE = Service(
    name="admin_test",
    title="Admin Test",
    profile=TestProfile,
    filterable_profile=frozenset({"country"}),
    searchable_profile=frozenset({"display_name"}),
)
