---
name: project-code-review
description: The pre-PR quality gate for OpenBGA Lab Ops. It reviews the current diff against AGENTS.md (server-side authorization including roles, capabilities, client ownership and lookups, changes committed with their audit events, stale-edit checks, exact values, immutable history), the frontend rules and ui-standards, migrations, tests and page help, fixes what it finds in place, runs the CI checks plus the affected pytest and browser journeys, checks UI changes at phone and desktop widths and captures before/after screenshots, and ends with a summary table. Use it whenever you finish a change and before you commit, push or open a pull request, and whenever someone asks to review, check or verify the work on a branch.
---

# Pre-PR review and gate

This is a gate, not a report: fix what you find, rerun what you changed, and only report what
can't be fixed safely. Keep the review to the diff. Untouched legacy code is out of scope
unless the change makes it wrong.

## 0. Scope the diff

```sh
git fetch origin main -q
git diff --name-only origin/main...HEAD   # committed on this branch
git status --short                            # plus uncommitted and untracked work
```

Sort the files into groups. Each check below says which group it applies to:

| Group | Paths |
| --- | --- |
| Python domain and views | `*/models.py`, `*/services.py`, `*/views*.py`, `*/portal*.py`, `*/permissions.py`, `*/forms.py`, `*/selectors.py`, `accounts/*.py`, `core/*.py`, other app `*.py` |
| Migrations | `*/migrations/*.py` |
| Templates and front end | `templates/**`, `*/templates/**`, `static/css/**`, `static/js/**` |
| Tests | `tests/**`, `*/tests/**`, `e2e/**` |
| Docs and help | `docs/**`, `docs/pages/**` |

If the diff is empty, say "Nothing to review" and stop. Ignore `.local/`. Don't edit
`static/vendor/**`, and don't run Git commands that write unless you own the branch.

## 1. Domain and authorization (Python changes)

Read each changed function together with its callers, against `.agents/rules/backend.md`, which
names the helpers to use (`accounts/roles.py`, `client_required` and client querysets,
`core.audit.record_event`, `core/concurrency.py`, `core/forms.py`, `core/lists.py`, `core/due.py`, `core/lookups.py`).
Code that reimplements one of them is a finding. Fix violations in place:

| # | Check | What to look for | Fix |
| --- | --- | --- | --- |
| 1 | Exact values (rule 3) | `float(`, `round(` or arithmetic on quantities or money; a `FloatField`; formatting that drops trailing zeros of an entered value | `Decimal` / `DecimalField`; keep the entered string |
| 2 | Every read authorized (rule 4) | A view, export, download, HTMX fragment or `core/lookups.py` queryset that doesn't filter by role, or by the portal user's client (`client_scope(queryset, user)` from `clients/portal.py`, or ownership through a client-owned parent) | Scope the queryset on the server; return 404 or 403 for records the user may not see; add a test for the other client's user |
| 3 | Every mutation authorized | A POST handler or service without a role, capability or permission check, relying on a hidden button, `admin` or a model `save()` hook; a capability (`has_capability`) assumed from a role or superuser status | Check in the service (`require_role`, `require_capability`); test the forbidden role |
| 4 | Transition and audit committed together | A state change and its `AuditEvent` or history in separate transactions; a check done before `select_for_update` and not rechecked | One `transaction.atomic()`: lock, recheck, change, record the event |
| 5 | Client isolation and accounts | A client portal account that can reach staff data, a staff account that can reach the portal's data under a client, a client account given a role or capability | `client_required`, `client_scope`; the admin account form rules; test the other client's user |
| 6 | Immutable history | Editing or deleting an audit event or a stored logo; rewriting history instead of adding a corrected record with a reason | Add a new record or event |
| 7 | Concurrency | Read-modify-write without a version check or lock | `select_for_update` or the existing `expected_revision` pattern; a clear stale-edit message |
| 8 | No invented policy | New thresholds, rules a laboratory should decide, regulatory or compliance wording, another vendor's product names | Make it configuration or remove it; flag it for the owner |
| 9 | Secrets and data | Real client, patient or personal names, keys, dumps or runtime output in code, fixtures or logs | Synthetic values only |

## 2. Migrations

- Additive and backward compatible: new nullable or defaulted columns and new tables. No dropped
  or renamed columns in the same release as the code that stops using them. Never edit a
  merged migration.
- `uv run python manage.py makemigrations --check --dry-run` must say "No changes detected".
- A data migration is reversible or says why it isn't, and never rewrites recorded history.

## 3. Templates and front end (follow the build-page skill and `.agents/rules/frontend.md`)

