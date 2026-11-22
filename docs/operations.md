# OpenBGA Lab Ops operations

Use the [installation guide](../README.md) to set up the application. The procedures below
cover recovery, access controls and the optional public demonstration.

## What is protected

PostgreSQL holds the application state, including accounts and MFA credentials, client
records, tickets and conversations, form answers, attachment bytes, lab logos and audit
events. Back up the entire database. The container filesystem is disposable.

Keep the database backup encrypted and restrict access. It contains the authentication
secrets and recovery codes stored by django-allauth. Back up the deployment configuration
and secret keys separately in a secret manager. Reusing the deployment's secret key on
recovery preserves signed sessions, tokens and the seal used by a demonstration database.

The database enforces restrictions on updating and deleting history. A database owner or
superuser can change the schema or disable triggers. Restrict database administration and
keep separately protected backups.

## Decisions the operator must make

Set recovery time and recovery point objectives, backup retention, an encrypted off-provider
destination and a restore-test schedule. Check that the provider's plan meets those
objectives. Give each person a separate account so that audit events and independent
complaint reviews identify the people involved.

## Take a backup

Use PostgreSQL 17 client tools. Configure connection details using PostgreSQL service
definitions and a protected password file. The examples use service names `lab_ops_source`
and `lab_ops_restore`. Give the second service its own empty database on an isolated
instance. Confirm the database and host before running a restore.

```sh
pg_dump --dbname=service=lab_ops_source --format=directory --jobs=4 --no-owner --file=lab-ops.dump
```

Encrypt and copy the backup to its destination. Record the time, application revision and
database version. To compare source and restored counts, suspend writes while taking the
dump and recording the source counts. Queries issued separately from the dump can otherwise
see a different snapshot.

## Restore into an isolated database

```sh
pg_restore --exit-on-error --no-owner --no-privileges --jobs=4 \
  --dbname=service=lab_ops_restore lab-ops.dump
```

Use a role with permission to create the objects in the empty database. Grant the application
role its usual privileges before testing. A provider's point-in-time recovery can also
supply an isolated restored database.

## Verify the restore

1. Start the same application revision with its database connection pointed at the restore.
   Keep outbound mail disabled. Run `python manage.py migrate --check`.
2. Compare these counts with the source summary taken while writes were suspended. An
   installation that captures additional business records can include their tables too.

   ```sql
   select 'accounts', count(*) from accounts_user
   union all select 'organizations', count(*) from clients_client
   union all select 'tickets', count(*) from desk_servicerequest
   union all select 'entries', count(*) from desk_requestentry
   union all select 'attachments', count(*) from attachments_attachment
   union all select 'audit events', count(*) from core_auditevent;
   ```
3. Check stored files and logos. Both queries should return zero.

   ```sql
   select count(*) from attachments_attachment a
   left join attachments_attachmentblob b on b.attachment_id = a.id
   where b.data is null or octet_length(b.data) <> a.size
      or encode(sha256(b.data), 'hex') <> a.sha256;
   select count(*) from core_lablogo where encode(sha256(image), 'hex') <> sha256;
   ```
4. Compare non-internal triggers against the original database and verify the restore
   serves `/ready/`. Sign in, open a known ticket and download an attachment using an
   account entitled to read it. Record the result of the drill.

## Recover production

For a bad application release, check whether the previous revision works with the current
schema before redeploying it. Restoring an older database discards newer writes.

For data loss, stop writes and retain the damaged database. Restore to a separate database,
verify it and reconcile records created since the backup with the laboratory. Switch the
application to the verified database. Keep the old copy for the laboratory's recovery and
retention requirements.

## Security settings

Setup > Security settings shows administrators the values this deployment runs with, among them
the session limits, the failed sign-in limit, `SECURE_HSTS_SECONDS`, `CONTENT_SECURITY_POLICY` and
whether email sign-in codes are available. The session limits are set on that page; deployment
settings are changed on the host, not on that page. The page compares the configuration files as they were when the application started, so
restart the application after changing them, and do not replace them while it starts or runs.
Bytecode compiled in unchecked-hash mode is not compared with its source, so do not ship the
configuration that way. The [safeguards reference](safeguards.md) separates what the application does from what
stays with the laboratory and its host.

## Sign-in limits behind a proxy

Failed sign-ins, password resets and sign-in code requests are rate limited. By default the
address is the connecting peer. If a proxy serves everyone, requests can share one
per-address limit. Set `ALLAUTH_TRUSTED_PROXY_COUNT` to the number of trusted proxies
appending to `X-Forwarded-For`, or set `ALLAUTH_TRUSTED_CLIENT_IP_HEADER` to a header
your platform overwrites with the visitor's address. Verify the selected value from a known
client. Make sure visitors cannot supply that value themselves. With the default per-process
cache, each worker maintains separate counters. The same setting chooses the address recorded for
each session at sign-in and shown on Where you are signed in; without it, every session shows the
proxy's address.

## Browser security headers

The default Content Security Policy restricts resources to the application's origin.
The Permissions Policy disables camera, microphone, geolocation, payment and USB access.
`CONTENT_SECURITY_POLICY` replaces the entire policy. Review any replacement before deployment.

## Request size

The application rejects declared bodies larger than `MAX_REQUEST_BODY_BYTES` (12 MiB
by default). Configure a body-size limit at the reverse proxy to cover requests without
a declared length. Attachment type, count and file-size checks also apply in the application.

