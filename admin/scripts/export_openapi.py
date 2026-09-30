"""Write or verify the deterministic OpenAPI document."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from identity_hub_admin.api.audit import audit_application
from identity_hub_admin.app import create_admin_app

OUTPUT = Path(__file__).resolve().parents[1] / "openapi.json"


def render_openapi() -> str:
    app = create_admin_app()
    errors = audit_application(app)
    if errors:
        raise RuntimeError("invalid admin contract:\n" + "\n".join(errors))
    return json.dumps(app.openapi_schema.to_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_openapi()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit("admin/openapi.json is out of date; run poe export-openapi")
        print("admin/openapi.json is current")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
