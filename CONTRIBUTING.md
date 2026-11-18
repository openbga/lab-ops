# Contributing to OpenBGA Lab Ops

Bug reports, documentation and pull requests are welcome. Use synthetic data in examples,
screenshots, tests and reports. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md) and
report vulnerabilities through the [security policy](SECURITY.md).

## Before you start

Search existing issues and pull requests. Discuss new workflows, schema changes and dependencies
in an issue before starting implementation. Branch from `main` and open your pull request
against `main`. The [product scope](docs/product.md) and [architecture](docs/architecture.md)
describe the workflows and design constraints.

## Making a change

- Check permissions, capabilities and client ownership on the server for every read and write.
  Commit each change, its concurrency checks and its attributed audit event in one transaction.
- Keep audit history append-only. Add a record and a reason for corrections. Store quantities
  exactly with Python `Decimal` and PostgreSQL `numeric`.
- Add a migration for each schema change. Keep already published migrations intact and document
  upgrade steps. Include regression coverage for changed behaviour.
- Keep deployment policy under the laboratory's control. For a regulatory requirement, cite
  the primary source, jurisdiction and date.
- Follow the [UI standards](docs/ui-standards.md). Check user-facing changes on a phone and a
  desktop in both light and dark themes. Update the operator help in `docs/pages/` for changes
  to a page, field, action, permission or workflow.
- For a change visible to users, deployers or integrators, add a release note and regenerate
  `CHANGELOG.md`. For internal-only changes, explain this in the pull request so a maintainer
  can apply the `no-changelog` label if needed.

### Release notes

Add a file named `changelog.d/YYYY-MM-DD-short-description.md`. Its front matter has four
required fields as in the example below. Write the summary in the terms people see in the
application. For a migration, setting or upgrade, add a `## Technical notes` section.

```markdown
---
date: 2026-11-18
type: added
impact: medium
title: Filter clients by site
---
Staff can narrow the client list to organizations with an active site.
```

Use `added`, `changed`, `fixed`, `removed`, `deprecated` or `security` for the type.
Use `high` for new workflows, security fixes or required operator action, `medium` for a
noticeable improvement and `low` for small fixes. Run `uv run python manage.py changelog` and
include the generated file with your entry.

## Verify your change

Follow [Local development](README.md#local-development). With `DEBUG=true` and the local
`DATABASE_URL` exported, run:

```sh
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py changelog --check
uv run python manage.py collectstatic --noinput
uv run pytest
```

PostgreSQL must allow the configured user to create a separate test database. To run concurrent
test sessions, set `TEST_DATABASE_NAME` to a unique disposable database name beginning with
`test_`. Collect static assets again after changing templates' assets.

### Browser journeys

The headless browser suite starts its own server on an ephemeral port and uses a separate
PostgreSQL test database. With `TEST_DATABASE_NAME` set, the browser database adds the
suffix `_e2e`.

```sh
uv sync --frozen --group e2e
uv run --group e2e playwright install chromium
uv run --group e2e python manage.py collectstatic --noinput
uv run --group e2e pytest -c e2e/pytest.ini e2e
```

On Linux, add `--with-deps` to the browser installation command. CI runs the application checks,
browser journeys and a container test for a fresh database migration, sign-in, static assets
and health endpoints.

### Project website

The static project website in `website/` is published to <https://openbga.github.io/lab-ops/>
by `.github/workflows/pages.yml` when it changes on `main`. The application does not serve it.
State only what this repository's README and documentation support, and use synthetic data in
screenshots. Preview it with
`python3 -m http.server 8118 --bind 127.0.0.1 --directory website` at <http://127.0.0.1:8118/>
and run `uv run pytest tests/test_project_site.py`. Website changes need no release note.

## Pull requests

Describe the change, why it matters and how you tested it. Call out changes to permissions,
stored history or migrations. Include before and after screenshots for interface changes.

## Licence of contributions

By submitting a contribution, you confirm you have the right to submit it under
[AGPL-3.0-only](LICENSE). Preserve [third-party notices](THIRD_PARTY_NOTICES.md).