| # | Check | Fix |
| --- | --- | --- |
| 10 | Built from `{% load ui %}` components, no copied component markup, no `{% include %}` of a registered component | Use the tag; add a parameter to the component if needed |
| 11 | Tokens only: no inline `style`, hex, rgb or hsl, raw px or rem, untokened Bootstrap colour utility; muted is `.vr-muted` | Use a `--vr-*` token or class |
| 12 | Icons only via `{% icon %}` or `vrUi.icon`, Bootstrap Icons symbols | Copy the symbol into `static/img/icons.svg` |
| 13 | Controls through form widgets (no raw select, datalist, date, number or file input); large model choices use `RemoteSelect` with an authorized lookup | Render the form field; register the lookup |
| 14 | Page type rules: one primary, at most three secondary plus More, identifier row links, list states, breadcrumbs only where due | ui-standards §2–§6 |
| 15 | Must-see facts (status, deadlines, exceptions) in `page_meta` or a notice, never colour alone | Move them up; add a text or icon cue |
| 15a | Phone: at 360 to 430px no horizontal page scroll, tables readable as stacked rows or cards, touch targets of at least 44px, right input types, nothing hover-only (ui-standards §13) | Fix the markup or component; check both themes |
| 16 | Panel spacing owned by the panel (no padding or margin utilities inside panel bodies) | `panel_section`, `table=True` |
| 17 | Wording (ui-standards §9), sentence case, the same term as the sidebar and title | Rename |

## 4. Tests and page help

- New or changed behaviour has a test next to the app's existing tests: authorization (the
  allowed role and a refused one, and another client's user on portal or lookup code),
  changes and their audit events, exact values, and the stale-edit path where relevant. Fix the
  code, not the test, unless the test is provably wrong. A test that guards an authorization
  gate is never loosened to make it pass.
- Any page, field, action, filter, permission or rule change has its help updated in the same
  change: run the `document-page` skill. New POST-only, download or JSON routes go in
  `NON_PAGE_ROUTES` in `tests/test_page_docs.py`.
- Any change users, deployers or integrators can observe has an entry in `changelog.d/` and a
  regenerated `CHANGELOG.md`: run the `changelog` skill. Nothing observable: the pull request
  says so.

## 5. Run the gate

From the repository root, with your own test database so parallel runs don't collide:

```sh
export DEBUG=true   # and your local DATABASE_URL (see README)
uv run ruff check . && uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py changelog --check
uv run python manage.py collectstatic --noinput -v0
TEST_DATABASE_NAME=test_lab_ops_<you> uv run pytest <affected paths> -q
```

Affected paths are the changed apps' `tests/`, the `tests/test_*.py` that cover what you changed
(`test_ui_standards`, `test_css_tokens`, `test_include_isolation`, `test_controls`, `test_icons`,
`test_navigation` and `test_page_docs` for any template change), and the full `uv run pytest`
when shared code (`core/`, `templates/includes/`, `static/css/app.css`, settings) or a
migration changed. Fix and rerun until green. Run each failing test once more by itself before
calling it flaky.

When templates, `static/js` or `static/css` changed, run the browser journeys that open those
pages, plus the layout checks for any UI diff. Use an isolated port and database (CONTRIBUTING,
"Browser journeys"):

```sh
uv run --group e2e python manage.py collectstatic --noinput -v0
# find the journeys that visit the changed pages: grep -ln "<url path or route>" e2e/test_*.py
TEST_DATABASE_NAME=test_lab_ops_<you> uv run --group e2e pytest -c e2e/pytest.ini \
  e2e/<affected journeys>.py e2e/test_panel_spacing.py e2e/test_alignment.py e2e/test_theme.py
```

The browser journeys include a phone viewport (`NARROW` in `e2e/conftest.py`); a new page needs
a journey step there as well as at desktop width.

## 6. PR evidence: before/after screenshots (any UI change)

A PR that changes what a page looks like shows before and after in its description. The
repository has no capture script yet, so take them with browser tooling: run the dev server on
127.0.0.1:8000 with demo data (`seed_demo`), sign in as `demo_admin` (or `demo_client` for the
portal), and capture each changed page in light and dark at a desktop width and at a phone width
(390px). "Before" is the base branch served the same way from a temporary worktree on another
port, with the same database. Keep the files under `.local/` (git-ignored) and attach the
relevant pairs to the PR description. Never commit the screenshots.

## 7. Summary

End with this table, then a short list of anything that needs a human:

```markdown
## Review summary

| Area | Result | Notes |
| --- | --- | --- |
| Scope | N files (Python, templates, migrations, tests, docs) | |
| Domain and authorization | Pass / Fixed N | file:line, what changed |
| Migrations | None / Additive, check clean | |
| Templates and UI rules | Pass / Fixed N | |
| Tests added or updated | Yes / Not needed (why) | |
| Page help | Updated / Not needed (pure refactor) | |
| ruff, format, check, makemigrations, collectstatic | Pass | |
| pytest (paths) | N passed | failures and their owner if not yours |
| Browser journeys and layout checks | N passed / Not run (no UI change) | |
| Screenshots | Attached to the PR / Not needed | |

### Needs a human
- Decisions about policy or behaviour, failures outside this diff, anything not fixed and why.
```

Flag rather than auto-fix a change that would alter behaviour, a public URL or stored data. Say
so explicitly when the diff affects authorization, stored history or a migration: the PR
template asks reviewers to look there.
