---
date: 2026-11-25
type: added
impact: medium
title: See every account's roles and capabilities with who gave them, and list accounts nobody has used
area: Access review
link: access_review:accounts
---
The new Access review, under Oversight, lists every account with its roles, capabilities, teams, portal access, second factor and last sign-in, and who gave each role, capability and admin console access and when. A second tab lists the active accounts with no sign-in for a period the laboratory sets.

## Technical notes

Migration `audit_review.0004_access_report` adds the table `audit_review_unusedperiod`, which holds the unused account period in one row written the first time it is saved (none ships), and allows two more sources, `accounts` and `unused`, in `audit_review_workspaceuse` (its check constraint is dropped and recreated; no row is rewritten). Reversing it fails once a use of Access review is recorded, since workspace use rows cannot be changed or deleted. New routes `access-review/accounts/`, `access-review/accounts/export/`, `access-review/unused/`, `access-review/unused/export/` and `access-review/unused/period/`, for staff with a laboratory role who hold "Can review access and audit records". At its next reset the demonstration seed sets the unused account period to 90 days, adds the synthetic staff account `former_technician`, which has no password and last signed in 120 days before, and records a change event naming the role of each demonstration staff account it creates. Two saves of a role group's page at once, or a role group renamed while an account naming it is saved, now record each grant once and under the name the group had when it was recorded: the role group page holds the group's row before it reads the values it records, and the account page reads the role names as it records the change. No new setting or environment variable.
