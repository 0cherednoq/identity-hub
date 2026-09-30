"""Standalone command-line entry point."""

from __future__ import annotations

import argparse
import hmac
import importlib
from collections.abc import Sequence

from identity_hub.hub import IdentityHub
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from identity_hub_admin import __version__
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.settings import AdminSettings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="identity-hub-admin")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="start the standalone web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    demo = commands.add_parser("demo", help="start a safe in-memory demo server")
    demo.add_argument("--host")
    demo.add_argument("--port", type=int)
    commands.add_parser("check", help="validate configuration and production assets")
    commands.add_parser("hash-password", help="interactively create an Argon2 password hash")
    return result


def _load_hub(spec: str | None) -> IdentityHub | None:
    if spec is None:
        return None
    if ":" not in spec:
        raise ValueError("IH_ADMIN_HUB_FACTORY must use package.module:callable")
    module_name, attribute = spec.split(":", 1)
    factory = getattr(importlib.import_module(module_name), attribute)
    hub = factory()
    if not isinstance(hub, IdentityHub):
        raise TypeError("admin hub factory must return IdentityHub")
    return hub


def _engine(settings: AdminSettings) -> AsyncEngine | None:
    return create_async_engine(settings.database_url) if settings.database_url else None


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = AdminSettings()
    if args.command == "check":
        errors = settings.startup_errors()
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print("configuration OK")
        return 0
    if args.command == "hash-password":
        import getpass

        from argon2 import PasswordHasher

        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Repeat password: ")
        if not password or not hmac.compare_digest(password, confirmation):
            print("ERROR: passwords are empty or do not match")
            return 1
        print(PasswordHasher().hash(password))
        return 0
    if args.command == "serve":
        import uvicorn

        host = args.host or settings.host
        port = args.port or settings.port
        app = create_admin_app(settings, hub=_load_hub(settings.hub_factory), engine=_engine(settings))
        uvicorn.run(app, host=host, port=port, log_level=settings.log_level.lower())
        return 0
    if args.command == "demo":
        import uvicorn

        from identity_hub_admin.demo import create_demo_hub

        demo_settings = settings.model_copy(update={"demo_mode": True, "run_dispatcher": False})
        errors = demo_settings.startup_errors()
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        host = args.host or demo_settings.host
        port = args.port or demo_settings.port
        app = create_admin_app(demo_settings, hub=create_demo_hub())
        uvicorn.run(app, host=host, port=port, log_level=demo_settings.log_level.lower())
        return 0
    parser().error("choose a command")
    return 2
