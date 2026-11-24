---
date: 2026-11-24
type: added
impact: high
title: See who opened a ticket
area: Tickets
highlight: true
---
Opening a ticket or its form details, downloading a file and being shown a patient-marked answer are now recorded; supervisors read them under More, Who opened it.

## Technical notes

Migration `core.0009_access_records` adds the table `core_accessrecord`, with a trigger that refuses `UPDATE` and `DELETE`, and the column `core_auditevent.request_ref` (blank by database default, so a release still running during the upgrade keeps recording change events; events from before the upgrade, and from scheduled jobs and commands, have no request ID). An access record is written for each staff or client portal ticket page drawn (also after a refused reply, note or contact), audit history, Who opened it and Form details page, file download, staff reveal of a patient-marked answer or a person-level key (besides its change event), patient-marked answer shown on a portal ticket page, and ticket whose entries a client organization's timeline page shows. It holds the account id, the capacity (laboratory staff or client portal), the kind, the ticket id, the file or answer id and the request ID that the `lab_ops.request` log line and the `X-Request-ID` header carry; never a subject, text, answer, file name or content. A refused read records nothing; if the record cannot be written, the request fails with a server error and the page is not sent. Records start once the new release serves every request, are kept with no retention period and grow by about one row per page view. Lists and searches are not recorded. Reversing the migration deletes the records and the change events' request IDs. No new setting. New route: `requests/<id>/access/` (supervisors).
