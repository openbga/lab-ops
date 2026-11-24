# OpenBGA Lab Ops product scope

OpenBGA Lab Ops is a client-services desk for laboratories. Each installation serves one
laboratory and its client organizations. Staff handle calls, pickups, supply orders, questions
and complaints in one ticket queue. Clients can ask for a service, exchange messages with the
laboratory and follow their tickets from a phone or desktop.

A ticket can concern an organization, a site, a contact or an internal issue. It can refer to
an order, accession, sample, report or invoice in another system. Testing, result reporting,
billing and critical-result notification stay in the laboratory's own systems. Patient and
specimen records are optional. See the [workflow guide](workflows.md) for staff and client
tasks and the [roadmap](roadmap.md) for planned work.

## Client records

Staff keep each organization's account code, sites, contacts, parent organization, account
owner and external identifiers. Contact responsibilities and sites help staff reach the right
person. A contact and a sign-in account are separate records. Staff can preview and import
organizations, sites, contacts and external organization identifiers from CSV files. Each file
is applied as one change. Importing a directory never grants someone portal access.

## Ticket taxonomy and presets

A category groups similar tickets and proposes a priority and team. It can also flag complaints,
restrict visibility and offer a client-facing label for the portal. Administrators can load
common categories and add clinical, toxicology, environmental, manufacturing quality control
or biorepository presets. Loading a preset again preserves local edits. Laboratories set their
own teams, intake guidance and service targets.

## Tickets, life cycle and rules

Each ticket has a permanent number, a team, a category, a status and a priority. Its assignee,
if any, must be an active member of its team. When the assignee leaves the team or their
account is deactivated, work that is still open or resolved returns to the team's queue.
A restricted ticket appears only to the responsible team's members and supervisors on staff,
and only to the account that raised it in the client portal.

| Status | Who acts next |
| --- | --- |
| Open | The laboratory works the ticket. |
| Waiting on client | The laboratory needs information or action from the client. A client reply reopens it. |
| Waiting on third party | The laboratory awaits someone outside the conversation. |
| Resolved | Staff have recorded an internal resolution summary. A client reply reopens it. Staff can reopen it with a reason. |
| Closed | Work is final. The client can open a linked follow-up ticket. |

An administrator can have resolved tickets close automatically a set number of days after they
were resolved (from 1 to 365; none by default). The scheduled jobs close them under the same
rules as closing by hand, so a complaint stays resolved until its review counts. The timeline
names the "Scheduled jobs" account and the period, and a page of the ticket opened before the
close can no longer save.

### Entries and visibility

A staff reply appears in the client conversation. Internal notes and logs of calls and other
outside contacts stay with staff. The application records which capacity the author acted in.
Entries keep their original text. A supervisor can withdraw a reply or a file from client view
with a reason. A file inherits the visibility of its entry and the ticket. A client can
download a file only while the file and its parent reply are both visible.

### Complaints and feedback

Staff can mark a ticket as a complaint. The assignee or a supervisor records whether to
investigate, the rationale, findings, actions and communication of the outcome. Closing a
complaint requires a review by a supervisor who has never been its assignee and has never
recorded or revised its investigation decision. The application checks independence again when
closing. Assign one account to each person so the review history identifies the individual.

### Ticket forms

Laboratories can create several forms for one category. Each form has a client label, instructions
and fields, including choices, dates, numbers, confirmations and catalog items with quantities.
Fields distinguish answers supplied by clients, details staff share with clients and staff-only
details. A form can be available to all organizations or restricted to selected ones. Staff
taking a call can record that a required detail is missing and fill it later.

Tickets preserve the form and field labels and values as captured. Staff correct a value with
a reason and can see the earlier record. A change to a form's definition requires the person
entering answers to review the current form. A closed ticket's form values are frozen.
Deactivating a form, field or item preserves historical tickets. **Request again** copies only
eligible client answers from a ticket the account may read. Dates, times, confirmations and
patient information must be entered again.

A laboratory can mark a form field as holding patient information. Staff need an explicit
grant to enter, reveal or correct it. A reveal is audited. Where the laboratory turns it on, the
first reveal in a session also asks the person to confirm it is them (Security settings and
contacts, below). A portal account may see a
patient-marked value if it may read the ticket and the field was, and still is, visible to
clients. Enable those fields only when that audience should see the information.

