---
date: 2026-11-24
type: added
impact: high
title: Review access and audit records across the laboratory
area: Audit review
link: audit_review:changes
highlight: true
---
Holders of the new capability "Can review access and audit records" filter and export every change, access record and sign-in in Audit review; each use is recorded.

## Technical notes

New capability `accounts.review_audit_records` ("Can review access and audit records"), granted to nobody; a superuser grants it in the admin console to a person or a role group, and it takes effect for a staff account with a laboratory role. Migrations: `accounts.0010_review_capability` (the capability and an index on `accounts_signinevent (occurred_at DESC)`), `core.0010_recent_history_indexes` (indexes on `core_auditevent` and `core_accessrecord (occurred_at DESC)`) and `audit_review.0001_initial` (the table `audit_review_workspaceuse`, with check constraints and a trigger that refuses `UPDATE` and `DELETE`). While each index is built, writes to its table wait: seconds for the rows a laboratory keeps; plan the upgrade outside busy hours if those tables hold millions of rows. A workspace use row is written for each page of the workspace shown and each export, with the account id, the list, the filters accepted (ids, codes and dates), the rows matched and the request ID, before the page or file is sent; if it cannot be written, the request fails with a server error. Exports are CSV files of at most 10,000 rows, built in memory. No new setting or environment variable. New routes under `audit-review/` (`changes/`, `access/`, `sign-ins/`, `uses/` and an `export/` of each) and the remote option list `lookups/review-accounts/`. The public demonstration grants the capability to `demo_supervisor` from its next daily reset. Reversing the migrations deletes the workspace use records, the three indexes and the capability with its grants to people and groups.
