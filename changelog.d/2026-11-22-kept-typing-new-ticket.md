---
date: 2026-11-22
type: added
impact: medium
area: Tickets
link: desk:request-create
title: Get back a new ticket you were typing after a sign-out
---
When a session limit signs you out while you log a ticket, what you typed comes back on New ticket when you sign in again in the same browser, with its category, organization, site, contact and ticket form. This works when you were typing in the dialog over the ticket list too.

## Technical notes

No migration. The new ticket keeps its own fields, the ticket form chosen and that form's fields as one copy under the form key `staff-ticket`, in the table and with the cookie added by the previous release. The organization, site and contact are kept by id and given back only while still active. Files and fields that hold patient information are never kept. When a copy is held for a form in the shared form dialog, signing in again returns to that form's own address instead of the page behind the dialog.