### Links to external records

A link stores a source system, type and identifier. The same source record can be referenced by
multiple tickets or organizations. Non-person links can have an HTTPS address on their system's
configured host. Staff choose which links a client sees. Removing a link keeps its history.
Links on a closed ticket are frozen.

A ticket can have one person-level link for a patient, participant or donor. Its key is masked,
has no URL and is never shown to clients. Showing the full key or searching for an exact key
requires the patient-information capability and creates an audit event. Prefer an order,
accession or sample reference when a person-level key is unnecessary.

### Service clocks

First-response and resolution clocks use the laboratory's targets. The most specific applicable
target wins, in order of category and priority, category, category group and priority, group,
priority, then the general target. Each target can use business hours and holidays or run
around the clock. Tickets with no applicable target have no clock.

A staff reply or a logged contact that reached the client meets first response. A phone call or
walk-in logged at intake meets it immediately. The resolution clock pauses while waiting on the
client, and while waiting on a third party if the target allows that pause. Time spent resolved
is excluded if the ticket reopens. Changing category or priority recalculates outstanding clocks.
Editing a target alone does not move existing due times.

When a running clock passes its due time, the scheduled jobs send one email notice to the
assignee and the leads of the ticket's team, or to every member of the team when it has neither.
A clock gets one notice each time it runs past its due time: resolving and reopening a ticket,
or a change of category or priority that moves a due time, starts that clock again. At the due
time itself a target is still met; a notice waiting to be sent from an earlier run of the clock is
dropped. Clocks already past due when a deployment installs this
release get no notice for that breach. The ticket itself does not change.

## Optional modules

An administrator switches optional modules on or off on the laboratory profile, with a reason.
The change is recorded in the profile's history. A module that is off shows no page, menu entry,
link or action, and its addresses do not exist. Its records are kept and reappear when it is
switched on again. History elsewhere keeps what it recorded: a ticket's timeline and a complaint's
record still show the number of a nonconformance, without a link. The desk works with every
module off. A new installation starts with every module off.

### Nonconformance and corrective action

A nonconformance records a problem in the laboratory's work. It has a permanent number such as
`NC-000123`, a title, what happened, a source (a complaint, an internal finding, an audit finding,
proficiency testing, equipment, an external assessment, a preventive action or improvement, or
other), a severity of minor, major or critical as the laboratory's procedures define them, and an
optional area. Every staff role can raise one and read every record. A supervisor chooses its
owner. The owner or a supervisor works it.

| Status | What happens |
| --- | --- |
| Open | Raised. A supervisor chooses the owner. |
| Investigating | Containment, root cause and whether similar cases exist are recorded. |
| Corrective action | Actions are carried out, each with a person and a due date. |
| Verification | The owner or a supervisor checks whether the actions worked. |
| Closed | A supervisor who is not the owner has closed it. |

The record holds the decision on results the laboratory has already reported: none affected,
some affected, or not about results, with the orders and reports that were reviewed. When
reported results are affected, it records whether the client was told and, if so, when, how and
by whom. Containment, root cause, similar cases and this decision can be revised while the
record is open. Each revision is kept in the history.

An action's assignee, the owner or a supervisor marks it done with what was done. The owner or a
supervisor changes an open action with a reason, or cancels it with a reason. An action is
overdue after its due date. Lists show open records, records the reader owns or holds an open
action on, and open or overdue actions.

The effectiveness check is recorded in Verification: effective, not effective, or no corrective
action needed. Effective needs every action finished. Moving a record out of Verification, or
adding or changing an action, clears its check, and the history keeps it. A supervisor who is not the owner closes the record once it
is in Verification with the decision on reported results recorded, the client notice recorded
when results are affected, no open action and a check of effective or no corrective action
needed. The closing form shows what is being closed. A supervisor can reopen a closed record with
a reason. It returns to Investigating without its closing or check.

The assignee or a supervisor can open a nonconformance from a complaint's record while the
complaint can still change. The complaint then shows the record's number in place of a
free-text corrective action reference, and its review shows that number. A reference recorded
earlier stays as it was. A record can
also be linked to other tickets. Linking, unlinking and opening from a complaint add an event to
the ticket's timeline. A linked ticket shows on the record only to staff who may open that
ticket. Raise records about restricted tickets with care: every staff member reads every record.

