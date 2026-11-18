---
date: 2026-11-18
type: added
impact: high
title: Retry email notices and run scheduled jobs
area: Scheduled jobs
link: jobs:overview
highlight: true
---
Notices the mail server refuses are retried. Teams hear when a ticket passes a service target, and resolved tickets can close after a set number of days.

## Technical notes

Run `python manage.py run_scheduled_jobs` every five minutes from the host's scheduler, with the web service's image, database and mail settings (see docs/operations.md, "Scheduled jobs"). Without it, notices are still sent once after each change, but nothing is retried or closed automatically. Migrations add the `jobs` app's tables and two columns to ticket clocks; clocks already past their target at upgrade are marked as noticed so the first run does not notify old breaches. Notices are now recorded before sending: with the dummy mail backend they are recorded as not sent and never sent later. New setting `EMAIL_TIMEOUT` (seconds, default 10) bounds each wait for the mail server.
