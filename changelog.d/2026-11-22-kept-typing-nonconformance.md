---
date: 2026-11-22
type: added
impact: medium
area: Quality
link: nonconformance:list
title: Get back nonconformance typing after a sign-out
---
When a session limit signs you out while you work a nonconformance or its actions, what you typed comes back on that form when you sign in again. If the record changed meanwhile, saving is refused.

## Technical notes

Migration `accounts.0006_kept_draft_revision` adds a nullable `revision` column to the kept-typing table; a release still running while it applies goes on inserting rows without it. Each nonconformance form keeps its copy under a key naming the form and the record, with the revision the form posts: the page that gives it back posts that revision, so a save is checked against the record as it stood when the person typed. A form page opened with GET is drawn in one reading and leaves the copy in place when it is not drawn after all.
