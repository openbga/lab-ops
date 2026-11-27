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
| `accounts` | User identities, roles, capabilities, authentication, sessions and their limits, and initial setup |
| `core` | Laboratory profile and contacts, the security settings facts and session limits, optional module switches, audit events, navigation, preferences, page help, announcements, What's new, shared controls and demonstration commands |
| `clients` | Organization directory, sites, contacts and portal access grants |
| `teams` and `taxonomy` | Team membership, ticket categories, priorities and laboratory presets |
| `desk` | Ticket lifecycle, conversation, assignment, complaint records, saved views, search and notices |
| `service_levels` and `links` | Business calendars and ticket clocks, and references to records in external systems |
| `request_forms` | Form definitions, offerings, catalogs and captured values |
| `attachments` | Upload inspection, file metadata, bytes, withdrawal and authorized downloads |
| `dataimport` | CSV previews and transactional updates for the client directory |
| `jobs` | The notification outbox, the scheduled jobs command, its settings and the account the jobs act as |
| `nonconformance` | The optional nonconformance and corrective action module: records, actions, linked tickets and monthly counts |
| `audit_review` | The audit review workspace over the change history, access records and sign-in history, its counts by person, its exports and the record of its own use, and the signed review records with their notes and the review interval; the Access review section (`access_review` URLs at `/access-review/`): the access report, the unused accounts with their period, and the signed access reviews |

Views and forms handle HTTP input, display and validation. Domain services use the Django ORM,
database constraints and explicit transactions. An action that affects multiple records checks
its authorization and the current state, makes the changes and records their audit events in
the same transaction. Row locks and expected revisions protect decisions against concurrent
changes. A form posted with an earlier revision of its record is shown again, to anyone who may
still read the record, with what was typed and the reason it cannot be saved: nothing is written.
A ticket's page, on the desk and in the portal, is read and drawn in one snapshot of the database
(`core.concurrency.one_reading`), the signed-in account and its access included, so the revision
its forms carry is the one of the ticket as it shows it: details, conversation, status and links.
The reply and entry posts run outside any request transaction (their services open their own),
so the page drawn again after a refusal has a snapshot of its own too. What drawing writes (the
What's new mark, only while the account has none) is saved after the snapshot
(`core.concurrency.after_reading`).
After its last lock a change reads the acting account and its scope again before it writes. The
deployment's `TIME_ZONE` defines local input and display. The laboratory profile
records the zone selected at setup.

## Authorization

Role and capability checks live in [accounts/roles.py](../accounts/roles.py). An admin role
includes the operational roles. A person or group needs an explicit grant for patient
information. A superuser can administer accounts. Portal accounts have a client organization
and cannot hold staff roles or capabilities.

A capability given directly (`accounts.models.CapabilityEnd`) and a portal access
(`PortalAccess.ends_at`) may end ([accounts/grant_ends.py](../accounts/grant_ends.py)). The form
takes a last day; the end stored is the start of the next day in `TIME_ZONE` as set when the day
is saved, so a later zone change does not move it. Each form sends back, hidden, the day it
showed: a day sent back unchanged keeps the stored end (`grant_ends.chosen_end`), so a save made
after a zone change does not move an end nobody edited, and only an end of access granted for the
account's current organization is kept; any other day is a new choice (today or later, within 100
years). There is one clock: every check compares the
end with `transaction_timestamp()`, the database's time at the start of the checking transaction,
in the statement that reads the grant, so every process agrees and every check within one change
or one page reading gives the same answer. The choke points are `accounts.roles.direct_grants`
(every check of whether an account holds a capability directly: `has_capability`, the sidebar's
`roles_and_capabilities`, the Security settings holder counts, the access report's capability
filter, the access review's choice of staff accounts and whether a removal is still held) and
`clients.portal.current_access` (every portal scope reading); the second-factor rule's SQL repeats
the same condition. Pages that list stored grants, ended ones included, read each grant with its
end and whether it has ended (the access report's rows and the access review's list of grants in
one statement, `audit_review.access_report.direct_with_ends`; the admin account list from two
prefetches) and leave out or mark the ended ones. An ended grant stays stored, giving nothing; removing a direct
grant deletes its end and a grant given anew starts without one (an `m2m_changed` receiver).
Saving a `CapabilityEnd` takes the account's access gate exclusively (a `pre_save` receiver, like
a removal); a portal access's end is changed only by the Set access form and the portal access
services, which take the gate and advance the access's revision. The move trigger of
`clients.0007`, replaced in `clients.0008`, empties the end with the rest of the access and
advances the revision; the gate is the moving caller's (an attributed move takes it). A page that
sent back no shown day is read as showing the day of the end the save holds, after the gate. Drawn
again after a refusal, the account page carries the day of the end the refused save held under
the gate, or, when it held none, says again that none was sent; Set access carries the day it
read, which the access's revision checks.
Known limits: a change that checked the grant before the end and commits after it is a holder's
change, since nothing waits for an end; other code that saves a `PortalAccess` (its end or its
scope) neither takes the gate nor advances the revision, as before this change; paths that bypass
`save()` (`QuerySet.update`, raw SQL) take no gate; and a form cannot tell an altered (or emptied)
shown day: a day sent equal to it keeps the stored end, and any other day is a new choice, checked
as one, so after a zone change an unedited day sent with an emptied shown day is stored again in
the new zone.

Use the same scoped query in lists, pages, lookups, counts and actions. Staff ticket
queries start with `staff_requests`. Portal queries start with `portal_requests`, which combines
organization, site grants, the originating account and the ticket's restriction. Client-visible
conversation is defined in `desk/visibility.py`. Checking the ticket URL alone is insufficient
for a file download. It must also pass the visibility rules for the entry and file.

