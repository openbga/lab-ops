---
date: 2026-11-27
type: added
impact: high
title: Reveal patient information on one ticket in an emergency
area: Tickets
link: desk:request-list
---
Staff you give Can use emergency access can, with a reason, reveal one ticket's patient information for 60 minutes; each use waits for a review by someone else.

## Technical notes

Migration `accounts.0012_emergency_access_capability` adds the capability "Can use emergency access" (`use_emergency_access`), granted to nobody, so the feature is off until a superuser gives it to a person or a role group; reversing it deletes the permission with its grants. Migration `desk.0008_emergency_access` adds the tables `desk_emergencyaccess` (one row for each use) and `desk_emergencyreview` (its review), with triggers that refuse `UPDATE` and `DELETE` on both (core's `prevent_record_mutation`) and refuse a review by the person who used the access or of a use whose end has not passed by `transaction_timestamp()`; reversing it is refused once a use is recorded. Migration `audit_review.0006_emergency_access_uses` allows the source `emergency` ("Access review: emergency access") in `audit_review_workspaceuse` (its check constraint is dropped and recreated). None of the three rewrites an existing row. A use lasts 60 minutes, fixed in this release (`desk.emergency.MINUTES`); no new setting or environment variable. Starting a use records the change event `emergency_access.started` (the ticket, the reason code and the times, never the explanation) and a notice under the new outbox topic `desk.emergency` to each supervisor and holder of "Can review access and audit records" with an email address, sent whatever their notice preference; reviewing records `emergency_access.reviewed` (the outcome, never the note). A reveal made under a use adds `emergency` (the use's id) to its `request_form.value_revealed` or `external_link.person_revealed` event. New routes `requests/<id>/emergency/`, `access-review/emergency/` and `access-review/emergency/<id>/`. At its next reset the demonstration seed gives `demo_client_services` the capability, recorded as `account.capability_granted`; the demonstration keeps email off, so its notices are recorded as not sent.
