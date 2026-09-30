"""Seeded development application imported by the Litestar CLI."""

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import create_demo_hub
from identity_hub_admin.settings import AdminSettings

app = create_admin_app(
    AdminSettings(demo_mode=True, run_dispatcher=False),
    hub=create_demo_hub(),
)