Person-level links have no web address. A staff reveal or an exact-key search needs the
explicit capability and produces an audit event that omits the key. A non-person link can open
an HTTPS URL only on the external system's configured host. The application does not fetch
records from that URL. [Product access rules](product.md#roles-and-capabilities)

A form that changes roles, capabilities, security settings or another account's sign-in factors
mixes in `accounts.confirmation.ConfirmIdentity`: in the account administration the account, new
account, password, compromised-password, authenticator-removal and role forms, and the session
limits and second-factor rule forms. Unless the browser's session holds a re-authentication of
every factor the account has now, each within `ACCOUNT_REAUTHENTICATION_TIMEOUT` (the sign-in does
not count), the form adds the password and code fields and checks them before its own `clean()`,
so nothing is locked, decided or saved for a request that has not confirmed; the view's access
check comes first, so a refused request checks no password. Each factor proved is recorded as
allauth records a re-authentication, so allauth's own re-authentication pages and these forms read
the same records; a password re-authentication counts only while the account keeps the password
that was checked (a keyed digest of its hash in the session), a code only while it keeps the
authenticator the code came from and when it was given after its newest app was added. Failures go
through the adapter and allauth's signals into the sign-in history, as below. The confirmation
fields never count as a changed field. Ending sessions does not ask. The confirmation is read
when the save is checked and not held through the save: a factor changed by another request
between that check and the commit does not stop that save. Tests run as confirmed unless marked
`confirmation` (the root and e2e `conftest.py`). `start_session` drops the re-authentication
records at every sign-in, even of the same account in the same browser, so a confirmation never
outlives the sign-in it was given in.
[Confirm it is you](../accounts/confirmation.py)

Where the laboratory turns it on (`core.models.RevealConfirmationRule`, one row written when an
administrator first saves it, so a database never saved asks nothing), the views that reveal a
patient-marked answer or a person-level key ask for the same confirmation once per browser session.
Each decides in the reveal's transaction, after holding the acting account and before reading
anything: `may_reveal` takes the rule's advisory lock shared (a save takes it exclusive), and passes
when the rule is off, the session carries this sign-in's mark, or `accounts.confirmation.current`
holds, which then sets the mark. The mark names the account and the sign-in's session row
(`accounts.sessions.SESSION_KEY`), so it lasts for that sign-in only: a sign-out empties the
session, and a new sign-in, even of the same account in the same browser, has a new row. Otherwise
the view answers with a redirect (`HX-Redirect` under HTMX) to `account-confirm-reveal`, which sets
the mark, without showing its form, when `current` holds, or once the confirmation is accepted, and
returns to the page on screen; the person reveals again.
[Confirm before revealing](../core/reveal_confirmation.py)

## Access records

A sensitive read leaves a `core.models.AccessRecord` ([core/access.py](../core/access.py)): the
account that made the request, as signed in when its view was called (`AccessBindMiddleware`
keeps it in `process_view`, so a page that signs the browser out while it is drawn is still
recorded for it; by id, no database constraint, as for the sign-in history), the capacity
(laboratory or client; an account's kind never changes), the kind of read,
the ticket's id, the file, answer or person-level link read (`object_type`, `object_id`, checked
against the kind by a constraint) and the request ID that `core.request_logging` generates, never
what was shown. The views call `access.note` once access is decided and what they show is read:
the staff and portal ticket pages (also when drawn again after a refused entry or reply), the audit
history, Who opened it (`desk.views.request_access`, `desk.opened`, whose pager draws every page as
of the newest record when the first was drawn, so new records never move a row to the next page),
the Form details page and the complaint forms (`_action_form`'s `access_kind`), after the drawing;
a download after the file's bytes are read; a staff reveal of an answer or a person-level key after
it is read in the reveal's transaction; the `request_form_details` tag for each patient-marked
answer it draws for a portal account (`request_forms.selectors.client_values` gives each row's
value id and mark); the client timeline for each ticket whose entries its page shows; and Request
again for the ticket its answers come from (`request_forms.selectors.repeat_from`). A refusal
raised before the note records nothing. `AccessBindMiddleware`, the innermost, moves what the view
noted onto the answer it returned, and drops it when the view raised (`process_exception`);
`AccessRecordMiddleware`, inside the request log middleware and outside the session middleware,
writes in one statement what the answer it is about to send carries, so a page drawn in
`one_reading` writes after its reading has ended, and an answer another layer put in the view's
place (Django's 400 when the session was deleted in another tab while the page was drawn) carries
nothing. A HEAD request and an answer of 500 or more record nothing; when the write fails, the
middleware raises and the answer becomes a server error, so nothing read is sent without its
record. The table is kept apart from `AuditEvent`, so a ticket's history lists its changes only; a
trigger (core migration 0009, `prevent_record_mutation`) refuses `UPDATE` and `DELETE`, and the
model has no permissions and no admin. Change events gain the same request ID
(`AuditEvent.request_ref`, filled from the request being served by the field's default, blank
outside one, with a database default so that a release still running during the migration keeps
inserting events). Not covered: a record whose write was under way, in another
request, when Who opened it drew its first page can still take a place in the later pages (ids are
given out before a write commits); its pages never show a record written after that, and only
records of the ticket. Request again decides from its reading of the earlier ticket whether it
copied something: a site deactivated before the form reads its sites again, or typing kept across a
sign-out given back over the copied answers (accounts.kept), leaves the notice and the record
although the form shows none of them. Code that signs another account in during a request (nothing in this
release does) does not change the account recorded. This release's views draw their pages at once;
a deployment's own template-response hook or post-render callback that replaces or rewrites a deferred page leaves the
records with the answer Django ends with, whatever it shows.
[Access records](../core/access.py)

## Audit review workspace

`audit_review` reads `AuditEvent`, `AccessRecord` and `SignInEvent`. Its lists write only its own
`WorkspaceUse`; its review records (below) write `Review`, `ReviewNote` and `ReviewSchedule`, each
with an `AuditEvent`. Its views (`audit_review.views`) decide access, the filters' choices and the
rows in one reading (`one_reading`, `reading_account`): an active staff account with a laboratory
role and the `review_audit_records` capability (`audit_review.permissions`; `has_capability` reads
the grant on the account or a group). Each list (`audit_review.sources`) orders by
`(-occurred_at, -pk)` over a new `-occurred_at` index per table (core 0010, accounts 0010) and is
read as of `upto` (`sources.newest`), which the pager's and the export's links carry and the view
clamps to what is newest now: the newest id for the tables whose ids follow the order rows are
written (a sign-out by a session limit is written with the earlier moment the limit passed, so a
time would let it into an earlier reading), and for `AuditEvent`, whose ids are random, the moment
by the application's clock, which also times each event, taken before the reading's snapshot. As
for Who opened it, a row whose write was under way in another request at that moment can still take
a place in the later pages; so can a change saved by a process whose clock runs behind. The
organization filter reads which accounts and tickets belong to the organization at each page and
export, so an account moved since the first page counts for its new organization. A day filter runs
from the day's first local midnight to the next day's (`sources._day_start`), so on a day whose
clocks went back across midnight the repeated hour's rows belong to the later day. The ticket and
organization filters on Changes match `object_type` and `object_id` against subqueries of the
ticket's parts (`TICKET_PARTS`) and the organization's records, cast to text. Rows are built from
ids and codes: names as they stand now, never `before`, `after` or `reason`; access rows reuse
`desk.opened.describe`, which names a file or an answer only when it belongs to the record's ticket.
The export is the same reading as CSV (UTF-8 with a byte order mark, formula-leading cells prefixed
with an apostrophe), at most `EXPORT_LIMIT` rows, built in memory. Each page shown and each export
hands a `WorkspaceUse` to `core.access.keep`, so it is written by `AccessRecordMiddleware` with the
answer on the access records' terms (none for a refusal, a redirect, HEAD or a 500, and a failed
write fails the answer); `core.access.write` writes each table in one statement, in one transaction
when there are several. A trigger (audit_review migration 0001) refuses `UPDATE` and `DELETE`. The
sidebar shows Audit review from `accounts.roles.roles_and_capabilities`, which reads the role groups
and the capabilities in one statement (`CAPABILITY_ITEMS` in `core.templatetags.navigation`).

`audit_review.hours` judges business hours in the database: it turns an active
`BusinessCalendar`'s opening intervals over the period (`service_levels.business_time.open_intervals`,
holidays closed, the day before included for an opening that runs past midnight) into a PostgreSQL
`tstzmultirange` literal, and `Outside` tests `occurred_at` against it, so an opening moment is
inside and a closing moment outside. The calendar is read when the page is drawn, at most
`MOST_DAYS` (366) days at a time. The lists' "Outside the hours of" filter and the People tab both
use it; the Event and What choices that stand for several kinds are `sources.GROUPS`.
`audit_review.people` draws the People tab: one grouped count per table (`SignInEvent`,
`AccessRecord`, `AuditEvent`) by account, each as of its own marker (the same `upto` the lists use,
carried as `upto_changes`, `upto_access` and `upto_sign_ins`), merged and sorted in Python and paged
50 at a time. Unlike the Changes list, the tab's Changes marker is the time of the newest change
its reading's snapshot holds that is not timed ahead of this server's clock, or 0 when there is
none since 1970 (`people.newest_change`, read after the reading's first query), so the three counts
come from one snapshot, and Changes is also held by that marker. Being held by a time, Changes can
still take on a later page a change saved while the first page was drawn, or by a server whose
clock is behind, ahead of or set back from this one's; a change timed ahead of this server's clock
counts from the time it names. Its own links also
carry the period and calendar the first page applied (`reading_from`, `reading_to`,
`reading_calendar`, which are not filters), so later pages and the export keep them past midnight
or a change of the calendars, and refuse a calendar no longer active. `people.link` gives each
count the address of its list with the person, period, marker, group and calendar, so the list
returns exactly the rows counted. Its pages and exports are
`WorkspaceUse` rows of the source `people`, which audit_review migration 0002 adds to the allowed
values without rewriting a row.

`audit_review.reviews` keeps the review records. `Review` holds the period, the lists examined
(`EXAMINED`, a subset of `Source`, in an array checked by a constraint) and other records examined,
the outcome, findings and follow-up, and the sign-off: `signed_by` (an id without a foreign key
constraint, as for access records), the signer's name, username and roles as they stood
(`roles_label`), the meaning (`Meaning.REVIEWED`), `signed_at` by the application's clock and
`checksum`, the SHA-256 of `signed_content`: those fields as JSON with sorted keys and no spaces,
notes excluded. `sign_review` holds the acting account (`accounts.acting`) and checks
`may_review` and the checked password's hash on it, takes `SIGNING_LOCK` (an advisory lock held
until it commits, so numbers follow the order signings commit in), then takes the id from the
table's sequence so the number (`AR-` and at least six digits) is part of what is signed, and writes the
review and its `audit_review.signed` event in one transaction. The view checks the password before calling it (`review_forms.signing_refusal`, through
the allauth adapter's `reauthenticate`, so a refusal is a failed re-authentication in the sign-in
history and counts toward the failed sign-in limit, and nothing is written to the session).
`ReviewNote` rows are added by `add_note` under a row lock on the review, refused when the page's
revision (1 plus the number of notes) is stale. Triggers from audit_review migration 0003 refuse
`UPDATE` and `DELETE` on both tables with core's `prevent_record_mutation`; `reset_demo`'s
`TRUNCATE` is not seen by row triggers. `ReviewSchedule` is one row (`id = 1`), written the first
time the interval is saved, with a `revision` checked under an advisory lock and the change
recorded as `audit_review.interval_changed` with a reason. `status()` works out where reviews stand
on each request: the last review is the one with the highest number (signed last, in the order
signings commit), and the next due day is the local date of its `signed_at` plus the interval, so
servers whose clocks disagree can give a later signing an earlier time; gaps are computed from
every review's period. The Reviews list is read as of the newest review
id (`upto`), like the other lists, and its pages, each review page and its export are
`WorkspaceUse` rows of the source `reviews`, which migration 0003 adds; the Changes list names a
review's events by its number.
[Audit review](../audit_review/views.py)

## Access review

`audit_review.access_report` draws the Access review section, mounted at `/access-review/` with
the namespace `access_review` (`audit_review.access_urls`, `audit_review.access_views`), for the
same holders as the workspace (`require_reviewer`). Each page and export runs in one reading
(`one_reading`, `reading_account`) and describes the accounts as they stand at that moment, not
as of a first page: `describe` reads a page's groups, direct capability grants, group grants,
teams, portal access, authenticators and the second-factor rule in a few queries per batch of 500
accounts. Whether the rule covers an account comes from `core.second_factor.covered_among`, which
answers `requires` for many active accounts in one statement. The scheduled jobs' account
(`jobs.actor.system_account_id`) is its own kind.

Who gave a role, capability or admin console access comes from `AuditEvent`, read in order of
`(occurred_at, id)` (`grant_history`): the time each change was recorded by the application's clock,
then its random id, so two changes recorded at the same moment, or by a server whose clock runs
behind another's, can be replayed in another order than they were made, and who gave a role can then
be shown wrongly or not at all. Account events `account.created`, `account.updated`,
`setup.account_created` and `setup.account_roles_added` carry `groups`, `permissions` and the
`is_active`, `is_staff` and `is_superuser` flags before and after; the seed's
`account.capability_granted` names one capability; group events `group.created`, `group.updated` and
`setup.group_created` carry a group's permissions and name. A `group.updated` that changes `name`
moves the memberships recorded under the old name to the new one. Known limit: the application never
removes a group, so a name belongs to one group; a group removed outside the application and
replaced under the same name is not told apart from it, and who gave a membership of the removed
group can be shown for the replacement. Every writer of an account's roles (the account page when
the save changes roles, the add-account page, `setup_lab`) names them from
`accounts.setup.held_role_names`, which reads them `FOR SHARE` as the change is recorded, and the
role group page holds the group's row before reading its values for the audit, so a rename or a
second save waits and the history names each grant in the order it was made; an account save that
does not change the account's roles names them as read, so a rename committed while it saves can
leave the earlier name in its event (no change of roles is read from it). Known limit, which
predates Access review: when the role group page takes a capability away, it reads the members whose
access gates it takes before it holds the group's row, so a member who joins, or a grant made, in
between is gated only by the removal's own signal (`accounts.acting`), after the row; were that
member's roles being changed at the same moment, PostgreSQL would refuse one of the two saves as a
deadlock. For each item held now the latest event that gave it, not taken away since, is shown;
otherwise "Not in the change history". A capability through a group names both the grant to the
group and the account's membership. Actor names are read as they stand now. `seed_demo` records an
`account.created` event (actor `demo_admin`, the account's snapshot without its email address:
`accounts.setup.seed_snapshot`) for each demonstration staff account it creates, so their roles are
attributed.

