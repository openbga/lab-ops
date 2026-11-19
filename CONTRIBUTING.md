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

### Security checks

Every pull request also runs the security checks listed in the
[security policy](SECURITY.md#what-is-checked): static analysis, a dependency audit, a secret scan
and a container image scan. To run the last three before pushing, install
[gitleaks](https://github.com/gitleaks/gitleaks), [Grype](https://github.com/anchore/grype) and
[Syft](https://github.com/anchore/syft). The commands write their output under `build/`, which Git
ignores. The dependency audit lists a copy of the files Git would commit, so that local state such
as `.venv/` or `.local/` is not scanned:

```sh
rm -rf build/scan && mkdir -p build/scan
git ls-files --cached --others --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C build/scan
syft scan "dir:$PWD/build/scan" --config .syft.yaml --output cyclonedx-json@1.6=build/dependencies.cdx.json
python3 tools/security/sbom_coverage.py build/dependencies.cdx.json --locked
python3 tools/security/grype_gate.py dependencies --grype "$(command -v grype)" build/dependencies.cdx.json
```

The secret scan reads the history as text, merge commits included, file by file and then as one
stream so that no file is skipped by its name:

```sh
gitleaks git --log-opts="--text --diff-merges=first-parent HEAD" --ignore-gitleaks-allow \
  --redact --config .gitleaks.toml .
git log -p --text --diff-merges=first-parent HEAD \
  | gitleaks stdin --ignore-gitleaks-allow --redact --config .gitleaks.toml
```

The image scan builds the image, lists it and checks the listing against what the image reports:

```sh
docker build --tag lab-ops:local .
syft scan docker:lab-ops:local --config .syft.yaml --output cyclonedx-json@1.6=build/lab-ops.cdx.json
docker run --rm --entrypoint python --volume "$PWD/tools/security:/probe:ro" lab-ops:local \
  /probe/image_contents.py > build/image-contents.json
python3 tools/security/sbom_coverage.py build/lab-ops.cdx.json --image build/image-contents.json
python3 tools/security/grype_gate.py image --grype "$(command -v grype)" build/lab-ops.cdx.json
```

When a check fails:

- **A real secret**: revoke or rotate it first. Removing it from the branch does not remove it
  from history, and anything pushed to a public branch must be treated as exposed.
- **A synthetic test value**: add the exact value to `.gitleaks.toml` with a comment saying where
  it is used.
- **A vulnerable dependency**: update it (`uv lock --upgrade-package NAME`, a newer base image, or
  a new vendored copy as `static/vendor/README.md` describes). If the vulnerability does not apply
  or no fixed release exists yet, add an entry to `.grype.yaml` with the package version and the
  reason, and say so in the pull request.
- **A static analysis alert**: fix the code. A false positive is dismissed in the alert with the
  reason, by a maintainer.
- **"cannot be trusted"** or **"does not list"**: the check could not prove that it would find a
  problem, so its clean result is not used. Read the message: a scanner or database that did not
  load, a setting that hides the known finding, or a package the listing misses.

### Presentation website

The [product website](https://openbga.github.io/lab-ops/) is built from `website/` and published
from `main` by the `pages` workflow. Its roadmap page is rendered from `docs/roadmap.md` each
time, so change planned work there. The build needs only Python and stops at a broken link or
at roadmap Markdown the page cannot show. To preview a change:

```sh
python3 tools/website/build.py
python3 -m http.server --bind 127.0.0.1 --directory build/website
```

After changing a page that appears in the site's screenshots, regenerate them with the command
in `e2e/test_website_shots.py`, then check their alt text and captions in `website/index.html`.

## Pull requests

Describe the change, why it matters and how you tested it. Call out changes to permissions,
stored history or migrations. Include before and after screenshots for interface changes.

## Licence of contributions

By submitting a contribution, you confirm you have the right to submit it under
[AGPL-3.0-only](LICENSE). Preserve [third-party notices](THIRD_PARTY_NOTICES.md).