## HTTPS policy

With `DEBUG=false`, HTTP redirects to HTTPS and cookies are secure. The default
`SECURE_HSTS_SECONDS` is one hour. Increase it once every applicable host works over HTTPS.
Enable `SECURE_HSTS_INCLUDE_SUBDOMAINS` only if all subdomains support HTTPS.

## Email

Production mail is disabled until configured. Set the SMTP values in [.env.example](../.env.example)
and verify delivery with a test account. Store credentials in the host's secret settings.
Set `PUBLIC_URL` to the external HTTPS origin so sign-in links reach the correct deployment.

Ticket notices include the number, a fixed event description, the laboratory name and a
sign-in link. Each is recorded with the change and sent after the change commits. An SMTP
failure leaves the ticket change in place: the notice waits and the scheduled jobs retry it. The
`lab_ops.notifications` logger records each failed attempt with the notice id, the ticket number,
the recipient's account id and the error's class, never an address or the server's message.
Administrators see failed notices and those waiting to be retried on the Scheduled jobs page.
With `EMAIL_BACKEND` set to the dummy backend, notices are recorded as not sent and are never sent
later, even after mail is configured. `EMAIL_TIMEOUT` (10 seconds by default) bounds how long a
send waits for the mail server. The attempt right after a change stops at the first notice the
server does not take, and once one timeout has been spent, so a slow server delays a save (a bulk
action over many tickets included) by about one timeout, and the rest wait for the scheduled
jobs. A run of `send_notices` stops after three failed sends in a row and leaves the
rest for the next run.

## Scheduled jobs

Run `python manage.py run_scheduled_jobs` every five minutes from the host's scheduler, with the
same image, settings and database as the web service. One run retries waiting notices, sends
service target notices and closes resolved tickets after the period an administrator sets (none
by default). Without it, notices are still sent once right after each change, but nothing else
happens.

- Overlap is safe: a run that starts while another holds the lock prints that nothing was done and
  exits normally.
- A failed job exits with an error status, so the scheduler can alert. The `lab_ops.jobs` logger
  records each job's counts and any error's class and frames.
- The Scheduled jobs page (Setup) shows each job's last run and warns when no run started in the
  last 30 minutes.
- Use `--job <name>` to run one job by hand, for example `--job send_notices`.

On Render, a cron job with the web service's image works: schedule `*/5 * * * *`, command
`python manage.py run_scheduled_jobs`, and the web service's `DATABASE_URL`, `SECRET_KEY`,
`TIME_ZONE` and mail settings. It is a separate paid service that `render.yaml` does not create.
Deploy it from the same release as the web service.

Upgrade note: clocks already past their target when this release is installed are marked as
noticed by migration `service_levels.0003`, so the first run does not notify old breaches.

## Logs

`lab_ops.request` records the generated request ID, HTTP method, matched route, status
and duration. Use the `X-Request-ID` response header to correlate an error.
`lab_ops.signin` records sign-ins, failures, explicit sign-outs and sessions ended by the session
limits (`signout.inactivity`, `signout.maximum`), a deactivation (`signout.deactivated`), a new
password (`signout.new_password`), the person from another of their sessions
(`signout.signed_out_elsewhere`) or a superuser (`signout.administrator`). Treat usernames as
personal data. Set access and retention limits for platform and proxy logs too.
Keep credentials, bodies, URLs carrying tokens and ticket contents out of operational logs.

## Software bill of materials

Each release published on GitHub carries a software bill of materials (SBOM) of the container
image, in SPDX and CycloneDX formats, and each run of the security checks keeps one for 90 days
([What is checked](../SECURITY.md#what-is-checked)). It lists the operating-system packages,
Python, and the Python and browser packages in the image. An image built later, by you or by a
host that builds from the Dockerfile, can contain newer operating-system packages from the
updated base image. To describe what you run, generate an SBOM from your own image and scan it,
from a checkout of the release you deploy (its `.syft.yaml` and `.grype.yaml` apply):

```sh
mkdir -p build
syft scan docker:lab-ops:local --config .syft.yaml --output cyclonedx-json@1.6=build/lab-ops.cdx.json
grype sbom:build/lab-ops.cdx.json --config .grype.yaml
```

The project's checks cover its source and image, not your host, configuration or network.

## Public demonstration

Use a dedicated deployment with synthetic data. To display shared sign-in credentials,
run `seed_demo --announce-accounts` with a public `DEMO_PASSWORD` and a different,
private `DEMO_ADMIN_PASSWORD`. The seed checks that announced accounts have the expected
permissions, organization and site access. It publishes credentials only when it can verify
that the demonstration was initialized from an empty database.

For a scheduled reset, set `ALLOW_DEMO_RESET=true` **only on the demonstration reset job**.
The reset erases application records, including sessions and audit history, and reseeds in
one transaction. It requires an empty or sealed demonstration database and matching
migrations. A laboratory initialized with `setup_lab` is ineligible.

`render.demo.yaml` defines an optional paid cron job for an existing demonstration web
service and database. The job shares their source revision, database connection and signing
key. Set both demo passwords when configuring it. Its default schedule is 08:00 UTC daily.
Change the schedule and the public reset notice together. A regular deployment uses
`render.yaml`; its deploy button creates no reset job.

Deploy each release to the web service and reset job together. If using automatic deploys,
point both services at the same branch and enable deployment after CI passes. A web deploy
applies migrations. Seed changes take effect on the next demonstration reset.
