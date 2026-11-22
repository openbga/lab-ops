---
date: 2026-11-21
type: security
impact: high
title: See where you are signed in and sign out elsewhere
link: account-sessions
highlight: true
---
Where you are signed in, in the account menu, lists your open sessions with their browser and address and signs out the ones you no longer use. A superuser can end a person's sessions from the admin console, with a reason.

## Technical notes

Migration `accounts.0004_session_browser_address` adds the browser (`User-Agent`, up to 300 characters, with a database default so a release still running during the upgrade keeps signing people in) and the client address at sign-in to each session; existing sessions show neither. The address follows `ALLAUTH_TRUSTED_PROXY_COUNT` or `ALLAUTH_TRUSTED_CLIENT_IP_HEADER`, as the sign-in limits do: behind a proxy without either, every session shows the proxy's address. The sign-in log (`lab_ops.signin`) adds `signout.signed_out_elsewhere` and `signout.administrator`, and each ending is an `account.sessions_ended` audit event without browsers or addresses. A session that has passed a session limit when it is ended, by any of these, a deactivation or a new password, is now recorded as ended by that limit, at the moment the limit passed.
