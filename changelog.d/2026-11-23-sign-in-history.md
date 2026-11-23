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

Migration `accounts.0009_sign_in_history` adds the table `accounts_signinevent`, the column `accounts_accountsession.factor` (blank by database default, so a release still running during the upgrade keeps signing people in; its sign-ins show the factor as not recorded) and triggers: the database writes each session's sign-in and sign-out to the history, and refuses `UPDATE` and `DELETE` on it, so entries are append-only. Failed passwords, codes and lockouts are recorded only for the one account the username or email address names (refusals by the limit at most once a minute for each account, step and address), and a refused sign-in for the account whose password or code was accepted; no entry holds a password, a code or a typed identifier. A sign-in completing after its account was deactivated now starts its session ended. History starts at the upgrade, entries are kept with no retention period, and an account with history cannot be deleted. Reversing the migration deletes the history. The address follows `ALLAUTH_TRUSTED_PROXY_COUNT` or `ALLAUTH_TRUSTED_CLIENT_IP_HEADER`, as for sessions. No new setting; the sign-in log (`lab_ops.signin`) is unchanged.