The Unused accounts list (`unused_accounts`) keeps active accounts, without the jobs' account, whose
`last_login` is before now minus the period, or that never signed in and whose `date_joined` is
before then, ordered by `Coalesce(last_login, date_joined)` (longest unused first), then id. The
period is `UnusedPeriod`, one row (`id = 1`, days null or 1 to 366, both checked by constraints)
written the first time it is saved, never by a migration. `set_period` holds the acting account,
takes `PERIOD_LOCK` (`pg_advisory_xact_lock(19565, 0)`), refuses a `revision` other than the one the
form showed, and records `access_review.unused_period_changed` with a reason; the form's own reading
takes the same lock shared. The two lists' pages and exports are `WorkspaceUse` rows of the sources
`accounts` and `unused`, which audit_review migration 0004 adds to the allowed values (the check
constraint is dropped and recreated, no row rewritten) along with the `UnusedPeriod` table.

`audit_review.access_reviews` keeps the signed access reviews (the Reviews tab,
`audit_review.access_review_views`). A review covers one `AccessScope`: active staff accounts
without the jobs' account, or active portal accounts, of every organization or of one; at most
`MOST_ACCOUNTS` accounts (their gates are held until it commits) and `MOST_GRANTS` grants (one form
field each, under Django's field limit). `read_grants` reads each account's grants as they stand:
the console flags, group memberships with the capabilities each group gives, capabilities given
directly whose end has not passed (with their last day), and a portal account with its
`PortalAccess` (its last day, or none once it has ended). An end passing before the signing reads
again is a changed grant. A grant's identity is its account, kind and
code; its form field is named from a digest of everything shown about it, so a decision is never
carried over to changed words, and the form posts a `fingerprint` of every grant it showed.
`sign_access_review` reads the ids of the accounts the scope covers before any lock, takes their
access gates shared with the signer's, in key order (`accounts.acting.share_access`, the order
`close_access` uses), before anything else it locks, then holds the acting account (`hold_actor`)
and checks `may_review` and the checked password's hash on it. It reads the accounts and grants
again and compares them, with the scope and organization, with the posted fingerprint; any
difference refuses the signing, and the view draws the form again over the new reading, keeping the
decisions whose field names are unchanged. Then it reads the signer's roles and takes the moment of
signing, so everything recorded was read before it. Until the review commits, what takes a reviewed
account's gate exclusively waits (the account's page, removing a role, a capability or a team
membership, portal access). Known limit, one rule: what changes without that gate goes on, such as
a name of a group, site or organization, a capability added to a group on its page, or an account
created or made active; such a change saved while the signing reads can be recorded as it was just
before.
Numbers (`AC-` and at least six digits) come from the table's sequence under the advisory lock
`(19566, 0)`, so they follow the order signings commit in. The review, an `AccessReviewItem` for each
grant with its decision, and the `access_review.signed` event (no email address field) are written in one
transaction, with a SHA-256 of `signed_content`. Triggers from audit_review migration 0005 refuse
`UPDATE` and `DELETE` on both tables with `prevent_record_mutation`; reversing the migration is
refused while any access review or a use of the tab is recorded. The review records decisions and
changes no account. `still_held` reads, when a review page or its export is drawn, whether each grant
decided for removal is still held: a deactivated or missing account holds nothing, and a portal
account is held while it is active in the organization it was reviewed in. The tab's pages, each
review page, both exports and each drawing of the form are `WorkspaceUse` rows of the source
`grant_review` ("Access review: reviews"), which migration 0005 adds to the allowed values.
[Access review](../audit_review/access_report.py)

