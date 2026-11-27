---
date: 2026-11-27
type: added
impact: medium
title: See what an account held when it was deactivated
area: Accounts & roles
link: admin:index
---
Deactivate an account with a reason after seeing what it holds; its page then keeps what it held, who deactivated it and when.

## Technical notes

Migration `accounts.0012_deactivation` adds the table `accounts_deactivation`, with a trigger that refuses `UPDATE` and `DELETE` on it. It changes no data: no record is written for an account deactivated before it, and such an account's page says that what it held then was not recorded. From this release, every save of an account that deactivates it (the new Deactivate page, clearing Active on the account's page, or code that saves the account) writes one record in the same transaction; a change made with `QuerySet.update` or raw SQL writes none. The Deactivate page (`admin/accounts/user/<id>/deactivate/`, route `admin:accounts_user_deactivate`) is for active superusers with staff status, asks them to confirm it is them, and records the change event `account.updated` with the reason. Reversing the migration drops the table and its records. No new setting or environment variable.
