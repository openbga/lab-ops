---
name: changelog
description: Record a change for What's new and CHANGELOG.md in OpenBGA Lab Ops. Every change that users, deployers or integrators can observe adds one entry file in changelog.d/ in the same change and regenerates CHANGELOG.md. Covers when an entry is needed, the file format, how to rate impact, how to word it, the regenerate and check commands, and how to move hand edits of CHANGELOG.md into entry files. Use it whenever you finish a feature, fix, removal, security fix, migration, setting, dependency or upgrade step, before you commit or open a pull request, and whenever the changelog check fails in CI.
---

# Record a change

What's new (top bar, `/whats-new/`) and `CHANGELOG.md` are built from one source: one Markdown
file per change in `changelog.d/`. Parallel branches each add their own file, so they never
conflict. `CHANGELOG.md` is generated and never edited by hand. The parser and the page live in
`core/changelog.py`, `core/views.py` (`whats_new`) and `templates/core/whats_new.html`.

## When an entry is needed

| Change | Entry? |
| --- | --- |
| New page, workflow, field, action, notification, import or export format | Yes |
| Changed behaviour, wording people rely on, permissions, defaults, limits | Yes |
| Bug fix someone could notice | Yes |
| Removal or deprecation | Yes |
| Security fix or hardening | Yes |
| Migration, new setting or environment variable, management command, dependency, Docker or Render change, upgrade step | Yes, with `## Technical notes` |
| Refactor, tests, CI, agent instructions, internal docs, presentation website (`website/`), roadmap | No |

A pull request with nothing observable carries the `no-changelog` label and says so in its
description. One coherent change is usually one entry; a large pull request that delivers
separate capabilities may add one entry for each.

## The file

`changelog.d/YYYY-MM-DD-short-slug.md`, dated the day you write it (the merge day, if you know
it). Slug: a few lowercase words joined by hyphens.

```
---
date: 2026-11-17
type: added
impact: high
title: Find clients missing an active contact
area: Client organizations
link: clients:client-list
pr: 23
---
Filter Client organizations to see which accounts need a contact.

## Technical notes

Optional. Migrations, settings, commands, dependencies, upgrade steps: what a deployer needs.
```

| Key | Required | Value |
| --- | --- | --- |
| `date` | Yes | `YYYY-MM-DD` |
| `type` | Yes | `added`, `changed`, `fixed`, `removed`, `deprecated` or `security` ([Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)) |
| `impact` | Yes | `high`, `medium` or `low` (below) |
| `title` | Yes | Sentence case, no trailing period, written for users |
| `area` | No | The sidebar item as users know it: `Client organizations`, `Announcements` (`core/templatetags/navigation.py`) |
| `link` | No | A route name without URL arguments, such as `clients:client-list`. What's new shows a "Go to" link only to users whose menu has that page |
| `pr` | No | The pull request number, once known |
| `highlight` | No | Use `true` for a change most staff will want to know about at a glance. Defaults to `false`. |

Front matter is flat `key: value` lines (no quotes, no lists). An unknown key, a missing
required key, a bad value or a link that names no page fails `manage.py changelog`, and the
message names the file and the key.

## Rating impact

- **high**: a new capability or workflow; anything a user or deployer must act on (a migration
  that cannot be reversed, a new required setting, an upgrade step); a change to how people
  work; a removal; a security fix.
- **medium**: a noticeable improvement to something that exists.
- **low**: small fixes and polish.

Rate honestly. When most entries are high the ranking stops helping anyone.

## Wording

- Write a headline that makes sense on its own. Aim for three to eight words and use the
  names staff see on screen.
- Give one concrete outcome or required action in one short sentence, around 20 words. Use
  page help for instructions and exceptions. Put migration and upgrade steps under
  `## Technical notes`, which readers can expand.
- What's new shows up to three highlights per month. Unread entries come first, then entries
  marked `highlight: true`. Impact and date break ties. Mark a handful of everyday workflow
  changes that deserve that space. The full list includes every entry.
- Everything is public: no other products or vendors, no customer or real names, synthetic data
  only, no compliance or accreditation claims, nothing about agents or how the work was done.

## Regenerate and check

```sh
uv run python manage.py changelog          # writes CHANGELOG.md from changelog.d/
uv run python manage.py changelog --check  # fails when an entry is invalid or CHANGELOG.md is stale
```

Commit the entry and the regenerated `CHANGELOG.md` together. CI runs the check in the
`application` job, and the `changelog` workflow fails a pull request that changes application
source without adding a file under `changelog.d/`, unless it carries the `no-changelog` label.
`tests/test_changelog.py` covers parsing, ordering and the page.

## A branch that edited CHANGELOG.md by hand

`CHANGELOG.md` is output. If a branch (yours or one you are rebasing) edited it directly:

1. Turn every hand-written bullet into an entry file as above, keeping its technical detail
   under `## Technical notes`. Use the date the change merged, or today for unmerged work.
2. Take the other side's `CHANGELOG.md` in the conflict (or discard your edits to it), then run
   `uv run python manage.py changelog` to regenerate it from all entries.
3. Run the check before committing.