`accounts.deactivation` keeps what an account held when a save deactivated it
(`accounts.models.Deactivation`, migration `accounts.0012`, whose trigger refuses `UPDATE` and
`DELETE`). `User.save` decides that it deactivates the account (the stored row active, the save
writing `is_active` false) once it holds the account's access gate and row; it reads `holdings`
(the access report's `describe` without history: kind, organization, console flags, roles,
capabilities with how each is held, teams and portal access, in words) before it writes anything,
and inserts the record after the account's row, in the same transaction, with `changed_by`, the
Deactivate page's reason, the tickets `desk.signals` returned and the database's clock. Every
deactivation through `save()` leaves one; `QuerySet.update`, raw SQL and deactivations before the
migration leave none. The Deactivate page (`AccountAdmin.deactivate_view`, `deactivate`) takes the
access gates of the owner and the account in id order (`close_access`), reads the owner again (an
active superuser with staff status), refuses the owner's own account, an inactive account and the
jobs' account, locks the account's row FOR NO KEY UPDATE, compares a fingerprint of `holdings`
with the one the page was drawn with, hands that reading to the save as the record's (so the
record is the list the page showed), then saves `is_active` alone and records `account.updated`
with the reason; two owners deactivating each other take turns and the second is refused. Known
limit: the reading sees what had committed when it was read, so an addition that takes no gate of the
account (a member added to a team or made its lead, a capability added to a group, code, the
database directly) and commits after it is not in the record and does not refuse the Deactivate
page's save; the account page, portal access and every removal take the gate.
[Deactivation](../accounts/deactivation.py)

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
on that page is a row with a revision, written the first time it is saved: its change holds the
acting account (`accounts.acting`), takes its lock, checks the revision the form showed and the
role as the account is held, requires a reason and records one audit event, as the session limits
do in `core.session_limits`. The same change updates [the safeguards reference](safeguards.md).
[Security settings](../core/security_settings.py)

## Required second factor

