---
date: 2026-11-23
type: changed
impact: high
title: Longer passwords, checked against common and expected ones
area: Security settings
link: security-settings
highlight: true
---
New passwords need 15 characters, or 8 where a second sign-in factor is required; each password form says which, and common or predictable ones are refused.

## Technical notes

`AUTH_PASSWORD_VALIDATORS` is now `accounts.passwords.LengthByFactorValidator`, `accounts.passwords.AccountSimilarityValidator` (Django's similarity check against the account as it stands), Django's `CommonPasswordValidator` and `accounts.passwords.ExpectedPasswordValidator`; Django's `MinimumLengthValidator` and `NumericPasswordValidator` are no longer used, so no rule asks for particular kinds of character. Existing passwords keep working and meet the rule when next changed. `ACCOUNT_FORMS` points allauth's change, set and reset-from-link password forms at `accounts.forms`, which state the rule for their account. `seed_demo` now needs `DEMO_PASSWORD` and `DEMO_ADMIN_PASSWORD` of at least 15 characters (was 12) that meet the rule for each account it creates: set such ones before the next demonstration reset. `manage.py changepassword` now saves the password alone. `ACCOUNT_PASSWORD_RESET_TOKEN_GENERATOR` is `accounts.tokens.ResetTokenGenerator`: a reset link does not work while its account is a shared demonstration account; other links are unchanged.
