---
date: 2026-11-20
type: fixed
impact: medium
title: Saves check what changed while they waited
---
Saves now decide from records as they stand: an account page opened earlier cannot undo a newer change, and a portal reply refused on a closed ticket is shown again.

## Technical notes

No migration or setting. On the account administration page, Last login and Date joined are now read only, and a save writes only the fields the page edits. A password change, by the person or on the account's password page, saves only the password.
