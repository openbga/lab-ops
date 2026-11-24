---
date: 2026-11-24
type: added
impact: high
title: See who opened a ticket
area: Tickets
highlight: true
---
Opening a ticket, its form details or complaint forms, downloading a file and being shown a patient-marked answer are now recorded; supervisors read them under More, Who opened it.

## Technical notes

Migration `core.0009_access_records` adds the table `core_accessrecord`, with check constraints and a trigger that refuses `UPDATE` and `DELETE`, and the column `core_auditevent.request_ref` (blank by database default, so a release still running during the upgrade keeps recording change events; events from before the upgrade, and from scheduled jobs and commands, have no request ID). An access record is written for each staff or client portal ticket page drawn (also after a refused reply, note or contact), audit history, Who opened it, Form details and complaint form page, file download, staff reveal of a patient-marked answer or a person-level key (besides its change event), patient-marked answer shown on a portal ticket page, new ticket opened with Request again (for the ticket its answers come from) and ticket whose entries a client organization's timeline page shows. It holds the account id, the capacity (laboratory staff or client portal), the kind, the ticket id, the file, answer or link id and the request ID that the `lab_ops.request` log line and the `X-Request-ID` header carry; never a subject, text, answer, file name, key or content. A refused read, a HEAD request and an answer another layer put in place of the page record nothing; if the record cannot be written, the request fails with a server error and the page is not sent. A process of the new release records from the moment the migration has applied; records are complete once no process of the earlier release serves requests. They are kept with no retention period and grow by about one row per page view, plus one per patient-marked answer on a portal ticket page and one per ticket shown on a client timeline page (up to 25). Lists and searches are not recorded. Reversing the migration deletes the records and the change events' request IDs. New setting value: `DEFAULT_EXCEPTION_REPORTER_FILTER` is `core.request_logging.NoPostedValuesFilter`, so Django's error reports show no posted value. No new environment variable. New route: `requests/<id>/access/` (supervisors).
