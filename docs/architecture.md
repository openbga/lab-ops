# OpenBGA Lab Ops architecture

Each deployment serves one laboratory and its client organizations. A single Django application
renders the staff workspace and the client portal and stores their records in PostgreSQL. The
browser uses ordinary URLs and forms, with HTMX for partial updates. The application assumes
online access. Python dependencies are pinned in [uv.lock](../uv.lock) and browser assets are
served locally. [Browser dependency versions and licenses](../static/vendor/README.md)

## Application boundaries

| Package | Responsibility |
| --- | --- |
| `config` | Runtime settings, URL configuration and logging |
| `accounts` | User identities, roles, capabilities, authentication and initial setup |
| `core` | Laboratory profile and contacts, the security settings facts, optional module switches, audit events, navigation, preferences, page help, announcements, What's new, shared controls and demonstration commands |
| `clients` | Organization directory, sites, contacts and portal access grants |
| `teams` and `taxonomy` | Team membership, ticket categories, priorities and laboratory presets |
| `desk` | Ticket lifecycle, conversation, assignment, complaint records, saved views, search and notices |
| `service_levels` and `links` | Business calendars and ticket clocks, and references to records in external systems |
| `request_forms` | Form definitions, offerings, catalogs and captured values |
| `attachments` | Upload inspection, file metadata, bytes, withdrawal and authorized downloads |
| `dataimport` | CSV previews and transactional updates for the client directory |
| `jobs` | The notification outbox, the scheduled jobs command, its settings and the account the jobs act as |
| `nonconformance` | The optional nonconformance and corrective action module: records, actions, linked tickets and monthly counts |

Views and forms handle HTTP input, display and validation. Domain services use the Django ORM,
database constraints and explicit transactions. An action that affects multiple records checks
its authorization and the current state, makes the changes and records their audit events in
the same transaction. Row locks and expected revisions protect decisions against concurrent
changes. A form posted with an earlier revision of its record is shown again, to anyone who may
still read the record, with what was typed and the reason it cannot be saved: nothing is written.
After its last lock a change reads the acting account and its scope again before it writes. The
deployment's `TIME_ZONE` defines local input and display. The laboratory profile
records the zone selected at setup.

## Authorization

Role and capability checks live in [accounts/roles.py](../accounts/roles.py). An admin role
includes the operational roles. A person or group needs an explicit grant for patient
information. A superuser can administer accounts. Portal accounts have a client organization
and cannot hold staff roles or capabilities.

Use the same scoped query in lists, pages, lookups, counts and actions. Staff ticket
queries start with `staff_requests`. Portal queries start with `portal_requests`, which combines
organization, site grants, the originating account and the ticket's restriction. Client-visible
conversation is defined in `desk/visibility.py`. Checking the ticket URL alone is insufficient
for a file download. It must also pass the visibility rules for the entry and file.