`core.second_factor` holds the laboratory's rule (`SecondFactorRule`, one row, saved like the
session limits) and turns it into one condition on the accounts table, used both to decide for one
account and to count them, so the page and the sign-in agree. Roles are matched as
`effective_roles` counts them and the patient-information grant as `has_capability` does (on the
account, while its end has not passed, or a group). At sign-in, `accounts.sessions.start_session` records on the new
`AccountSession` row whether the rule covered the account then (`second_factor_required`).
`accounts.enrolment.EnrolmentMiddleware`, after `SessionLimitMiddleware`, reads in one statement
on each request that flag, whether the account has an authenticator of a type the deployment offers
(`MFA_SUPPORTED_TYPES`) other than recovery codes, and whether the rule as it stands still covers
the active account; while the flag is set, there is none and the rule covers it, only the setup
routes (`ALLOWED_VIEWS`) are reached. Other requests are sent to the setup page: redirected (the
first GET opened in the window is remembered for Continue: the start page when it is the setup
page itself, and the next one when it is another setup route), told to load it in the whole window
(HTMX and the form dialog) or refused with JSON for scripts. The setup page draws whether the
session waits, why, and the account's recovery codes from one snapshot. Deciding at sign-in keeps open sessions and
their unsaved forms going when the rule changes; deciding each request from the factor keeps a
session that removes its only authenticator held. The rule, the account, its groups and its grants are read in one
statement (`core.second_factor`: the setup page's reasons and the Security settings counts too).
Continue's page is kept in the browser's session, which Django writes as a whole, so two tabs
choosing Continue at the same moment can both go there (a strict xfail documents it). Two
limitations of allauth's own sign-in steps predate this and are documented with strict xfails: a
stored credential of a type the deployment does not offer (a security key kept from an earlier
configuration), when it is the account's only authenticator, leaves password sign-in asking for a
code the account cannot give; for an account holding only recovery codes, allauth's overview
offers Generate, whose submission explains that an app is needed first; and an account
without a password whose recovery codes are all used is asked for a code to confirm before adding
an app; a superuser removes the stored credential in the admin console (Authenticators). After a
re-authentication that resumes a posted setup form, the help shown is the re-authentication page's. allauth's
refusal to add an authenticator while an address record is unverified is off
(`MFA_ALLOW_UNVERIFIED_EMAIL`): sign-up is closed, and on a deployment without outgoing email a
required account holding an unverified address could otherwise never enrol (an account with no
address record was never refused).
[Required second factor](../accounts/enrolment.py)

## Sessions

Each sign-in is an `AccountSession` row, created by Django's `user_logged_in` signal once every
sign-in step has passed and carried in the browser's session by its id
([accounts/sessions.py](../accounts/sessions.py)). `SessionLimitMiddleware` runs after the
session, authentication, message and locked-credentials middleware. Before anything acts for the
account it reads the row and the limits for the account's audience (staff or client portal, as
the account is when the request arrives) in one statement, by the database's clock. A missing,
foreign or ended row signs the browser out. A row past a limit is locked and read again: if it
had still passed a limit as of the request, by its values and the limits as they stand, it ends
with the limit passed first and the moment it passed, the browser is signed out and a notice is
kept for the sign-in page; if it had been extended or a limit lengthened meanwhile, the request
goes on. A request that goes on counts as activity once it reaches its view, after its CSRF check
and the locked-credentials check: one statement moves the last activity to the database's clock
at that moment (never back), with the limits read in the same statement, and matches only an open
row inside both limits. A row another transaction holds at that moment (another request of the
session, a change or an ending under way) is skipped, not waited for, except by Stay signed in.
The status address the page asks before warning is not activity. A page opened with GET then goes
on as a signed-out visitor and its sign-in returns to it; a form, a dialog or a script request is
answered at once, the sign-in set to return to the page the person was on, and so is any such
request a view sends to sign in and back to its own address while the browser is not signed in.
`NoStoreMiddleware`, the outermost layer, marks every answer to a signed-in session, and to a
request with a session cookie answered before that was known, `no-store`, except static files and
a response that opts out (`vr_cacheable`, the laboratory's logo).

A change that holds its acting account (`accounts.acting.hold_actor`) also holds the request's
session row `FOR SHARE` and is refused (`SessionEnded`, not a permission refusal, answered like a session found ended) once
the row has ended or passed a limit, so ending a session waits for such a change under way.
Deactivating an account ends all its rows in the same transaction (`accounts.models.User.save`);
any change of the stored password ends all of them except, when the account changes its own
password other than by an emailed reset and the request keeps Django's session valid, the row of
the request making the change. Rows are ended before the account's row is written, so a change
holding a session can still take the account's row. Rows a browser abandoned stay open until they
pass a limit; they are closed as expired, and logged, when the account next signs in or its
sessions are ended. The rows are kept after they end. The limits are rows of `core.SessionLimit`,
one per audience, absent until first saved.

Passwords are checked by the validators in `AUTH_PASSWORD_VALIDATORS` (`accounts.passwords`),
which every form that sets one runs, and `setup_lab`, `changepassword` (replaced by
`accounts.password_command`, which saves the password alone) and the demonstration's seed. The
checks read what they compare when the password is checked, just before the save: the length
reads the second-factor rule and the account in one statement (`core.second_factor.requires`),
and the account's names are read again (`current_account`), so a page drawn under one rule or
name is checked against the ones in force then. A rule saved between that check and the save
applies from the next password (a strict xfail documents it). The form's guidance is drawn when
the page is, and drawn again from the length a check used, so a refusal and the guidance beside
it name the same length. The checks against common and expected passwords compare the whole
password and take time proportional to its length. A reset link's token also covers whether the
account is a shared demonstration account (`accounts.tokens`). allauth's sign-in limits and its
other counters live in the database cache `core.cache.SharedCache` (`CACHES`, the table
`lab_ops_cache` created by `core` migration 0007, outside the models so that reset_demo neither
reads nor erases it); allauth serialises each count with a lock entry added to the same table,
and the table's primary key makes that addition atomic across processes. Keys are HMACs, every
write removes expired entries and never a live one, and an entry lasts at least as long as asked.
Two allauth behaviours remain, documented with strict xfails: a completed second factor, and a
password reset, clear the failures counted for their address; and giving back an attempt removes
every attempt recorded at the same clock reading.

A row records the browser's `User-Agent` (printable, cut to 300 characters) and the client address
at sign-in, by the trusted proxy settings the sign-in limits use; a release still running while the
migration applies goes on inserting rows through the column's database default. Where you are
signed in (`accounts.session_list`) lists the account's rows that are open and inside both limits
by the database's clock, and the person ends one other row or every other row; a superuser ends
every row of an account from the account administration, with a reason (on their own account,
every row but the one in use). Each of these endings takes, before anything else, a
session-ending advisory lock for the account whose rows it ends and for the acting account, in key
order, then holds the acting account (`hold_actor`, which holds the acting session `FOR SHARE`).
Two sessions ending each other therefore take turns, and the second is refused because its own row
has ended, instead of each holding its own row and waiting for the other's. Signing in again in
the same browser takes the account's session-ending lock too. A save of a new password alone,
which takes no access gate exclusively and ends the rows before writing the account's row (an
access change locks the account's row first), takes the session-ending lock and then the access
gate shared, so it takes turns with the person's own endings and with an access change, but not
with the person's other changes. Every ending of rows (these, a deactivation, a new password, signing in again in
the same browser, Sign out, the cleanup of expired rows) locks all its rows in one statement, in
id order, waiting for a change under way that holds one, then decides each in a new statement of
the same transaction, so that what committed during the wait counts: a row that has passed a limit
by the database's clock then ends as expired, at the moment its limit passed; any other ends as
asked. One `account.sessions_ended` audit event names the rows ended as asked, never their browser
or address. Every other row open when the change is saved ends, including rows that signed in
after the page was drawn; the confirmation counts those by their sign-in time against the
database's clock when the page read the rows.

