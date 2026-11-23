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

`AUTH_PASSWORD_VALIDATORS` is now `accounts.passwords.LengthByFactorValidator`, Django's `UserAttributeSimilarityValidator` and `CommonPasswordValidator`, and `accounts.passwords.ExpectedPasswordValidator`; Django's `MinimumLengthValidator` and `NumericPasswordValidator` are no longer used, so no rule asks for particular kinds of character. Existing passwords keep working and meet the rule when next changed. `ACCOUNT_FORMS` points allauth's change, set and reset-from-link password forms at `accounts.forms`, which state the rule for their account.
