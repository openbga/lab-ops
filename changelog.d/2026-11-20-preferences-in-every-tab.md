---
date: 2026-11-20
type: fixed
impact: low
title: Saved layouts and What's new visits show in every tab
---
A column layout, page size, sidebar choice or What's new visit made in one tab or workstation shows on the next page in the others.

## Technical notes

No migration. Each request reads the person's preferences from the database once; nothing is copied into the session any more, so two tabs answering at the same moment can no longer put back an older value, such as a What's new mark. A list's page size and sort, and the What's new mark, are combined with the value stored when saving, so another tab's choice is kept; a list saves only what it showed, so an old link's sort or a lookup saves nothing. A display setting the server refuses shows a notice. Sessions signed in earlier keep an unused copy until they end.

Adding a preference counts the person's preferences and inserts holding a transaction-level advisory lock (keys 19557 and the account's id), so the limit of 300 holds when two are added at once. A list page opened at the limit with a page size or sort in its address now follows it without saving it, instead of failing with a server error.

While a deploy replaces processes one by one, processes of the earlier release still read and write their session copy and add preferences without the lock, so until every process runs this release a page they serve can show an older value and the limit can be passed by one.

Changing a display preference no longer saves the session, so it no longer extends its length (Security settings, Session length).