Marking a password as compromised (`accounts.compromised`) takes the session-ending locks of the
superuser and the account, holds the superuser (`hold_actor`), then locks the account's row
`FOR NO KEY UPDATE`, so a password saved with the whole account (setup, a command) has committed
or waits; it decides from that reading, including a keyed fingerprint of the stored password that
the page was drawn with (`drawn_password`, an HMAC under the deployment's secret key), records a
`CompromisedPassword` row with a copy of the stored hash, ends the account's rows
(`password_compromised`) and records one `account.password_marked_compromised` event without the
hash. A sign-in takes the same session-ending lock in `start_session`, so it either started before
the mark and is ended by it, or reads the mark: a sign-in whose steps, as allauth recorded them in
the browser's session, include no second factor or emailed code inserts its row already ended. An
open row of a marked account has therefore proved more than the password, and
`PasswordChangeMiddleware` (after `EnrolmentMiddleware`, which goes first) holds it to Choose a new
password; the mark is read in the same statement as the second factor's hold
(`accounts.enrolment.holds`). The middleware also checks the sign-in's recorded steps again at each
request, before either hold, and ends a marked account's session that proved only the password, as
a process of an earlier release could have opened one. `User.save` lifts the open mark in the same
transaction when a password is chosen (`set_password`, `_password` set), not when Django upgrades
a hash at sign-in. Such a save for a marked account, from a request acting through a session of
that account, first holds that session (`hold_acting_session`, after the locks a mark takes) and is
refused when it has ended, so a change checked against the old password before the mark cannot
lift it. `MarkedPasswordValidator` compares a new password with each mark's hash. All of this runs
in the application: a process of an earlier release, still serving during a deployment or after a
rollback, neither enforces nor lifts a mark (docs/operations.md).

Each account's sign-in history is a `SignInEvent` row per entry
([accounts/signin_history.py](../accounts/signin_history.py)): the kind (signed in, sign-in
refused, sign-in failed, too many failed attempts, re-authentication failed, signed out), the
factor a sign-in proved or the step an attempt failed at (`SignInFactor`), the session ending, the
session's id (not a foreign key: the session row is state, this is history), and the browser and
address as `accounts.sessions.reported_by` gives them. Triggers added by `accounts` migration 0009
write a row for each `AccountSession` inserted (signed in, or sign-in refused for a row inserted
already ended, which is how a marked password alone is refused) and for each row whose `ended_at`
is first set (signed out), with the session row's own times, ending, browser and address, in the
statement that starts or ends the session. Every path that starts or ends a session, including a
release still running while the migration applies, therefore leaves its entry in the same
transaction; such a release's sessions carry no `factor` (the column's database default is blank),
and their sign-ins show the factor as not recorded. `start_session` stores the factor on the row
from the steps allauth recorded in the browser's session (`sign_in_factor`: the last password or
emailed code and the second factor after it, re-authentications left out), read when allauth
completes the sign-in. After the account's session-ending lock it closes the browser's earlier
session and the abandoned ones (ending one waits for a change under way that holds it), then
holds the account's row and reads it as held: an account deactivated while the sign-in was under
way starts its session ended (`Account deactivated`), refused. A row past a limit is
ended at the moment its limit passed, so its sign-out entry carries that time even when it is
written later. The application writes the other kinds, through `accounts.signin_history.record`:
`accounts.adapter` a password not accepted at sign-in (`authentication_failed`) or on
re-authentication (`reauthenticate`, which decides it), one refused by the failed sign-in limit
(`pre_authenticate`) and the right password or emailed code of a deactivated account
(`pre_login`, for the account it was accepted for); `accounts.signin_log` an emailed code that
was entered, or a second factor, not accepted, from allauth's signals;
`accounts.forms.RecordedLockout` a second factor refused by the limit before it was checked; the
two Change password forms a current password not accepted. A refusal by the limit is written
only when none for the same account, step and address was written in the last minute, under an
advisory lock for the account (`SIGN_IN_LOCKOUTS`), so that two at once take turns; the second
step has no request limit of its own. A password checked by `reauthenticate` belongs to the
account being re-authenticated (the adapter marks the check, not whoever is signed in). A wrong
password at sign-in, or one the limit refused, is recorded only for the one account its username
or email address names in any letter case (`accounts.signin_log.accounts_named`, the sign-in
log's match), and not at all when it names none or several; a wrong code, or one refused at the
code step, belongs to the account already identified there; a refused sign-in to the account
whose password or code was accepted. No history entry holds a submitted identifier, password or
code (allauth keeps the identifier typed among the sign-in's steps in the browser's session until
it signs out). The factor is read by `ClosedSignupAdapter.login` before Django signs the browser
in; a Django `login()` called otherwise records none, and `accounts.compromised` then reads the
browser's session with the same reader. A third trigger refuses `UPDATE` and `DELETE`
on `accounts_signinevent`; `reset_demo` empties it with `TRUNCATE`, which row triggers do not see,
and the account reference has no database constraint (`db_constraint=False`, `DO_NOTHING`): an
entry outlives its account, and writing one inside a session's start or end takes no lock on the
account's row. The table has no model permissions and no admin; the
page (`accounts.session_views.sign_in_history_page`) reads the account's own rows of the last 90
days by the database's clock, at most 100, newest first, with whether the account is a shared
demonstration account in the same statement (`signin_history.recent`), and hides the browser and
address of every row but the current session's for such an account. Where you are signed in reads
that flag with its sessions in the same way (`session_list.reading`).

What a person was typing is kept across a sign-out by a limit ([accounts/kept.py](../accounts/kept.py)).
A full-page form drawn with `{% form_page keep=... %}` carries a hidden token signed for its
account, its session row, the form's key and the names of the fields that may be kept: visible
fields a person types in or chooses from, ticked lists and an item list's quantity boxes
included, never a file, a hidden value such as a revision or one the form lists as holding patient
information, and a list fetched as one types only where the form names it in `kept_fetched` (the
chosen record's id, given back only while the field's own records still hold it); a form with more
than 200 such fields gets no token.
Once the page learns that its session has ended it sends what is typed with the token to
`account/session/keep/`, which needs no session and no CSRF token (the signed token is the proof)
and is not activity. A form posted after its session ended is kept from its post: when the
middleware refuses it, when a view sends it to sign in (with or without a referrer), and when its
CSRF check fails because the person signed in again in another tab. A copy is inserted, in one
statement by the clock when it runs, only when the token's row ended by a limit less than 24 hours
earlier, the account is active, no Sign out came after (an ending recorded by any release, or the
account's discard moment), no copy for that form already given back in that browser came from a
later session, and a typed field holds text; the request form's own fields are read again first,
under a share lock on the form that a change of its fields takes first, and only names that still
exist and hold no patient information are kept. Each copy records the kind of control each value
came from. The first copy for a row and form stands. Each copy is bound to the browser by the
digest of a value in a cookie of its own (`lab_ops_kept`), set with every page that draws such a
form and with every answer that held a copy; a page drawn for a browser without it derives the
value from its session row with a keyed hash, so every answer of that session sets the same value
in whatever order they arrive, and a shared account never hands one person's typing to another.
The cookie lasts as long as a page's token (48 hours). Not covered: in a browser that has no
cookie yet, answers of two different sign-ins arriving in either order leave one sign-in's value,
and a copy kept under the other is not given back; and if the cookie is deleted while two pages of
an ended session send their typing, each gets its own value and only one copy comes back (nothing
on the server can tell which answer the browser kept, and the cookie is HttpOnly). The same form drawn for the same account in that browser on a page opened
with GET (not a prefetch) is drawn with the newest eligible copy as its initial values, before
its fields render, so nothing depends on scripts; a ticked box takes a value only from a ticked
box and only the value a ticked box posts, one answer carries between one choice and several, a
date its input cannot hold (still being typed, or impossible) is handed to the date editor as text
(without scripts the page says it was not put back), and a choice no longer offered or a field no
longer on the form, also one left out when the copy was kept, is left as drawn, and the page says
so (also when nothing could be put back). The copies for that form are locked `FOR UPDATE SKIP LOCKED` and
emptied in a savepoint of the page's reading, so the opted-in pages draw inside a reading and a
page that fails rolls the take back: a page that finds the newest copy held by another page gives
nothing back (never an older copy), and one whose reading began before another committed the take
gets a serialization refusal, rolled back to the savepoint, and gives nothing back either. Discard is a link that opens the page again. A refused
save drawn again gives nothing back. Sign out, and Sign out arriving just after the session passed
a limit with its CSRF check passed, records the account's discard moment and removes its copies after taking a per-account
advisory lock that keeping takes shared, so no copy from an earlier session is inserted
afterwards. The scheduled job removes copies 24 hours after their session ended, skipping one a
page holds at that moment. Copies have no audit event and are never logged. The two addresses a
refused reply is drawn at (`desk:request-entry`, `portal:request-reply`) send a GET on to their
ticket with the list it was opened from, so signing in again from such a page opens the ticket;
the composer posts to an address that carries its queue.

The desk's new ticket keeps its own fields, the ticket form picker and the chosen ticket form's
fields as one copy (`accounts.kept.Parts`, key `staff-ticket`). Its marker is drawn in the category
section, so the htmx request that draws the section again for another category or form issues a
new token naming that form's fields; a section request never gives a copy back. The ticket form's
recheck names the picker as its chooser: a copy whose picker value is not the form whose fields the
token drew (the picker changed and the session ended before the section came back), or whose form
is gone, keeps none of that form's fields (Some required details are not known yet included) and
keeps the rest; without a chooser (the portal's request form page, which is the form's own) a copy
of a form that is gone is not kept. The page takes the copy before it draws anything (`take`), draws
the ticket's own fields from it (`draw`), decides the category, the organization and the ticket form
from what was drawn, as the section would (`request_forms.intake.slot`), and then works out the
marker and draws the copy in every part (`prepare` with the copy taken). The page is drawn in one
reading with the account read in it (the view is not atomic for the request; a save keeps the
request's transaction where the deployment sets ATOMIC_REQUESTS); when PostgreSQL refuses one of the ticket form's share locks
because the row changed after the reading began, the reading is rolled back, the take with it, and
drawn once more. The section is not drawn in one reading (nor in a request's transaction), so that
a change of a ticket form never waits for it: the slot reads the list of forms once, chooses from it,
reads the chosen form's definition after it, and draws that list (a form the definition shows
retired, moved or withdrawn is neither drawn nor listed, and the chosen form is labelled from its
definition, as read between two equal lists). A submission reads the list before and after its
picker and form in the same way, and a refused save is drawn again from that moment (the list, the
form and its category as read then, nothing read again), less a form removed since or not offered
to the request's organization, which the picker then names as the reason for No form. "Some
required details are not known yet" is kept only while the form would draw it. Not covered: a
setup change and its reversal both committed between two reads of one page (a form retired and
restored with other instructions) look like no change, so that page can show the list of one
moment with the instructions of the other; the next submit is checked against the current
definition as always. A refused save lists every form of the category (those for selected
organizations marked), as it did before; the offer to the ticket's organization is checked when it
is saved. When three readings in a row each see a setup change, the form is not drawn and the page
shows the last reading's list with the category as read at its start. A page drawn in its own
reading makes its small writes (the first What's new visit) once
the reading has ended, so a failure of such a write comes after a copy of kept typing was taken. A copy taken for a form grown past 200 fields since is still
drawn, with its notice, without a token. The new ticket is a dedicated form page in the shared form
dialog. A copy held for a form in the dialog makes signing in again open that form's own page: the
keep address answers, for each form sent, whether a copy is held, and the page sends
`data-vr-form-url` of the dialog's page as `next` for a form held; a form posted after its session
ended and kept from its post, from the dialog or from a page the browser does not name, is answered
with a sign-in that returns to the address it was posted to. An htmx request that finds the session
ended (401 `{"signed_in": false}`) no longer goes to sign in at once: the page sends what is typed
first, as at the end of its countdown, also when the element that asked has left the page by then
(the refusal, like the keep address for each form, says whether what that form sent is the copy
held: stored now, or the same answers from the same controls stored before, so another tab's copy
never lets this page go),
and it goes to the sign-in address the server gave when it has no dialog form of its own to return
to (only this site's sign-in page with a `next` on this site). While the dialog has a form
disabled for a request (taken as under way when it is sent, with each control's state from when it
was asked for), the page collects the controls the dialog disabled; while the dialog loads another
page over the form (Discard, a link) it collects nothing of that form; a chosen option disabled by
itself or its group is not collected, as the form would not send it. The kept notice comes from the request that stored
a copy and is not added while it already waits. Not covered: a copy of another form stored after
the sign-in page has shown the notice is announced again, and a repeated delivery whose first
answer was lost is not announced. Once a copy is held for the dialog's
form the dialog lets the page go without asking, and the composer only for its own copy; a refusal
leaves the dialog's question in place until then. An answer without `held` (an earlier release)
counts every form held only when its count covers them all. The dialog announces when it has set up its form (`vr:form-ready`), and the
page's script notes how the form was drawn then and after each htmx swap; a page that held unsent
text when it was drawn counts as unsent after the section is drawn again; Discard inside the dialog
loads the form again in place and puts the focus in its first text box. A Discard load that fails offers Try
again, which repeats it, asking first when the form was edited since; one that answers with no form
(Not found, or the record because the form is no longer offered) opens its address as a page. Not
covered: after a Discard load failed, a Close or a save that is then abandoned (Keep editing, a save
cancelled before it is sent) drops the focus rule, so Try again's form takes the dialog's own focus.

The nonconformance forms keep the revision they post
([nonconformance/views.py](../nonconformance/views.py)). A form that posts the revision of the
record it was drawn from names that hidden field in `kept_revision`; its token carries the revision
as drawn (on a refused save, as posted; a form whose revision is not a whole number gets no token),
and the copy keeps it (`KeptDraft.revision`). A page giving such a copy back draws that revision in
the field instead of the one it read, draws its token again with it, and says when the two differ,
so the save is checked against the record as it stood when the person typed and is refused as
changed since, with the typing kept, when the record changed after it. The copy's revision is drawn
whenever a copy is given back, also when nothing of it fits the form any more; a copy none of whose
answers can be drawn leaves the whole form as it starts (no emptied answer is put back unannounced).
Two deliveries of the same answers count as the same copy whether it was stored without a revision
or with 0. A copy that holds no revision (kept by the
previous release while both ran during an upgrade) is drawn with 0, which no record has, and its
token carries 0, so its save is refused, the page says the version is not known, and the typing can
be kept again. A revision is a whole number up to what the column stores. Each form is kept under a key naming the form and
its record (`nc-12-edit`, `nc-12-action-4-done`, `nc-12-link-3-remove`, `nc-from-ticket-7` with the
ticket's revision, `nc-new` with none). A form page opened with GET is drawn in one reading with the
account read in it and takes its copy before it decides what to draw: a copy typed against an
earlier revision is drawn like a form posted from an earlier reading, also where this reading no
longer allows the change or offers the form (a closed record, a finished action, a removed link, a
complaint whose record was opened or reviewed meanwhile), and the service answers its save; any
other copy is given back only where the form is drawn anyway. A page not drawn after all (a
redirect or a refusal) rolls the take back, so the copy waits for a later visit. A POST is not drawn
in a reading: its service takes its own locks.

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
page, a portal access change, setting or moving the end of a capability given directly, and every
removal of a role, a capability, a team membership or a portal site. It waits for that person's changes under way. The gate queues in order, as row
share locks do not, so a change the person starts meanwhile waits behind it, then reads the new
state and is refused. An access change that affects several accounts, such as the role group
page, takes their gates one at a time in id order: a member it has not reached yet can still
start a change, which is saved before it. Because a person's own change holds their account before any ticket, it and
that person's deactivation or removal from a team wait for each other one way, never in a cycle.
Updates that take no access away, such as the email notice preference or a capability added to a
role, do not wait for the gate. A sign-in takes it shared, after the account's session-ending
lock, so that it and a deactivation never hold what the other needs: it waits for an access change
of that account under way (such as `setup_lab` giving the account a role), and an access change
that starts meanwhile waits for it; the person's own changes, which hold it shared too, are not
waited for (a sign-in that upgrades the stored password hash saves a new password, and waits as a
password change does; an update of the account's row still waits a moment for
a change that holds that row, such as an assignment of a ticket to the person or a notice being
sent to them; `setup_lab`, giving an existing account a role it lacks, takes the gate as the account
page does, so each records only its own additions); a save on the account's page and every save of a portal account's
access take the gate and wait for the person's change under way. A password change, by the
person or on the account's password page, saves the password only: a full save of the account
as read when the request began would put back a deactivation committed meanwhile. It takes the
gate shared, so it waits for an access change under way but not for the person's own change. A save that names only other columns (a sign-in's time) leaves the organization as stored
and never compares it with the copy of the account it read earlier
([clients/signals.py](../clients/signals.py)). The account page carries the account as it was
drawn (a fingerprint of the values it draws: names, email, organization, flags, the roles and
capabilities it holds and the ends of those given directly, `accounts.admin.PAGE_FIELDS`; a role renamed on its own page changes no
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
organization locks the organization in the ticket's place. Attaching a link to a ticket, removing
it, and showing it to the client or hiding it are changes to the ticket and advance its revision;
Show to client and Hide from client, and a ticket's Add link and Remove link pages, send the
ticket's revision of the page they came from; it is checked right after the acting account (and
the capability for a person-level link) is read again, so any change to the ticket since is
refused as stale. A correction of a link's own label or address is a change to the link (its own
revision), not to the tickets that carry it; an organization's link pages carry no such check. An import of external identifiers
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

Notices about a person's sign-in factors use the same outbox under the topic `accounts.security`:
the compromised mark, the admin console's authenticator removal and the person's own two-factor
pages record one notice per change, with event codes, the actor kind, factor types and the time,
and the reference "Account". allauth's Activate, Generate and Deactivate views are served by
`accounts.mfa_views` at allauth's paths and names (`accounts/mfa_urls.py`, included before
allauth's URLs), running allauth's flow in one transaction that locks the account's row first and
records the audit event and the notice with it. At each attempt the handler holds the account's
row shared and sends only while it exists, is active and has an email address, worded from a fixed
allow-list with the security contact read then; the person's notice preferences do not apply.
[Security notices](../accounts/security_notices.py) and [two-factor views](../accounts/mfa_views.py)

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
stored in the job settings, not looked up by name. Its name and address are a base name with a
random suffix drawn when it is created, so a person's account saved at that moment meets neither,
in any letter case, unless it is given that very suffix, which nobody can know before it is
drawn: the old base name or address being registered never delays the first run. A job decides
from records read under its own
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
up without the settings row's lock once it exists; a first breach run that meets the settings
being saved leaves its work to the next run, while the automatic close waits for the save, as
each of its closes does, because it decides from the period in force. Each breach run has its own identifier, which a
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
to individual accounts: stored in the database ([core/preferences.py](../core/preferences.py)),
read once for each request and never copied into the session, because two tabs of one sign-in
can answer at the same moment and each saves its whole session as it ends. A change made in the
page (a column shown or hidden, the sidebar collapsed) is posted at once (`static/js/preferences.js`,
with `keepalive`, so that it completes when the page is left); two changes of one setting made
within a moment of each other, in one tab or two, each write the setting as they are handled: the one
written last is kept, which can differ from the order they were made, and a refusal answered late
can still say a setting was not saved after a later save of it succeeded. Every save compares
with what is stored, not with what the request read: a list's page size and sort, and the What's
new mark, are combined with the stored value holding its row, and a list saves only the page
size and sort it applied with valid filters, once the response is ready (not a sort it does not
offer, not with refused filters, nothing for a lookup or an error). A list's links and forms
re-send only what the person chose on that page, not values added from their saved settings,
so they put back no value the person did not choose there; an empty page size or sort (a filter
form's unset field) counts as not chosen. The staff queue's ticket links and its Save view add
the order the queue showed (`order` in their `list` value), which Previous, Next and the stored
view use, while every way back from the ticket drops it. A sort the queue offers takes precedence
over `order` (in a value the queue builds they are the same); an older address with a typed
`order` and a missing or retired sort follows the `order`. A ticket page carries no `list` value
over 4,000 characters, or with more parameters than a request may carry besides the one `order`;
Save view takes the whole value and stores only valid filters. The queue's own links assume
Django's limit of 1,000 parameters, which this project does not change: a deployment that lowers
it below the queue's filters plus a saved view's id gets refusals from those links. Changes to
several tickets post to the queue's address as chosen and return to it; a refused change is drawn
as a GET of that address after the change (the saved page size and sort, the saved views and the
teams read then), saving nothing. A saved view's order
is applied by the queue, not sent as a sort, so opening the view saves no sort for the person
(an address from before this change that carries the sort is a choice, as it was), and a way
back to the view applies its order again unless the person chose a sort there. The order shown is
the sort chosen on the address, else a saved view's offered order, else the person's saved sort,
else the list's default; a chosen sort the list does not offer shows the default. Saved views' audit events written from this change on record that a view
keeps a search, not its text; events written earlier keep theirs (history is not rewritten). A person keeps at most 300; an
addition counts and inserts holding that person's additions lock, a transaction-level advisory
lock keyed like the access gate, so two at the same moment cannot both take the last place,
while changing or removing a stored one takes no lock. The count needs READ COMMITTED, Django's
default; a save checks its own transaction's isolation and is refused in one with an older
snapshot. The first page's What's new mark is recorded as that page is drawn, for the account it
is drawn for: it can take a last free place before the page's list choice is saved, and it is
kept when the page is then replaced (an address without its trailing slash draws a 404 page
before redirecting) or fails after drawing the top bar, unless the page was drawn inside its
own reading (a ticket page), which discards it, or `ATOMIC_REQUESTS` rolls the request back. A
mark drawn for an account deactivated meanwhile still records, for that account only. The seen
mark keeps at most 400 entry ids; ids of another release's entries, whose dates this process
does not know, are kept until that bound, ahead of a visit's own ids when theirs is the newer
mark. A page drawn for an account removed at that moment, What's new included, draws without
recording its mark (the preferences table's reference to that account; the transaction writes
only that person's rows). A save the preferences refuse (the limit of 300, an isolation other
than READ COMMITTED, a row changed under every attempt) leaves the page as drawn without
recording the visit; a database failure, also of another table's reference, is raised, and
under `ATOMIC_REQUESTS`, which this project does not use, the removal shows only when the request
commits, so that page fails. A stored mark that does not read back exactly as written, or cannot
be written back at all (a JSON number beyond double precision or range, which the application
never writes), is not repaired by a page's first mark: it counts as none until a What's new
visit with entries to mark replaces it. A stored page size that is not a whole number counts as
none, and choosing that size replaces it. A stored value Python cannot read at all (an integer
of more than 4,300 digits, which only another writer can store) fails that person's pages until
it is removed. Processes of an earlier
release take no such lock and keep a copy in the session, so while a deploy replaces processes
one by one the limit and the next-page guarantee hold only between processes of this release.
[UI standards](ui-standards.md)

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
