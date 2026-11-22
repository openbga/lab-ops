---
date: 2026-11-22
type: added
impact: high
title: Get back what you were typing after a sign-out
highlight: true
---
When a session limit signs you out, what you were typing in a ticket's reply or note, a portal reply or a new portal ticket comes back when you sign in again in the same browser.

## Technical notes

Migration `accounts.0005_kept_draft` adds the table of kept typing and one row per account for the moment it last used Sign out. A copy is stored only after its session ended by a limit, for that account and the browser that sent it, identified by the SHA-256 of a random value in a new HttpOnly cookie, `lab_ops_kept` (Secure as the session cookie is, SameSite=Lax, 24 hours, set with each page that draws a kept form); files and fields that hold patient information are never kept. It is drawn back into the form on the server, given back once, discarded at Sign out, never logged or audited, and removed 24 hours after the sign-out by a new scheduled job, `remove_kept_typing`; without a scheduler, copies are no longer given back after 24 hours but stay in the database until a run removes it. A Sign out handled by the previous release while this one rolls out does not remove copies, but they are not given back afterwards. The page sends the copy to `account/session/keep/`, which takes no CSRF token: a token signed for the form's account and session stands in for it. A GET of the address a refused reply is drawn at (`/requests/<id>/entries/`, `/portal/requests/<number>/reply/`) now opens the ticket instead of answering 405.
