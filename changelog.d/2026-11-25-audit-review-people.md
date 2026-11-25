---
date: 2026-11-25
type: added
impact: medium
title: Count what each person did, and what happened outside business hours, in Audit review
area: Audit review
link: audit_review:people
---
The new People tab of Audit review counts, for each person over a period, their sign-ins, failed or refused sign-ins, tickets opened, files downloaded, patient information shown and changes made, and how many of these happened outside the hours of one of your business calendars. Select a count to open the rows behind it. Changes, Access records and Sign-ins can now be narrowed to what happened outside a business calendar's hours.

## Technical notes

Migration `audit_review.0002_people_summary` allows a fifth source, `people`, in `audit_review_workspaceuse` (its check constraint is dropped and recreated in one transaction; no row is rewritten). Reversing it fails once a use of the People tab is recorded, since workspace use rows cannot be changed or deleted. New routes `audit-review/people/` and `audit-review/people/export/`. Business hours come from the active business calendars the service clocks use, read as they stand when a page is drawn; a period judged against one covers at most 366 days. No new setting or environment variable.
