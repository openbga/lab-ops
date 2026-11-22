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
on that page is a row with a revision, written the first time it is saved: its change holds the
acting account (`accounts.acting`), takes its lock, checks the revision the form showed and the
role as the account is held, requires a reason and records one audit event, as the session limits
do in `core.session_limits`. The same change updates [the safeguards reference](safeguards.md).
[Security settings](../core/security_settings.py)

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
disabled for a request, the page collects the controls the dialog disabled, except while the
dialog loads another page over the form (Discard, a link), when it collects nothing of it; a chosen
option disabled by itself or its group is not collected, as the form would not send it. The kept notice comes from the request that stored
a copy and is not added while it already waits. Not covered: a copy of another form stored after
the sign-in page has shown the notice is announced again, and a repeated delivery whose first
answer was lost is not announced. Once a copy is held for the dialog's
form the dialog lets the page go without asking, and the composer only for its own copy; a refusal
leaves the dialog's question in place until then. An answer without `held` (an earlier release)
counts every form held only when its count covers them all. The dialog announces when it has set up its form (`vr:form-ready`), and the
page's script notes how the form was drawn then and after each htmx swap; a page that held unsent
text when it was drawn counts as unsent after the section is drawn again; Discard inside the dialog
loads the form again in place and puts the focus in its first text box.

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
added to a role, do not wait for the gate (a sign-in that upgrades the stored password hash saves
a new password, and waits as a password change does; an update of the account's row still waits a moment for
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
can answer at the same moment and each saves its whole session as it ends. Every save compares
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