Refer to orders, samples and reports by their numbers. Do not enter patient names or
identifiers in a nonconformance. Its text is shown to all staff and kept in its history.
A summary page counts records by month raised, source and area. As on every list, the words
typed in a list's search box go into the page's address, so a search can be bookmarked; the
application's request log records the page only, without its search.

## Roles and capabilities

| Account or grant | Access |
| --- | --- |
| Any staff role | Read and work authorized tickets. Log contact, assign tickets, manage non-person links, fill form details and save personal views. |
| Client services or account manager | Maintain client organizations, sites, contacts and portal access. Import organization data. |
| Supervisor | Read restricted tickets across teams, review eligible complaints, withdraw replies or files, read ticket audit history, share saved views and manage team membership. Choose the owner of a nonconformance, close one they do not own and reopen one. |
| Administrator role | All operational roles plus laboratory settings, security and privacy contacts, the Security settings page, optional modules, teams, taxonomy, forms, catalogs, external systems, business calendars, targets and scheduled jobs. |
| Superuser | Manage identities and explicit grants through the admin console. The staff workspace still requires a role. |
| Can view patient information | An explicit grant to a staff account or group. It permits protected person-link actions and protected form-field actions on authorized records. |
| Client account | Raise, read and reply to tickets within its portal scope. |

## Client portal rules

A portal account belongs to one organization. It can be granted access to the whole
organization or to listed sites. It can also read tickets it raised in its current organization.
Without an effective access grant it sees only those tickets and raises tickets without a site.
Moving an account to another organization resets its access. Client accounts have no staff
roles. Replies from staff identify the team and the laboratory.

## Notifications

When email is configured, client recipients with access can receive a notice after a staff
reply or a change to Waiting on client or Resolved. Assignment and client replies can notify the
assignee, and a passed service target notifies the people responsible (see Service clocks).
Notices use the ticket number, a fixed event description, the laboratory's name and a sign-in
link. Conversation text and attachments stay in the application. Staff may disable their own
notices. The demonstration disables email.

The account holder also gets an email notice about a change to how their account signs in: when a
superuser marks their password as compromised, or removes their authenticator app or recovery
codes in the admin console, and when an authenticator app is added (one notice, which says new
recovery codes were issued with it), new recovery codes are issued (earlier ones stop working) or
the app is removed with its recovery codes on the person's own two-factor pages. The notice names
the laboratory, the account's username, what changed and when (in UTC), and says "an
administrator" where a superuser made the change. It tells the person to contact the laboratory's
security contact, with the name, email address and phone number on the profile when the notice is
sent, or "your laboratory's administrator" when none is recorded, if they did not make or expect
the change. After a compromised mark it also says what to do next: sign in with the second factor
and choose a new password, or use Forgot your password? (or an emailed sign-in code, where offered)
without one, or that a new password has been chosen since. It carries no code, secret, network
address, browser or ticket content, and its only link is the site's address. These notices are
sent whatever the person chose under Email notices, and are not sent to an inactive account or one
without an email address when the notice is sent. A password changed by the person or set by a
superuser, and a change of email address, send no notice.

Each notice is recorded with the change that raised it and sent once that change is saved. A
notice the mail server does not accept is retried by the scheduled jobs, after 5, 15 and 30
minutes and then 1, 2, 4 and 8 hours, and marked failed after the eighth attempt. Before every
attempt the application checks again that the recipient may still read the ticket and that the
event still applies: a reply not withdrawn, the ticket still waiting on the client or still
resolved, the person still the assignee or still responsible for a passed target. It holds what
that check read until the notice is sent, so a change made meanwhile waits for it; a notice whose
ticket or recipient is being changed at that moment is tried again a minute later. A notice about
sign-in factors is checked against the account instead: it still exists, is active and has an
email address. With email off
when a change is saved, its notices are recorded as not sent and are never sent later.

### Scheduled jobs

