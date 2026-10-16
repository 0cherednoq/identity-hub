"""Fail a release before build when tag, package, changelog and schema head diverge."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _quoted_assignment(text: str, name: str) -> str:
    match = re.search(rf'^{re.escape(name)}\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    if match is None:
        raise ValueError(f"не найдено присваивание {name}")
    return match.group(1)


def changelog_schema(changelog: str, version: str) -> str:
    header = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(?P<body>.*?)(?=^## \[|\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    if header is None:
        raise ValueError(f"в CHANGELOG.md нет секции {version}")
    match = re.search(r"Ревизия схемы\s*[—-]\s*`(?P<revision>\d{4})`", header.group("body"))
    if match is None:
        raise ValueError(f"в секции CHANGELOG.md {version} не указана итоговая ревизия схемы")
    return match.group("revision")


def validate_metadata(
    *,
    package_version: str,
    source_version: str,
    schema_head: str,
    changelog: str,
    release_tag: str | None,
) -> None:
    if source_version != package_version:
        raise ValueError(f"src version {source_version} должна совпадать с package version {package_version}")
    if release_tag is not None and release_tag != f"v{package_version}":
        raise ValueError(f"release tag {release_tag} должен совпадать с v{package_version}")
    documented = changelog_schema(changelog, package_version)
    if documented != schema_head:
        raise ValueError(
            f"CHANGELOG.md для {package_version} указывает schema {documented}, код — {schema_head}"
        )


def validate_repository(release_tag: str | None = None) -> tuple[str, str]:
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = str(package["project"]["version"])
    source_version = _quoted_assignment(
        (ROOT / "src" / "identity_hub" / "__init__.py").read_text(encoding="utf-8"),
        "__version__",
    )
    schema_head = _quoted_assignment(
        (ROOT / "src" / "identity_hub" / "postgres" / "migrate.py").read_text(encoding="utf-8"),
        "SCHEMA_HEAD",
    )
    validate_metadata(
        package_version=package_version,
        source_version=source_version,
        schema_head=schema_head,
        changelog=(ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
        release_tag=release_tag,
    )
    return package_version, schema_head


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="GitHub Release tag, например v1.0.0a3")
    args = parser.parse_args()
    try:
        version, schema = validate_repository(args.tag)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"release metadata ok: version={version}, schema={schema}")


if __name__ == "__main__":
    main()
