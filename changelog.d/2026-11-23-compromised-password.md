---
date: 2026-11-23
type: added
impact: high
title: Mark a password as compromised, so the person must choose a new one
area: Security settings
link: security-settings
---
A superuser marks a password someone else may know as compromised from the account's page in the admin console, with a reason: the account's sessions end, the password alone no longer signs in, and a sign-in that also gives an authenticator code or an emailed code opens only Choose a new password.

## Technical notes

Migration `accounts.0008_compromised_password` adds the `CompromisedPassword` table (one open mark per account, kept after it is lifted, with a copy of the marked password's stored hash) and the `password_compromised` session ending. `MIDDLEWARE` gains `accounts.compromised.PasswordChangeMiddleware` after `accounts.enrolment.EnrolmentMiddleware`, and `AUTH_PASSWORD_VALIDATORS` gains `accounts.passwords.MarkedPasswordValidator`, which refuses a password marked for the account. Any new password lifts the mark: on Choose a new password (`/account/new-password/`), through an emailed reset link, or set on the account's password page in the admin console; a hash Django upgrades at sign-in does not. A sign-in that proves only the password starts its session ended. Nothing is emailed to the person. Marking is recorded as `account.password_marked_compromised` and the new password chosen on that page as `account.password_changed`, without the password or its hash.
