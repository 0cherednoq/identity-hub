# Publishing Identity Hub

This runbook is for maintainers publishing `identity-hub` to PyPI and attaching the same verified
artifacts to a GitHub Release. Publishing uses PyPI Trusted Publishing, so the repository does not
need a long-lived PyPI token.

## Configure publishing once

1. Push the repository to GitHub.
2. Create a GitHub environment named `pypi`. Add required reviewers if the repository plan supports
   environment protection.
3. In the PyPI project settings, add a GitHub Trusted Publisher with these values:

   | Setting | Value |
   | --- | --- |
   | Owner | GitHub organization or user that owns the repository |
   | Repository | GitHub repository name |
   | Workflow | `release.yml` |
   | Environment | `pypi` |

4. Protect tags matching `v*` so only maintainers can create release tags.

PyPI can create a pending project during the first trusted publication. If the `identity-hub` name
is already owned by another PyPI account, choose a new distribution name in `pyproject.toml` before
the first release.

## Publish a release

1. Update `version` in `pyproject.toml` and record user-visible changes in `CHANGELOG.md`.
2. Run the fast local gate:

   ```bash
   IH_TEST_BACKENDS=memory uv run pytest -m "not pg and not load" -q
   uv run ruff format --check .
   uv run ruff check .
   uv build --out-dir dist
   uvx --from twine twine check dist/*
   bash scripts/check_package.sh dist
   ```

3. Merge through a pull request and wait for the required `Library CI` checks.
4. Create a GitHub Release whose tag is exactly `v<project.version>`, for example `v1.0.0a1`.

Publishing starts only after the GitHub Release is published. The workflow builds once, verifies the
wheel and source distribution, stores build provenance, attaches both files to the GitHub Release,
and uploads the same files to PyPI.

## Verify the release

1. Confirm both `.whl` and `.tar.gz` files appear on the GitHub Release and PyPI.
2. Install from PyPI in an empty environment:

   ```bash
   uv venv --python 3.12 /tmp/identity-hub-release-check
   uv pip install --python /tmp/identity-hub-release-check/bin/python \
     "identity-hub[postgres,alembic]==1.0.0a1"
   /tmp/identity-hub-release-check/bin/identity-hub --help
   ```

3. Verify GitHub provenance after replacing `OWNER/REPOSITORY` and the artifact path:

   ```bash
   gh attestation verify identity_hub-1.0.0a1-py3-none-any.whl \
     --repo OWNER/REPOSITORY
   ```

## Recover from a failed release

- If validation fails before upload, fix the source, increment the version, and publish a new release.
- If PyPI already accepted a file, do not reuse that version. PyPI release files are immutable.
- If Trusted Publishing fails, verify the repository owner, repository name, `release.yml`, and
  `pypi` environment in the PyPI publisher settings. Do not add an API token as a fallback.
