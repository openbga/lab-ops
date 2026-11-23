---
date: 2026-11-23
type: added
impact: high
title: See your sign-in history
link: account-sign-ins
highlight: true
---
Sign-in history, in the account menu, lists your sign-ins, sign-outs and failed or refused attempts of the last 90 days, so you can spot any you do not recognize.

## Technical notes

Migration `accounts.0009_sign_in_history` adds the table `accounts_signinevent`, the column `accounts_accountsession.factor` (blank by database default, so a release still running during the upgrade keeps signing people in; its sign-ins show the factor as not recorded) and triggers: the database writes each session's sign-in and sign-out to the history, and refuses `UPDATE` and `DELETE` on it, so entries are append-only. At sign-in, a wrong password, or one the failed sign-in limit refused, is recorded only for the one account the username or email address names; on re-authentication or Change password, for the signed-in account; a wrong code, and a refusal at the code step, for the account already identified at that step; a refused sign-in for the account whose password or code was accepted; refusals by the limit at most once a minute for each account, step and address; no entry holds a password, a code or a typed identifier. A sign-in completing after its account was deactivated now starts its session ended. History starts at the upgrade, entries are kept with no retention period, also when an account is deleted by means other than the application (which deactivates accounts). Reversing the migration deletes the history. The address follows `ALLAUTH_TRUSTED_PROXY_COUNT` or `ALLAUTH_TRUSTED_CLIENT_IP_HEADER`, as for sessions. No new setting. The sign-in log (`lab_ops.signin`) gains a `signout.deactivated` line for a sign-in whose session starts ended because its account was deactivated while it was under way.
