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
grant to enter, reveal or correct it. A reveal is audited. A portal account may see a
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

## Roles and capabilities

| Account or grant | Access |
| --- | --- |
| Any staff role | Read and work authorized tickets. Log contact, assign tickets, manage non-person links, fill form details and save personal views. |
| Client services or account manager | Maintain client organizations, sites, contacts and portal access. Import organization data. |
| Supervisor | Read restricted tickets across teams, review eligible complaints, withdraw replies or files, read ticket audit history, share saved views and manage team membership. |
| Administrator role | All operational roles plus laboratory settings, teams, taxonomy, forms, catalogs, external systems, business calendars, targets and scheduled jobs. |
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

Each notice is recorded with the change that raised it and sent once that change is saved. A
notice the mail server does not accept is retried by the scheduled jobs, after 5, 15 and 30
minutes and then 1, 2, 4 and 8 hours, and marked failed after the eighth attempt. Before every
attempt the application checks again that the recipient may still read the ticket and that the
event still applies: a reply not withdrawn, the ticket still waiting on the client or still
resolved, the person still the assignee or still responsible for a passed target. It holds what
that check read until the notice is sent, so a change made meanwhile waits for it; a notice whose
ticket or recipient is being changed at that moment is tried again a minute later. With email off
when a change is saved, its notices are recorded as not sent and are never sent later.

### Scheduled jobs

One command, `manage.py run_scheduled_jobs`, run by the deployment's scheduler every few minutes,
retries notices, sends service target notices and closes resolved tickets after the laboratory's
period. Without a scheduler, notices are still sent right after each change, but nothing is
retried, no target notice is sent and nothing closes automatically. The Scheduled jobs page shows
administrators when each job last ran, warns when no run happened in the last 30 minutes, and
lists notices that failed or wait to be retried. Changes the jobs make are recorded under a
"Scheduled jobs" account that cannot sign in. The demonstration has no scheduler.

## Record keeping and laboratory policy

The laboratory sets its own targets, retention policy and procedures. The software records
actions and changes with an actor and time. Ticket history, recorded answers, complaints and
uploaded files can contain sensitive information. The demonstration uses synthetic records.

Two US references explain design decisions in this release, checked November 18, 2026.
Complaint records provide space to document a reported problem and a decision on investigation.
[42 CFR §493.1233, General Laboratory Systems](https://www.govinfo.gov/content/pkg/CFR-2024-title42-vol5/pdf/CFR-2024-title42-vol5-part493-subpartK.pdf)

For a US deployment that handles protected health information, assess the role of the hosting,
backup and other service providers and which business associate agreements are needed.
[HHS guidance on business associates](https://www.hhs.gov/hipaa/for-professionals/privacy/guidance/business-associates/index.html)
and the [operations guide](operations.md) cover the legal context and deployment tasks respectively.