One command, `manage.py run_scheduled_jobs`, run by the deployment's scheduler every few minutes,
retries notices, sends service target notices, closes resolved tickets after the laboratory's
period and removes typing kept across a sign-out once its 24 hours have passed. Without a scheduler, notices are still sent right after each change, but nothing is
retried, no target notice is sent and nothing closes automatically. The Scheduled jobs page shows
administrators when each job last ran, warns when no run happened in the last 30 minutes, and
lists notices that failed or wait to be retried; a notice about a person's sign-in factors is
listed as an Account notice, without saying whose account it is. Changes the jobs make are recorded under a
"Scheduled jobs" account that cannot sign in. The demonstration has no scheduler.

## Security settings and contacts

The laboratory profile records the laboratory's security contact and privacy contact: a name or
office, a title, an email address and a phone number. Every staff member reads them on the
profile. An administrator records, changes or clears a contact with a reason, and the change is
kept in the profile's history with the details before and after. The application designates no
one and checks nothing against a contact. Email notices about a person's sign-in factors name the
security contact as whom to contact.

The Security settings page, for administrators, lists what protects sign-in, sessions and patient
information in the running deployment and who sets each item: this release's code, the
application's configuration, a deployment setting, each person, the admin console or a setup page.
It shows the password rule, the failed sign-in limit, sign-in with an emailed code, what changing
access and security settings asks for, the session limits and what ends a session, how many active staff accounts with a role can view patient information,
whether staff confirm it is them before the first reveal of patient information in a session,
how many active staff and portal accounts have a second factor, how many form fields hold patient
information, and the HTTPS and content security policy settings. It describes this release's
configuration and follows the deployment settings read from the environment; where a deployment
has changed a setting in `config/settings.py`, the item says so and names the setting. It
reports facts and gives no score. Administrators set the session limits on this page, separately
for staff and for client portal accounts: after how long without activity a session is signed out
(5 to 60 minutes) and how long a session may last at most (1 to 24 hours), starting at 30 minutes
and 12 hours. Each change needs a reason and is kept in the page's history with the values before
and after. The laboratory chooses the periods; the application does not.

Administrators also choose on Security settings which accounts must have a second sign-in factor:
every staff account, staff with chosen roles (administrators do every role's work, so any role
covers them), staff who can view patient information, accounts that can open the admin console,
and client portal accounts. Nothing is required until the rule is first saved, with a reason, and
each change is kept in the page's history. Whether the rule covers an account is decided when it
signs in: an account it covers that has no authenticator app is taken to Set up a second sign-in
factor and opens nothing else until it adds one (recovery codes alone do not count), then goes on
to the page it was opening. Sessions the rule did not cover when they signed in are not interrupted
when it changes. A session it covered is sent to set up a factor whenever its account has none while
the rule still covers it, for example after the app is removed. A
shared demonstration account cannot add one. The panel counts the active accounts the rule covers
and how many have no second factor yet.

Pages warn 2 minutes before signing out for no activity, with Stay signed in, and 5 minutes before
the longest session ends; the admin console does not warn. Opening a page, saving, searching or Stay signed in counts as
activity; reading or typing on a page does not. Signing in again returns to the page. When a limit
signs the person out, what they were typing in a ticket's reply, note or logged contact, in a new
ticket, in a reply in the portal, in a new portal ticket, or in a nonconformance form (the record,
its investigation, impact, status, owner, effectiveness check, closing and reopening, its actions
and linked tickets) is kept for 24 hours, in the database
and for that account in that browser only: it comes back in the same form the next time they sign
in there and open the page, with a notice and Discard, and also when they press Send after the
sign-out. Ticked choices and quantities come back with the text. A new ticket comes back with its
category and that category's intake guidance, the ticket form chosen with its fields, and its
organization, site and contact while they are still active; one deactivated since, or a ticket form
no longer offered, is left out and the notice says that something was not put back. A new ticket
typed in the form dialog over the ticket list opens again on its own page when they sign in again,
and so does a nonconformance form typed in the dialog over its record. A nonconformance form keeps
the version of the record (or, opened from a complaint, of the ticket) it was typed against: when
the record changed since, the page says so and saving is refused with the typing kept on the form,
as for any form opened before a change, so nothing typed is saved over a later change; it comes
back even where the form is no longer offered, such as after the record was closed. Files they had chosen, fields that hold
patient information and typing in any other form are not kept. Sign out discards whatever was
kept. Signing in again after a reply was refused opens the ticket. Deactivating an account ends its sessions at once,
and a new password ends the account's other sessions. Pages shown to a signed-in person are
marked not to be stored by the browser.

