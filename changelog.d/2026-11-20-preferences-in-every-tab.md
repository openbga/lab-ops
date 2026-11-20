---
date: 2026-11-20
type: fixed
impact: low
title: Saved layouts and What's new visits show in every tab
---
A column layout, page size, sidebar choice or What's new visit made in one tab or workstation shows on the next page in the others.

## Technical notes

No migration. Each request reads the person's preferences from the database once; nothing is copied into the session any more, so two tabs answering at the same moment can no longer put back an older value, such as a What's new mark. Sessions signed in earlier keep an unused copy until they end.

Adding a preference counts the person's preferences and inserts holding a transaction-level advisory lock (keys 19557 and the account's id), so the limit of 300 holds when two are added at once. A list page opened at the limit with a page size or sort in its address now follows it without saving it, instead of failing with a server error.

Opening pages and changing a display preference no longer save the session, so they no longer extend its length (Security settings, Session length).
