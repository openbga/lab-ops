---
date: 2026-11-21
type: security
impact: high
title: Sign out after a period without activity
area: Security settings
link: security-settings
highlight: true
---
Sessions are signed out after a period without activity and after a longest session, which administrators set on Security settings for staff and for client portal accounts. Pages warn before signing out and offer Stay signed in, and signing in again returns to the page. Deactivating an account or setting a new password ends its other sessions at once, and pages are no longer kept by the browser.

## Technical notes

Migrations `accounts.0003_account_session` and `core.0005_sessionlimit` add a table of sessions (start, last activity, end and why) and a table of session limits. Neither writes rows: limits never saved are 30 minutes without activity and 12 hours at most, set from 5 to 60 minutes and 1 to 24 hours. After the upgrade, everyone signed in is asked to sign in once more, because sessions from earlier releases carry no session record. `SESSION_COOKIE_AGE` is now 24 hours, the longest a session can last. The new `accounts.sessions.SessionLimitMiddleware` follows the message middleware and marks every answer to a signed-in session `Cache-Control: no-store`; the laboratory's logo keeps its caching. The sign-in log (`lab_ops.signin`) adds `signout.inactivity`, `signout.maximum`, `signout.deactivated` and `signout.new_password`.
