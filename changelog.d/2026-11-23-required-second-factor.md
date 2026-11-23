---
date: 2026-11-23
type: added
impact: high
title: Require a second sign-in factor
area: Security settings
link: security-settings
highlight: true
---
Administrators choose which accounts must use an authenticator app; those accounts set one up at their next sign-in, before anything else.

## Technical notes

Migration `core.0006_second_factor_rule` adds the rule's table (one row, written the first time an administrator saves it, so nothing is required until then); `accounts.0007_accountsession_second_factor_required` adds a column with a database default to each session, recording whether the rule covered the account at sign-in. A new middleware, `accounts.enrolment.EnrolmentMiddleware`, follows `accounts.sessions.SessionLimitMiddleware`: a session the rule covered whose account has no authenticator app reaches only `/account/second-factor/`, the two-factor setup and re-authentication pages, sign-in, Sign out and the session status addresses. Sessions the rule did not cover at sign-in are not interrupted when it changes. The account and the rule are read together in one statement. `MFA_ALLOW_UNVERIFIED_EMAIL` is now on: sign-up is closed, and allauth's check against unverified addresses would leave a required account without outgoing email unable to add an authenticator.