A new password needs at least 15 characters, or 8 for an account the laboratory's second-factor
rule covers when the password is chosen, and is refused when it is a common password, a common
password (where it has at least four letters) or a name of the laboratory, the application or the
account with only digits, symbols, spaces or letter case changed, one character or a text of fewer
than 15 characters repeated, a run such as 123456789, or close to the account's username, names or
email address as they stand when it is checked. Nothing asks for particular kinds of
characters and passwords never expire. Each form that sets a password, on the account pages,
through an emailed reset link and in the admin console, states the rule for its account; the
`setup_lab` and `changepassword` commands and the demonstration's seed check it too. A password
chosen earlier keeps working until it is next changed. A reset link mailed to an account does not
work while the account is a shared demonstration account. Failed sign-ins are counted in the
database, so a deployment running several server processes counts them together rather than in each
process. Django's own `createsuperuser` command checks the password rule only when it asks for
the password, and lets its operator go past it; `setup_lab` always checks it.

A superuser marks an account's password as compromised from its page in the admin console, with a
reason, when someone else may know it: for example, the person typed it into a page posing as the
laboratory's. Every session of the account ends at once, and the password no longer signs the
account in on its own: a sign-in with it alone is signed out at once, with a notice saying what to
do. A sign-in that also gives the account's authenticator code, or a code emailed to its address,
opens only Choose a new password until a new password is saved there, after setting up a second
sign-in factor first where the laboratory requires one of the account and it has none. An account without a second
factor gets a new password through Forgot your password?, where the deployment sends email, or from
a superuser on its password page. Any new password lifts the mark, and the marked password is
refused as a new one, then and later. Superusers cannot mark their own password, a shared
demonstration account's or that of an account without a password, and a password changed after the
marking page was opened is not marked. Where the deployment sends email, the person is emailed
that their password was marked and what to do next (Notifications, above). Each
mark is kept, with who made it, when, the reason and when it was lifted, and is recorded in the
audit history without the password.

Every signed-in person, staff or client portal, opens Where you are signed in from the account menu.
It lists their own open sessions with the browser and the address each one reported when it signed
in, when it signed in and when it was last active, and marks the one they are using. They can sign
out any other session, or all of them at once; that browser goes to the sign-in page at its next
request, with a notice that it was signed out from another session. A shared demonstration account
sees only its own session there, because the others belong to other visitors. A superuser ends every
session of an account (on their own account, every one but the one they are using) from its page in
the admin console, with a reason, for example when someone leaves or loses a device; the person can
sign in again unless the account is also deactivated. Each ending is kept in the audit history with
the sessions ended, without their browsers or addresses.

Every signed-in person, staff or client portal, also opens Sign-in history from the account menu,
or from Where you are signed in. It lists their own entries of the last 90 days, at most the 100
most recent, newest first: each sign-in with what it proved (the password or an emailed code, and
the authenticator app or recovery code that followed), each sign-out with why the session ended,
attempts that failed (a password, an authenticator or recovery code, or an emailed code not
accepted), attempts refused by the failed sign-in limit before the password or code was checked,
sign-ins refused because the account is deactivated (also when it was deactivated while the
sign-in was under way) or the password alone was given while marked as compromised, and a password
or code not accepted when a signed-in page asked for it again (the re-authentication pages, Confirm
it is you, and the current password on Change password). Each entry shows the browser and the address the request
reported; a sign-out shows those its session reported at sign-in. A warning above the list counts
the failures and refusals shown. A wrong password, or one the failed sign-in limit refused, is kept
only when the username or email address given names exactly one account; one that names no
account, or several, stays only in the sign-in log. A refused sign-in is kept for the account whose
password or emailed code was accepted. Refusals by the limit are kept at most once a minute for the
same step and address. No entry holds a password, a code, or the username or email address as
typed. A shared demonstration account sees its entries, with
the browser and address only for the session it is using. Entries are kept with no retention
period and are never changed or deleted; nobody reads another person's history in this release.
The history starts at the upgrade that added it.

