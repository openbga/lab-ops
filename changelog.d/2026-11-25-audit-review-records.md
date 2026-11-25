---
date: 2026-11-25
type: added
impact: high
title: Record signed reviews of the activity records, and see when the next review is due
area: Audit review
link: audit_review:reviews
---
The new Reviews tab of Audit review keeps a record of each review: the period, what was examined, the findings and follow-up, signed with your password and never changed. It shows when the next review is due at the interval you set and which days no review covers.

## Technical notes

Migration `audit_review.0003_review_records` adds the tables `audit_review_review` and `audit_review_reviewnote`, with triggers that refuse `UPDATE` and `DELETE` on both, and `audit_review_reviewschedule`, which holds the review interval in one row written the first time it is saved (none ships). It allows a sixth source, `reviews`, in `audit_review_workspaceuse` (its check constraint is dropped and recreated; no row is rewritten). Reversing it is refused while any review exists, and fails once a use of the Reviews tab is recorded, since workspace use rows cannot be changed or deleted. The Security settings page lists the review interval. A password asked for again, at signing, on any form that asks to confirm it is you or on the re-authentication page (whose form is now `accounts.forms.PasswordReauthenticateForm` in `ACCOUNT_FORMS`), is left out of an error report's request data and of the variables of this application's code that checks it, as are a sign-in's credentials in the account adapter; Django's authentication backend still receives the password as an argument its report filter does not hide (see the operations guide; this release sends no error report). New routes `audit-review/reviews/`, `audit-review/reviews/export/`, `audit-review/reviews/new/`, `audit-review/reviews/interval/`, `audit-review/reviews/<id>/` and `audit-review/reviews/<id>/note/`. No new setting or environment variable.
