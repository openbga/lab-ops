---
date: 2026-11-26
type: added
impact: high
title: Record a signed access review that keeps or removes each grant
area: Access review
link: access_review:reviews
---
On the new Reviews tab of Access review, decide Keep or Remove for every grant the staff or portal accounts hold, sign with your password, and see which removals are still held.

## Technical notes

Migration `audit_review.0005_access_reviews` adds the tables `audit_review_accessreview` and `audit_review_accessreviewitem`, with triggers that refuse `UPDATE` and `DELETE` on both (core's `prevent_record_mutation`), and allows the source `grant_review` ("Access review: reviews") in `audit_review_workspaceuse` (its check constraint is dropped and recreated; no row is rewritten). Reversing it is refused while any access review or a use of the Reviews tab is recorded. Each signing records the change event `access_review.signed`. New routes `access-review/reviews/`, `access-review/reviews/export/`, `access-review/reviews/new/`, `access-review/reviews/<id>/` and `access-review/reviews/<id>/export/`, for staff with a laboratory role who hold "Can review access and audit records". At its next reset the demonstration seed signs one access review of the staff accounts, dated three days before, by `demo_supervisor`, with every grant kept except the Technical staff role of `former_technician`, which is decided for removal and still held. No new setting or environment variable.