Person-level links have no web address. A staff reveal or an exact-key search needs the
explicit capability and produces an audit event that omits the key. A non-person link can open
an HTTPS URL only on the external system's configured host. The application does not fetch
records from that URL. [Product access rules](product.md#roles-and-capabilities)

## Security settings

`core.security_settings` reads what protects sign-in, sessions and patient information from the
running configuration and the database on each request and stores nothing. Each fact describes
this release's configuration and follows the deployment settings read from the environment. The
code-level settings a fact depends on are listed with their release values (`RELEASE`), and the
settings module and the digests of `config/settings.py` and `config/logging.py` are pinned. The
settings record both digests while the configuration loads, through each module's own loader
(`config.digest`), so the comparison is of the configuration's source files as read while it
loads. It does not identify the bytecode Python ran: Python's cache can run older bytecode in rare
cases (bytecode compiled in unchecked-hash mode, or a source replaced within the same second at the
same size). The image writes no bytecode for its own source. When a deployment loads another settings module, the page names that module
instead of the files. When a deployment changes one, the fact says so and names it instead of
describing it. Whoever changes either file checks the
facts and updates the pinned digest (a test fails until then). The page and the
laboratory profile read in one snapshot (`core.concurrency.one_reading`) and are drawn after it. A laboratory setting
added there later is a row with a revision: its change takes the row lock, checks the revision the
form showed, rereads the acting account, requires a reason and records one audit event, as a
security or privacy contact does in `core.lab_profile`. The same change updates
[the safeguards reference](safeguards.md). [Security settings](../core/security_settings.py)

## Optional modules

An optional module's switch lives in `core.modules`. Its pages and reads start from selectors
that answer 404 while it is off, and menu and page links check the same switch. Each write holds
a shared lock on the switch, so turning a module off waits for writes already under way. A
module's records stay in place while it is off. [Optional modules](../core/modules.py)

## Services and hooks

The ticket service owns the transaction for a new ticket or entry. It checks scope, revision and
state before writing. Its `on_created` callback lets the forms and attachment services store data
with the ticket and its opening entry. A callback error rolls back the creation. The service also
calls the clock and notification hooks. [Ticket services](../desk/services.py)

A change that locks a person's rows and tickets locks the person's rows first. An assignment
locks the assignee's account, roles and team membership, then the tickets, then the team, and
decides from what it read once the acting account is read again; while it waits for a busy
ticket it holds up only work about the same person. A new ticket naming an assignee locks that
person's account, roles and membership of the ticket's team before anything else, its category
and a request form's definition included; if the team changes in between, the ticket is refused
and nothing is saved. Deactivating an account locks the account FOR NO KEY UPDATE, then the
person's open tickets, and then updates the account and returns those tickets to their queues
([desk/signals.py](../desk/signals.py)); removing a member locks the membership and then those
tickets. When an assignment and the end of the assignee's work meet, the second waits for the
person's rows and then decides on what the first saved: the assignment is refused, or the
deactivation or removal finds the new assignment and returns it to the queue. Neither fails with
a deadlock. A change that names an assignee without locking the account, such as a client's
reply that records a notice to the assignee, is checked against that account only when it
commits, with a key-share lock. The deactivation's no-key lock does not block that check. Its
update of the account does, until it commits: a full save names the unique username and email,
and with an update trigger on the table PostgreSQL then locks the row as FOR UPDATE does. The
tickets are locked before that update, so a reply holding one commits while the deactivation
waits for it. A reply that comes second waits for the ticket and, as the release changed the
ticket, is refused as changed; sent again, it finds the ticket unassigned. A nonconformance
names people (its owner, an action's assignee) by locking the record and then the person, and
never locks a ticket after
the person it names. An organization's new account owner is locked as an assignee is (the account
FOR NO KEY UPDATE, then its roles), after the directory lock and before any organization's row,
and checked on that reading ([clients/services.py](../clients/services.py) `hold_owners`): a
deactivation or role removal either committed before and refuses the owner, or waits for the
save. The edit form keeps showing a current owner who has since left, so saving other details
keeps them, and offers only active staff with a laboratory role; an import decides each row's
owner from the reading it held before the rows, and the rows' services use that reading
(`clients.services.owners_scope`) and lock nothing of the owner again. A ticket created naming that person as its assignee holds the account and checks the
organization when it commits, so the two wait for each other one way; an import holds every
owner its file names, in id order, before it reads the rows that lock organizations.

A ticket is locked by its id and the reader's scope is read again after the lock
(`desk.selectors.locked_in_scope`), so a change that waited while a colleague recategorized the
ticket is refused as changed, not answered as a missing ticket. A category is locked the same
way, by its id, with its group held after it, and both are read after the locks. Every change
reads the acting account again after its last lock and takes its checks of roles and
capabilities from that reading: the ticket services through `settle`, a list's bulk action right
after its tickets' locks, a new ticket's creation before it is written, the nonconformance module
through its own recheck, and file withdrawal, links and form corrections through `settle` or
`desk.services.locked_staff_request`; who asks is answered before anything about the ticket.

Each of these changes, and an import of external identifiers, holds the acting account from
before its first other lock until it commits ([accounts/acting.py](../accounts/acting.py)): first
the account's access gate, a transaction-level advisory lock, shared; then the rows its decisions
rest on, FOR KEY SHARE (the account, its roles, its capability grants and its team memberships,
or for a portal account its access and the sites it grants). The database checks the rows a
change names, such as its author and its creator, only when it commits, and that check can wait,
so a reading taken after the locks would not be enough on its own. What takes access away takes
the gate of each account it affects exclusively, before any row it locks: a save that
deactivates the account or moves a portal account to another organization (which then locks the
account FOR NO KEY UPDATE), the account page before its check of the remaining owners, the role group
page, a portal access change, and every removal of a role, a capability, a team membership or a
portal site. It waits for that person's changes under way. The gate queues in order, as row
share locks do not, so a change the person starts meanwhile waits behind it, then reads the new
state and is refused. An access change that affects several accounts, such as the role group
page, takes their gates one at a time in id order: a member it has not reached yet can still
start a change, which is saved before it. Because a person's own change holds their account before any ticket, it and
that person's deactivation or removal from a team wait for each other one way, never in a cycle.
Updates that take no access away, such as a sign-in, the email notice preference or a capability
added to a role, do not wait for the gate (an update of the account's row still waits a moment for
a change that holds that row, such as an assignment of a ticket to the person or a notice being
sent to them); a save on the account's page and every save of a portal account's
access take the gate and wait for the person's change under way. A password change, by the
person or on the account's password page, saves the password only: a full save of the account
as read when the request began would put back a deactivation committed meanwhile. A save that names only other columns (a sign-in's time) leaves the organization as stored
and never compares it with the copy of the account it read earlier
([clients/signals.py](../clients/signals.py)). The account page carries the account as it was
drawn (a fingerprint of the values it draws: names, email, organization, flags, the roles and
capabilities it holds, `accounts.admin.PAGE_FIELDS`; a role renamed on its own page changes no
account). After the gate and the remaining owners' rows it
locks the account's row FOR NO KEY UPDATE, so any other change of the account under way, an
email corrected on its own included, commits first, and refuses the save when what it reads
then differs from what the page showed. It writes only the columns it edits, so a password or
sign-in time recorded meanwhile is kept, and saves roles and capability grants as the
difference from that reading, so a role added by other means afterwards (nothing locks an
addition) is kept and the audit records only what this save added or removed, naming each role
from one reading. Setup changes (teams, categories,
forms, calendars and the like) do not hold their actor, except creating or changing a client
organization, a site or a contact, which can wait for the directory lock or an organization an
import holds, and hold their actor as an import does. An import holds its actor for the whole of its work
(`accounts.acting.holding`), and the demonstration seed the accounts it acts as that exist when
it begins (`accounts.acting.scope`); the services they call get that reading and lock nothing
again. The services that reveal patient
information read the account again after the hold.

A link change locks the ticket first, then the link's system, then the link; a link on an
organization locks the organization in the ticket's place. An import of external identifiers
locks all of the file's organizations, in the order the file names them, then their systems,
before its first row, so it and a link added at the same moment wait for each other one way.
Whatever changes how an import finds or checks records, or may lock several organizations in an
order of its own, first takes the organization directory lock, a transaction-level advisory lock
([clients/services.py](../clients/services.py) `hold_directory`), after holding its actor:
every import, before it reads the file; creating an organization; changing an organization's
name, account code or parent (which locks the organization and then walks up the new parent's
chain); changing a site's code; adding a contact or changing a contact's email or name; and the
demonstration seed, which first takes a lock of its own (one seed or reset at a time) and holds
the accounts it acts as. They take their turns: the second waits for the first to commit and
decides on what it saved, so a parent that would now close a loop is refused, a code taken
meanwhile is refused as a duplicate, and an import reads names, codes and emails as the change
before it left them. An organization change decides whether it needs the lock from a reading before its
locks; the revision check makes that reading the saved state for every change made through the
services. Changing an organization's other details, taking it out of its group, a site's or a
contact's other details and links do not take that lock. A sites or contacts file locks its
organizations FOR NO KEY UPDATE in file order before its first row, and a contact's served sites are held and
checked as read under that hold, so a file and a contact saved at the same moment wait for each
other one way. Should the database still end an import's locks to break a
deadlock, the file is refused with nothing imported.

Clock hooks update a ticket's first-response and resolution times inside that transaction.
A clock copies the applicable target, and business time uses the deployment zone and calendar.
Queries use the same clock states for lists and counts. The notification hooks record one
notice per recipient in the outbox inside the change's transaction, holding ids, event codes and
the ticket number only. After commit each notice gets its first attempt. At every attempt the
topic's handler takes shared locks on the rows its decision rests on (the ticket, the account and
the category; for a client, the account's portal grants; for staff, its roles, read and held in
one statement so the decision takes them from that reading, the team, held only against members
and leads changing, and the account's membership of it; for a passed target, all of the team's
memberships), decides from that reading whether the notice still applies, and builds the email
from the ticket number, fixed event text, the laboratory name and a sign-in URL. The notices one
transaction records get their first attempt together after it commits, sharing one wait for the
mail server and stopping at its first refusal; a savepoint that rolls back takes its notices and
their first attempt with it. The locks hold until the notice
is sent, so a change to those
rows waits; the ticket is waited for briefly and the rest are taken without waiting
([jobs/locks.py](../jobs/locks.py)), so an attempt never waits while holding a lock a writer
needs. A failed attempt is counted and retried later; delivery is
at least once. [Service clocks](../service_levels/hooks.py), [notifications](../desk/notifications.py)
and [the outbox](../jobs/outbox.py)

## Scheduled jobs

`manage.py run_scheduled_jobs` runs every job in [the registry](../jobs/registry.py) once, in
order, holding a PostgreSQL advisory lock so two runs never overlap. Each job works in short
transactions and locks each record it changes the way the services do, skipping a record another
transaction holds until the next run, so people working are never kept waiting. A job that fails
is logged with its error's class and frames, and the remaining jobs still run. The command exits
with an error when any job failed. Each job's last run is kept in `JobState` for the Scheduled
jobs page.

A job that changes a record acts as a dedicated "Scheduled jobs" account ([jobs/actor.py](../jobs/actor.py)),
created on first use, inactive, with no usable password, role or deliverable address. Its id is
stored in the job settings, not looked up by name. A job decides from records read under its own
locks: the automatic close locks the settings row and then the ticket, and the breach notice
locks the ticket and then its clock, the same order as the ticket services, and then, without
waiting, the access gates of everyone the rule names, shared
(`accounts.acting.try_hold_access`), and their accounts and its own FOR KEY SHARE, before
reading who may be told, and their email notice setting locks
(`desk.notifications.try_hold_notice_settings`; the setting's page takes its lock exclusively): a
deactivation, a removal of a role or a team membership, or a change of the notice setting
committed before is seen, one under way leaves the ticket for the next run, and one that starts
afterwards waits for the job.
A sign-in or an assignment of the same person does not wait for it. The job's account is looked
up without the settings row's lock once it exists; a first run that meets the settings being
saved leaves its work to the next run. Each breach run has its own identifier, which a
waiting notice must still match when it is sent. Later items add their
periodic work and their notice topics to the registry.

## Ticket forms

A form belongs to a category. Its fields define types, validation, catalog choices and who may
read or enter a value. A signed token binds the submitted form to the definition that the user
saw. The services recheck the definition, its availability and the account's permitted fields
when saving. A change that invalidates the definition requires a fresh form. Exact quantities
are validated as decimals. [Form definitions](../request_forms/definition.py)

A filled ticket keeps snapshots of field labels, types, audiences and patient marks. A correction
adds a replacement record with a reason. It preserves the earlier value. Visibility follows the
stricter of the recorded and current field rules. A staff reveal of a protected answer requires
a scoped ticket and a current value. It writes an audit event without the answer and returns a
response marked `no-store`. [Form services](../request_forms/services.py) and
[value visibility](../request_forms/selectors.py)

## Stored records and files

Operational entries present the conversation and state changes. Audit events record who acted,
when and why. Database triggers protect history, including audit rows, ticket entries, form
values and attachment bytes. A withdrawal or correction preserves prior material. Database
administrators with control of the tables and triggers remain within the deployment's trust
boundary. [Backup and restore](operations.md)

Logos and attachments live in PostgreSQL with their hashes and metadata, so a database backup
includes the files. Lists read metadata separately from file contents. Attachment intake checks
size, count, file extension and type-specific properties of the bytes. That inspection is a
format check. Downloads pass through a permission check on each request.
[Attachment inspection](../attachments/inspection.py) and [file access](../attachments/services.py)

The directory import validates CSV rows and presents their proposed changes. On confirmation it
checks the preview against current records and applies the file atomically with audit history.
The import does not provision accounts. [Importer](../dataimport/importing.py)

## User experience

Django forms and template components provide the same labels, error feedback and keyboard
behavior across pages. List filtering, sorting and pagination run on the authorized queryset.
DataTables manages column presentation. Table layout and other presentation preferences belong
to individual accounts. [UI standards](ui-standards.md)

Page help is versioned content selected by the authorized page's route and variant. Downloads
and POST actions are explained on the pages that invoke them. Staff What's new and the generated
changelog read the same entries from `changelog.d/`.
[Page help](../docs/pages) and [release notes](../CHANGELOG.md)

## Deployment and recovery

The Docker image runs Gunicorn and serves static assets through WhiteNoise. PostgreSQL holds the
persistent data. Migrate once before serving a new release, and keep migrations compatible
with any older instances still serving traffic. Publish subsequent schema changes as new
migrations. A code rollback does not reverse a migration.

`/health/` checks the process and `/ready/` checks database readiness. The normal deployment and
the resettable demonstration have separate Blueprints. Only the demonstration enables the reset
command. A reset checks the database's identity and schema, locks application tables and
reseeds in one transaction. Operators choose their host, mail delivery, backups, retention,
recovery objectives and access policy. [Deployment](../README.md) and [operations](operations.md)

## Verification

Run server tests with PostgreSQL, collect static files before testing templates, and exercise
staff and portal journeys at desktop and phone widths. Use a separate database for restore
checks. The contributor guide lists the test commands. [Contributing](../CONTRIBUTING.md)

Pull requests and a weekly run on `main` also go through static analysis, a dependency audit, a
secret scan and a scan of the container image, and build a software bill of materials. The audit
and the image scan do not run on pushes to `main`, so an advisory published for a dependency does
not stop an unrelated fix from deploying. Static analysis and the secret scan do run on those
pushes: an alert never fails static analysis, and the secret scan depends only on the commits
pushed. [Security policy](../SECURITY.md#what-is-checked)
