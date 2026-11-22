---
date: 2026-11-22
type: added
impact: high
title: Get back what you were typing after a sign-out
highlight: true
---
When a session limit signs you out, what you were typing in a ticket's reply or note, a portal reply or a new portal ticket comes back when you sign in again in the same browser.

## Technical notes

Migration `accounts.0005_kept_draft` adds the table of kept typing. A copy is stored only after its session ended by a limit, for that account and the browser that sent it, identified by the SHA-256 of a random value in a new HttpOnly cookie, `lab_ops_kept` (Secure as the session cookie is, SameSite=Lax, 24 hours); files and fields that hold patient information are never kept. It is given back once, discarded at Sign out, never logged or audited, and removed 24 hours after the sign-out by a new scheduled job, `remove_kept_typing`; without a scheduler, copies are no longer given back after 24 hours but stay in the database until a run removes them. The page sends the copy to `account/session/keep/`, which takes no CSRF token: a token signed for the form's account and session stands in for it.