Before a change to access or security settings, the application asks the person making it to
confirm it is them: their password, and a code from their authenticator app or a recovery code
where their account has one (an account without a password gives the code alone). In the admin console it asks a superuser who adds an account or saves
an account's page, its password page or Mark as compromised, removes an authenticator, or adds or
saves a role; on Security settings it asks an administrator who saves the session limits, the
second-factor rule or the confirmation before revealing patient information. Once they have confirmed, they are not asked again in that browser for 5
minutes, unless their password or authenticator app changes meanwhile; signing in does not count,
and each factor given on a page of its own counts for 5 minutes from when it was given. Without the confirmation nothing is saved and no change is
recorded, and what else they typed, new passwords aside, stays on the form. A password or code not accepted is kept in
their Sign-in history and counts toward the failed sign-in limit; what they type is never stored or
logged. An account with neither a password nor an authenticator cannot make these changes. Ending an
account's sessions does not ask, so it stays quick when a device is lost, and neither do the
laboratory's profile, contacts, logo, certificates, modules, retention statement or announcements.
A person's own Change password asks for the current password instead, and their own two-factor
pages ask with their own confirmation, which counts here when it covers every factor the account
has.

Administrators can also ask staff to confirm it is them before the first reveal of patient
information in a session. It is off until an administrator turns it on, on Security settings, with
a reason, and each change is kept in the page's history. When it is on, the first Reveal of a
patient-marked answer or Reveal key of a person-level link in a browser session takes the person
to Confirm it is you, with the same password and code as above; nothing is read, revealed or
recorded until they confirm. They then return to the page they were on and choose Reveal again.
Later reveals in that session are not asked until it ends: Sign out, a new sign-in, a session limit,
a new password set for the account or a deactivation. A confirmation given in that session in the
last 5 minutes for another change counts; the sign-in does not. Failed confirmations are kept in
Sign-in history and count toward the failed sign-in limit, and an account with neither a password
nor an authenticator cannot reveal while it is on. Staff without the patient-information grant who
send a reveal are asked first too, then refused, so the answer says nothing about the value. It is
not asked before a search by a person-level key, or for what a client portal account sees, which is
shown without a reveal. Each reveal is recorded whether or not it is asked. No rule the
application follows requires this confirmation; it is the laboratory's choice.

The [safeguards reference](safeguards.md) states,
for each safeguard the roadmap cites, what the application does today and what stays with the
laboratory and its host.

## Record keeping and laboratory policy

The laboratory sets its own targets, retention policy and procedures. The software records
actions and changes with an actor and time. Ticket history, recorded answers, complaints and
uploaded files can contain sensitive information. The demonstration uses synthetic records.

These US references explain design decisions in this release, checked November 18, 2026.
Complaint records provide space to document a reported problem and a decision on investigation.
[42 CFR §493.1233, General Laboratory Systems](https://www.govinfo.gov/content/pkg/CFR-2024-title42-vol5/pdf/CFR-2024-title42-vol5-part493-subpartK.pdf)

The nonconformance record provides space for the elements that US laboratory rules and
laboratory standards describe for corrective action: what was done, an evaluation of results
already reported, telling the client, the cause, the actions and a review of their
effectiveness. It does not decide what a laboratory must record or when.
[42 CFR §493.1282, Corrective actions](https://www.ecfr.gov/current/title-42/part-493/section-493.1282),
[42 CFR §493.1239, General laboratory systems quality assessment](https://www.ecfr.gov/current/title-42/part-493/section-493.1239)
and [42 CFR §493.1291(k), Test report](https://www.ecfr.gov/current/title-42/part-493/section-493.1291),
read November 17, 2026. Laboratories accredited to ISO 15189 or ISO/IEC 17025 will find the
corresponding elements in their clauses on nonconforming work and corrective action. No source
names who closes a record. Closing by someone other than the owner is this product's choice.

For a US deployment that handles protected health information, assess the role of the hosting,
backup and other service providers and which business associate agreements are needed.
[HHS guidance on business associates](https://www.hhs.gov/hipaa/for-professionals/privacy/guidance/business-associates/index.html)
and the [operations guide](operations.md) cover the legal context and deployment tasks respectively.
