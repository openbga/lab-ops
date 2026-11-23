# OpenBGA Lab Ops: free, open-source client service desk for laboratories

[![Checks](https://github.com/openbga/lab-ops/actions/workflows/checks.yml/badge.svg?branch=main)](https://github.com/openbga/lab-ops/actions/workflows/checks.yml)

OpenBGA Lab Ops is a free, open-source client service desk for US laboratories. Staff track
questions, complaints, pickups and supply orders from intake to resolution. Clients use a
portal to open tickets, reply to their laboratory and follow work for their organization or
sites. Each deployment serves one laboratory.

[Product website](https://openbga.github.io/lab-ops/) · [Try the public demo](https://openbga-lab-ops.onrender.com/) · [Deployment guide](#deploy-to-render) · [Roadmap](docs/roadmap.md)

## What you can do

| Area | Features |
| --- | --- |
| Client records | Manage organizations, sites, contacts and portal access. Preview CSV imports and see each client's ticket history. |
| Service desk | Route tickets to teams and staff. Record calls, send client-visible replies, attach files and keep internal notes. Filter work queues, save views and review complaints. |
| Service requests | Offer pickup, supply order and other forms through the portal. Configure required fields and orderable items. |
| Targets and references | Measure first response and resolution on the lab's business calendar, and tell the people responsible when a target passes. Link tickets to orders, samples and reports in existing systems. |
| Quality records | Optionally record nonconformances with containment, impact on reported results, root cause, actions, an effectiveness check and independent closing. Open one from a complaint. |
| Administration | Set up roles, account permissions, announcements and the lab's contact details. Review attributed changes in the audit history. Follow scheduled jobs and email notices that need attention, and close resolved tickets automatically after a set period. |

## Hosted demo

The public demo has example records for a reference laboratory. Its sign-in page lists shared
accounts and a password. Choose `demo_client_services` to work the staff queue or `demo_client`
to see the same desk from a clinic's portal. Use synthetic data only. The demo resets daily.
[Open the demo](https://openbga-lab-ops.onrender.com/)

## Try it with Docker

With Docker Compose installed, run these commands in Bash or Zsh. The application and
PostgreSQL listen on your machine's loopback interface. This configuration is for evaluation.

```sh
git clone https://github.com/openbga/lab-ops.git
cd lab-ops
docker compose --profile app up --build --wait
# Type a password of at least 12 characters and press Enter.
read -rs DEMO_PASSWORD
export DEMO_PASSWORD
docker compose --profile app exec -e DEMO_PASSWORD web python manage.py seed_demo
unset DEMO_PASSWORD
```

Open <http://127.0.0.1:8000> and sign in with the password you chose. The seed creates the
following accounts. Running it again preserves their passwords and records.

| Account | Where to start |
| --- | --- |
| `demo_admin` | Laboratory profile, teams, categories and service targets |
| `demo_supervisor` | Complaints awaiting review and restricted ticket details |
| `demo_client_services` | Desk queues, calls, replies and assignments |
| `demo_account_manager` | Client organizations, sites, contacts and portal access |
| `demo_technical` | Technical questions assigned to the team |
| `demo_client` | Portal for the whole example clinic |
| `demo_client_water` | Portal for one site of the example water utility |

Stop the trial with `docker compose --profile app down`. Its database stays in a named volume.

## Local development

Install Python 3.14, uv and PostgreSQL 17, or run PostgreSQL with the included Compose file.
From a clone of the repository:

```sh
uv sync --frozen
docker compose up --wait db
export DEBUG=true
export DATABASE_URL=postgresql://lab_ops:lab_ops@127.0.0.1:55432/lab_ops
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
uv run python manage.py runserver 127.0.0.1:8000
```

To load the same synthetic records, export a private `DEMO_PASSWORD` in a second terminal with
the same development settings, then run `uv run python manage.py seed_demo`. For a blank
laboratory, follow the setup below instead. Test commands are in the
[contributor guide](CONTRIBUTING.md#verify-your-change).

## Set up your laboratory

On a fresh PostgreSQL database, export the deployment settings from your secret manager or
process environment. Set `DEBUG=false`, a random `SECRET_KEY`, `DATABASE_URL`, `TIME_ZONE`
and your HTTPS host and origin. See [.env.example](.env.example) for setting names. Django
reads environment variables directly. Run these commands with the application's Python:

```sh
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py setup_lab --lab-name "Your Lab Name" \
  --admin-username owner --admin-email owner@yourlab.example
```

The command prompts privately for the first administrator's password. For unattended setup,
supply `SETUP_ADMIN_PASSWORD` in the environment. Sign in at `/accounts/login/` and open
**Accounts & roles** to create staff accounts. An administrator creates each account. Each account
can enrol an authenticator app, and **Security settings** chooses which accounts must have one. Staff need an explicit **Can view patient information** grant
to access person-level identifiers, including staff with the administrator role.

Under **Setup**, enter your laboratory profile, configure teams and choose ticket categories.
Add client organizations and contacts, then grant portal access to the appropriate organization
or sites. The help icon on each page explains its fields and permissions.

## Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/openbga/lab-ops/tree/main)

The [Blueprint](render.yaml) creates a paid Docker web service and PostgreSQL 17 database.
Review the estimate in your own Render account before creating them. It generates the secret
key, connects the database, collects static assets during the build and runs migrations before
deploying. Automatic deploys are off. In the web service shell, run the `setup_lab` command
above to create the first administrator.

Render's assigned HTTPS hostname is configured automatically. For a custom domain, set
`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`. Configure SMTP to enable password-reset and sign-in
email. To retry notices, send service target notices and close resolved tickets automatically,
run `python manage.py run_scheduled_jobs` every few minutes, for example from a Render cron job
([Scheduled jobs](docs/operations.md#scheduled-jobs)). The process and database health endpoints are `/health/` and `/ready/`. Back up PostgreSQL
and test a restore before storing laboratory records. For the separate, destructive demo reset
job, see [Public demonstration](docs/operations.md#public-demonstration).

## Documentation

| Guide | Contents |
| --- | --- |
| [Product scope](docs/product.md) | Laboratory workflows, roles and access rules |
| [Workflows](docs/workflows.md) | Staff and client journeys |
| [Roadmap](docs/roadmap.md) | Planned work |
| [Operations](docs/operations.md) | Hosting, backups, restore, mail and public demos |
| [Safeguards reference](docs/safeguards.md) | What the application records or enforces for each cited safeguard, and what stays with the laboratory and its host |
| [Architecture](docs/architecture.md) | Application boundaries, storage and integrity |
| [UI standards](docs/ui-standards.md) | Interface conventions and phone support |

## Contributing

See the [contributor guide](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md).
Please report vulnerabilities privately using the [security policy](SECURITY.md), which also
lists the security checks every change goes through.

## Project and support


OpenBGA Lab Ops is part of [OpenBGA](https://github.com/openbga) and an open-source project of [BGASoft](https://bgasoft.com), a laboratory software company.

**Community use.** The software is provided as is under the AGPL-3.0-only licence. The project does not include a support commitment: issues and pull requests are welcome and are answered on a best-effort basis.

**Enterprise support.** BGASoft offers paid support and services for organizations that run this software. Are you looking for support? Contact [bgasoft.com](https://bgasoft.com). Please do not use GitHub issues for support requests of that kind.

## Licence

Copyright (C) 2026 BGASoft Inc. and OpenBGA contributors.

OpenBGA Lab Ops is free to use, with no licence fee and no per-user pricing. Its source is
published under the GNU Affero General Public License version 3 only (AGPL-3.0-only), so anyone
may use, study, change and share it under that licence. BGASoft offers optional paid
[hosting](https://bgasoft.com/openbga/) and
[support and implementation](https://bgasoft.com/services/open-source-support/).

If you deploy a modified version, set `SOURCE_CODE_URL` to the corresponding source so users
can reach it from the application's footer. The trademark notice and bundled component licences accompany the
software. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [Third-party notices](THIRD_PARTY_NOTICES.md).
