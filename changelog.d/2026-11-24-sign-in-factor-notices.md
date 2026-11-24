---
date: 2026-11-24
type: added
impact: medium
title: Email notices when your sign-in factors change
link: mfa_index
---
You are emailed when your authenticator app or recovery codes change, or a superuser marks your password as compromised, so you notice a change you did not make.

## Technical notes

No migration and no new setting. The notices go through the existing outbox under the topic `accounts.security` and are retried by `run_scheduled_jobs`; with email off, as on the public demonstration, they are recorded as Not sent. They are sent when a superuser marks a password as compromised or removes an authenticator app or recovery codes in the admin console, and when the person adds an authenticator app (one notice, which also says new recovery codes were issued), generates new recovery codes or removes the app on their own two-factor pages. Each names the laboratory, the username, what changed and when (UTC), and the laboratory's security contact from its profile (or "your laboratory's administrator"), with no code, secret, network address, browser or ticket content, and links only to the site's address. They are sent whatever the person chose on Email notices, and not when the account is inactive or has no email address when sent. A password changed by the person or set by a superuser, and an email address change, send no notice. allauth's three two-factor pages that change factors (`mfa_activate_totp`, `mfa_generate_recovery_codes`, `mfa_deactivate_totp`) are now served by `accounts.mfa_views` at the same paths and URL names, included before allauth's URLs; they run allauth's own flow in one transaction that locks the account and also records `account.authenticator_added`, `account.recovery_codes_replaced` or `account.authenticator_removed` audit events with factor ids, types and times only. allauth's own `ACCOUNT_EMAIL_NOTIFICATIONS` is left at its default, off. Scheduled jobs lists a failed or retrying one as an Account notice without saying whose account it is.
