"""Safe SPA responses with explicit cache behavior."""

from __future__ import annotations

import re
from mimetypes import guess_type
from pathlib import Path

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import File

from identity_hub_admin.api.routes import route_meta

_HASHED_ASSET = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")


def spa_handlers(static_dir: Path) -> list[object]:
    root = static_dir.resolve()
    assets = (root / "assets").resolve()
    index = root / "index.html"

    @get(
        "/{client_path:path}",
        operation_id="admin_spa_fallback_get",
        include_in_schema=False,
        opt=route_meta(auth_required=False),
        sync_to_thread=False,
    )
    def fallback(client_path: FromPath[str]) -> File:
        client_path = client_path.lstrip("/")
        if client_path.startswith("assets/"):
            candidate = (assets / client_path.removeprefix("assets/")).resolve()
            if not candidate.is_relative_to(assets) or not candidate.is_file():
                raise NotFoundException()
            cache = (
                "public, max-age=31536000, immutable"
                if _HASHED_ASSET.fullmatch(candidate.name)
                else "no-cache"
            )
            return File(
                path=candidate,
                headers={"Cache-Control": cache},
                media_type=guess_type(candidate.name)[0] or "application/octet-stream",
                content_disposition_type="inline",
                filename=candidate.name,
            )
        if client_path.startswith(("api/", "docs/", "health/")) or Path(client_path).suffix:
            raise NotFoundException()
        if not index.is_file():
            raise NotFoundException("SPA build is not installed")
        return File(
            path=index,
            headers={"Cache-Control": "no-cache"},
            media_type="text/html",
            content_disposition_type="inline",
            filename="index.html",
        )

    return [fallback]
