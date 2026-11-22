# OpenBGA Lab Ops roadmap

This roadmap is the one list of planned work. It lists what is built, what comes Next and Later in
the order the project intends to take it, and what is only being explored. It carries no dates, and
the order can change as laboratories use the software. Reviewed 2026-11-18.

[Product scope](product.md) describes what the application does today and [workflows](workflows.md)
how staff and clients use it. This page holds only what is planned.

## What the planned work claims

Lab Ops records what people did and refuses steps taken out of order. It does not make a laboratory
compliant, accredited, certified or validated, and no planned item should be read, built or
described that way. Say what the software records or checks.

Several items add safeguards for patient information that a laboratory subject to HIPAA can use. The
laboratory's risk analysis, policies, workforce training, agreements, hosting, backups and physical
safeguards remain its own, and hosting and deployment are outside this roadmap. Intervals,
approvers, deadlines and retention periods are the laboratory's settings. The product ships no legal
deadline as a default. A proposal, or a rule that is not yet in force, is named as such.

Quality and privacy records are optional modules. A laboratory chooses which to switch on. A module
that is off shows no page, menu entry or link, and keeps the records it holds. The desk works with
every module off.

## How work is taken from this list

Each item says what it delivers, what it depends on and what it deliberately leaves out. Where a
rule, a standard or a published recommendation is the reason for an item, a Basis line names it, and
[Sources](#sources) gives the texts and the dates they were read. Read the cited text again before
building, because rules change.

An item is ready when everything under "Depends on" is built and it carries no "Skip until" line.
Take the first ready item in Next, then in Later, and never an Exploring item. A "Skip until" line
means the whole item waits for something only the maintainers or a real laboratory can supply: a
policy decision, real-world input, new spending or new access. Skip it and go on to the next ready
one. Where only a part of an item waits, that part is listed under "Leaves out" with what it waits
for, and the rest is built. Where an [open decision](#open-decisions) has a default, build to the
default.

An item is done when what it delivers works on the public demonstration with synthetic data, with
page help, tests and a changelog entry. Where an item says the demonstration lacks something it
needs, such as a scheduler or a mail route, the tests stand in for it. Things under "Leaves out" are
not part of it. A large item can ship as more than one change. Each change must be useful on its
own, and the item stays in place, with what remains stated, until the last one ships. When an item
ships, move it to Built in the same change.

## Built

| Area | What is in place |
| --- | --- |
| Client organizations | Organizations with sites and contacts, their identifiers in other systems, a per-client timeline, and import from CSV files with a preview; an import takes turns with new organizations, with changes of an organization's name or code or of the group it is placed in, and with changes of a site's code, instead of failing; an organization's account owner is checked as the person stands when the save or import is made |
| Client-services desk | Tickets logged from a call, email, fax or visit, or raised in the portal; teams and assignment; client-visible replies, internal notes and logged contacts, with files; statuses, priorities and assignment of several tickets at once, where an assignment or a client's reply and the assignee's deactivation or removal from the team at the same moment wait for each other instead of failing; each change holds the acting account until it commits, so ending a person's access (deactivation, a role, a capability, a team or a portal site) waits for what they are saving and refuses what they start afterwards; a ticket's page is read at one moment, so every form on it matches what it shows; saved views and Find; a ticket opened from the queue keeps the order the queue showed for Previous, Next and a view saved from it, while its way back, and the return after a change to several tickets, send only what the person chose, so the queue opens with their saved sort and page size as they stand; a saved view opens in its own order without changing the person's saved sort; restricted categories; complaints with a recorded decision and an independent supervisor's review |
| Ticket forms | Forms per category with client, shared and staff-only fields and an optional patient field behind a capability; corrections with a reason; a service menu in the portal and "Request again"; supply orders from the laboratory's catalog and one-time pickups |
| Links | Typed references to orders, samples, reports and other records in the laboratory's systems, and optional, masked person-level links; attaching, removing, showing or hiding a ticket's link is a change to the ticket, so a change sent from a page of the ticket opened before it, its link pages included, is refused (a correction of form details checks the fields it changes) |
| Service clocks | First-response and resolution targets by category and priority on the laboratory's calendars, paused while waiting on the client |
| Client portal | Each portal account raises, follows and replies to tickets for its organization or its sites; a reply refused because the laboratory closed the ticket meanwhile is shown again to copy into a follow-up; a ticket's page is read at one moment, so its reply form always matches the conversation it shows |
| Notifications | Email notices of replies, status changes and assignments that carry no ticket content; staff can turn off their own |
| Safeguards | Roles and an explicit patient-information capability checked on the server; attributed, append-only change history; masked person-level keys and audited reveals; optional authenticator sign-in with recovery codes; limits on failed sign-ins; file inspection and checksums; HTTPS, a content security policy and logs that carry no ticket content; sign-out after a period without activity and after a longest session, set by an administrator for staff and for portal accounts, with a warning and Stay signed in; deactivation and a new password end sessions at once; a page where each person sees where they are signed in, with the browser and address each session reported, and signs out the others; a superuser ends a person's sessions from the account administration, with a reason; signed-in pages marked not to be stored by the browser; an account page that refuses a save when the account changed after the page was opened, and password changes that save only the password; a Security settings page that lists what protects sign-in, sessions and patient information and who sets it; the laboratory's security and privacy contacts on its profile; and a [safeguards reference](safeguards.md) of what the application does for each safeguard this page cites |
| Platform | Page help on every page, announcements, laboratory certificates and a retention statement, What's new, and a synthetic demonstration; each person's list layouts, page sizes, sidebar state and What's new visits are saved for their account and read for each page, so a change in one tab or on one workstation shows on the next page in the others, and the limit of 300 saved preferences holds when two are added at the same moment |
| Quality records | An optional nonconformance and corrective action module, switched on by an administrator: records with source, severity, area and owner, five statuses, containment, root cause, the decision on reported results and the client notice, actions with a person and a due date, an effectiveness check, closing by a supervisor who is not the owner and reopening with a reason; opened from a complaint or linked to tickets; lists of open, mine and overdue work and monthly counts by source and area. The orders and reports reviewed are recorded as text; linking them as external records is not built yet |
| Scheduled jobs | One management command, run by a scheduler every few minutes, that sends email notices from a durable outbox with retries, tells the people responsible, as they stand when it decides, when a service clock passes its due time and, when an administrator sets a period, closes resolved tickets; an administrators' page shows the jobs' last runs and notices that need attention. The jobs act as an account of their own, named with a random suffix nobody can know in advance, so a person's account registered at the same moment under its old name, in any letter case, neither delays its first use nor ends up differing from it only in case. The public demonstration has no scheduler and keeps email off |
| Release checks | On every pull request and weekly: static analysis of the application code and its workflows, an audit of the locked and vendored dependencies for known vulnerabilities, a scan of the history for committed secrets and a scan of the container image; a software bill of materials of the image for each run and each published release; the security policy says what is checked and what is not. Penetration testing, a bug bounty and scanning of anyone's deployment are left out |

## Why this order

A follow-up to an independent review fixes something already built, so it comes first.

The items on sign-in, sessions, access records, review and emergency access come before anything
else, because a laboratory that handles patient-linked tickets needs them before real use. Today a
second sign-in factor cannot be required, what was typed is lost at an automatic sign-out,
opening a ticket or downloading a file leaves no record, failed sign-ins are kept only in the
server log, and nobody can review activity across records. Each of these answers a safeguard the
HIPAA Security Rule names, and none needs anything unbuilt.

Nonconformance and corrective action, the first of the quality modules, is built, with the module
switch the others reuse, and so are the scheduled jobs that reminders, retries and sealed audit
records need. Controlled documents come before training, because an acknowledgement and a training record
point at a document version.

The privacy records, encryption inside the application and tamper-evident audit records complete the
safeguards. They follow the named modules because the incident record reuses corrective actions and
the audit seal needs the scheduled job. Email comes after the outbox it depends on.

Later starts with time worked on a ticket, which needs nothing unbuilt, and with what completes the
modules above, then the desk features staff use daily, then the remaining quality records in the
order their dependencies allow. Integration, single sign-on and the fuller supply and pickup work
matter once a laboratory uses the desk every day, so they come last.

## Next

### Automatic sign-out and session control

Partly built: sign-out after a period without activity and after a longest session, set by an
administrator on the Security settings page within fixed bounds (5 to 60 minutes and 1 to 24 hours),
separately for staff and portal accounts, starting at 30 minutes and 12 hours; a warning before
sign-out with Stay signed in, and before the longest session ends; signing in again returns to the
page; deactivating an account or changing its password ends its sessions at once; every signed-in
page is marked not to be stored by the browser; a page where each person sees where they are signed
in, with the browser and the address each session reported at sign-in, and signs out the others; a
superuser ends a person's sessions from the account administration, with a reason. What remains:

- **Delivers:** what the person was typing kept, so that signing in again after a sign-out for
  inactivity returns them to it.
- **Basis:** 45 CFR §164.312(a)(2)(iii) (automatic logoff, addressable; the rule sets no period);
  §164.308(a)(3)(ii)(C) (termination procedures, addressable); NIST SP 800-63B-4 §2.2.3 (a
  recommendation).
- **Depends on:** security settings and safeguards reference.
- **Leaves out:** device management and limits by network address, which belong to the deployment;
  a warning in the admin console, which keeps Django's own pages.

### Required two-factor sign-in and password rules

- **Delivers:** laboratory settings that require a second factor for all staff, for chosen roles,
  for holders of the patient-information capability and for anyone who can reach the admin console,
  and optionally for portal accounts; a person who must enrol is taken to enrolment at the next
  sign-in and reaches nothing else first; passwords of at least 15 characters where the password is
  the only factor and 8 where a second factor is required, checked against a list of common and
  compromised passwords, with no composition rules and no periodic change; an administrator can mark
  a password as compromised, which forces a change; existing passwords meet the rule when next
  changed; failed sign-ins are counted across all server processes.
- **Basis:** 45 CFR §164.312(d) (person or entity authentication); §164.308(a)(5)(ii)(D) (password
  management, addressable); NIST SP 800-63B-4 §3.1.1.2 (a recommendation). Proposed and not in
  force: multi-factor authentication as a requirement (90 FR 898, proposed §164.312(f)).
- **Depends on:** security settings and safeguards reference.
- **Leaves out:** passkeys and security keys and single sign-on, which are Later items, and codes by
  text message.

### Sign-in history and re-authentication

- **Delivers:** sign-ins, failures, sign-outs and lockouts stored in the database with the time, the
  factor used, and the address and browser as the deployment reports them, never a password or a
  code; each person sees their own recent sign-ins; the password, and the second factor where the
  account has one, asked for again before changing roles, capabilities, security settings or another
  account's sign-in factors, and, where the laboratory turns it on, before the first reveal of
  patient information in a session.
- **Basis:** 45 CFR §164.308(a)(5)(ii)(C) (log-in monitoring, addressable); §164.312(b), (d).
- **Depends on:** required two-factor sign-in and password rules.
- **Leaves out:** alerts on unusual sign-ins and blocking by location.

### Access records for sensitive reads

- **Delivers:** a record each time a staff or portal account opens a ticket, its history or its form
  details, downloads a file, or is shown a patient-marked answer, which a portal account sees today
  without one; each record holds who, when, which record, in which capacity and the request
  identifier, never content; access records are append-only and kept apart from change history, so a
  record's history stays readable; change events gain the request identifier; supervisors see on a
  ticket who opened it and when.
- **Basis:** 45 CFR §164.312(b) (audit controls: record and examine activity). HHS states that the
  rule does not say what to record (OCR, January 2017), and NIST SP 800-66r2 §5.3.2 suggests access
  to patient information with the user, the type of event and the time. Which reads are recorded is
  therefore a product choice.
- **Depends on:** nothing unbuilt.
- **Leaves out:** a record for every row shown in a list or a search, the text of searches, and
  forwarding to an external log system.

### Audit review workspace

- **Delivers:** a capability, "Can review access and audit records", granted explicitly like the
  patient-information capability; a workspace where its holders filter change events, access records
  and sign-in history by person, record, client organization, action and date and export the result,
  with each use and export recorded; summaries per person of reveals, downloads, failed sign-ins and
  activity outside the laboratory's business hours; a review record with the reviewer, the period
  covered, what was examined, the findings and a sign-off, so that the laboratory can show that
  reviews happen at the interval it sets. Reviewers cannot change or remove what they review.
- **Basis:** 45 CFR §164.308(a)(1)(ii)(D) (information system activity review, required). OCR
  advises that access to audit trails be strictly restricted (January 2017).
- **Depends on:** access records for sensitive reads; sign-in history and re-authentication.
- **Leaves out:** automatic detection of unusual activity, alerts, and reports sent on a schedule.

### Access report, time-limited grants and access review

- **Delivers:** an access report of every account with its kind, roles, capabilities, teams, portal
  organization and sites, second-factor state, last sign-in, and who granted each role or capability
  and when, with an export; an optional end date on a capability grant and on portal access,
  enforced whenever access is checked; a list of accounts unused for a period the laboratory sets;
  an access review in which a reviewer confirms or removes each grant, recorded with a sign-off;
  deactivation shows what the account held and ends its sessions.
- **Basis:** 45 CFR §164.308(a)(3)(ii)(A)-(C) and §164.308(a)(4)(ii)(B)-(C) (authorization,
  clearance and termination, and establishing, reviewing and modifying access; all addressable).
- **Depends on:** audit review workspace; automatic sign-out and session control.
- **Leaves out:** creating accounts from a personnel or identity system, and an approval workflow
  for access requests.

### Emergency access

- **Delivers:** staff the laboratory names as eligible can, after giving a reason, open one
  restricted ticket or reveal patient information on one ticket without the usual grant; the access
  lasts a short fixed time, a banner shows it while it is active, supervisors and reviewers are told
  at once, and each use waits in a review list until someone other than the user records a review.
  It is off until the laboratory names eligible staff.
- **Basis:** 45 CFR §164.312(a)(2)(ii) (emergency access procedure, required). The procedure is the
  laboratory's. This item gives it a recorded means.
- **Depends on:** access records for sensitive reads; audit review workspace.
- **Leaves out:** access while the application is unavailable, which belongs to the laboratory's
  contingency plan, and emergency access for portal accounts.

### Sign-off record and controlled documents

- **Delivers:** a sign-off record with the signer, the role held then, the meaning (approved,
  reviewed or acknowledged), the time, a checksum of exactly what was signed, and the password
  entered again at signing. Controlled documents with a number, title, type (procedure, policy,
  form, external document), owner team, the people allowed to approve each type, and an optional
  review period; versions as uploaded files with a change summary; Draft, In review, Approved,
  Effective and Retired, a new version retiring the old one on its effective date, nothing deleted;
  a current list with a phone-friendly read view and a review-due filter; a "reviewed by name on
  date" record; a laboratory director named in the laboratory profile. File storage is extended to
  records other than ticket entries, and files can then be attached to a nonconformance. An optional
  module.
- **Basis:** 42 CFR §493.1251(a), (d), (e) and §493.1105(a)(2); CMS State Operations Manual Appendix
  C, D5409 (the director approves before use and cannot delegate it; annual review is not required);
  ISO/IEC 17025:2017 §8.3; ISO 15189:2022 §8.3; 45 CFR §164.316(b) and §164.530(i)-(j) (privacy and
  security policies kept in written or electronic form for six years); 21 CFR §11.50, §11.70 and
  §11.200(a) where Part 11 applies. The product records who approved which version, when and with
  what meaning. It never says "Part 11 compliant", "validated" or "certified".
- **Depends on:** nothing unbuilt.
- **Leaves out:** an editor inside the application and line-by-line comparison, parallel review
  routing, control of printed copies, and reminders, which come with reminders and due lists.

### Read-and-acknowledge and training records

- **Delivers:** a document version assigned to people or teams, each of whom reads and acknowledges
  it with a sign-off, and a list of who has not; training records with a topic or document version,
  date, method, trainer, attendees and an optional expiry or repeat interval the laboratory sets;
  optional record types that carry the fields their rules name, for bloodborne pathogens,
  hazardous-materials shipping, and privacy and security training; the reviewer of a training record
  is never the person trained.
- **Basis:** 42 CFR §493.1251(a) (procedures available to and followed by personnel; no federal text
  read requires staff to read and sign by name); 29 CFR §1910.1030(g)(2), (h)(2); 49 CFR
  §172.704(c)(2), (d); 45 CFR §164.530(b) and §164.308(a)(5)(i).
- **Depends on:** sign-off record and controlled documents.
- **Leaves out:** courses, quizzes, certificates, and blocking any work on training.

### Staff access by team

- **Delivers:** a laboratory setting that limits each staff account to the tickets of its own teams,
  with supervisors reading across teams as they do now and an explicit grant for staff who work
  every team; off by default, so a small laboratory sees no change; every list, search, count,
  timeline and notice follows the same scope.
- **Basis:** 45 CFR §164.502(b) and §164.514(d)(2) (minimum necessary: which classes of people need
  access, and to what).
- **Depends on:** nothing unbuilt.
- **Leaves out:** staff scope by client organization, and per-field rules beyond the
  patient-information capability.

### Privacy requests and identity verification

- **Delivers:** a restricted Privacy requests group of categories, available to every laboratory
  type, with forms for a request for access to records, an amendment, a restriction or confidential
  communication, an accounting of disclosures and a privacy complaint; the date received, a due date
  from a period the laboratory sets, one recorded extension with its reason, the decision, who made
  it and when the person was told; on any logged contact or ticket, a record of how the requester's
  identity and authority were checked, from options the laboratory lists. Part of an optional
  privacy records module.
- **Basis:** 45 CFR §164.524, §164.526, §164.522, §164.528, §164.530(d) and §164.514(h). The period
  for access is 30 days today. A final rule filed for publication on 2026-11-19 (FR Doc. 2026-23429)
  shortens it to 15 calendar days, with compliance due 240 days after publication. The product ships
  neither period.
- **Depends on:** nothing unbuilt.
- **Leaves out:** deciding what belongs to a designated record set, producing the records, and
  letters of denial. Also preset periods, until the maintainers decide whether a preset may carry a
  rule's period when it names its source ([open decision](#open-decisions) 4).

### Disclosure log

- **Delivers:** a disclosure record on a ticket or a person-level link with the date, who received
  the information and their address if known, a short description of what was disclosed, and its
  purpose or a reference to the request; visible only with the patient-information capability; an
  accounting for one person over a chosen period, exported for the laboratory to send. Part of the
  privacy records module.
- **Basis:** 45 CFR §164.528(a)(1), (b)(2), (d) (what an accounting holds, for the six years before
  the request). Which disclosures must be accounted for is the laboratory's decision, and the rule
  excludes many.
- **Depends on:** privacy requests and identity verification.
- **Leaves out:** deciding whether a disclosure is accountable, and disclosures made from other
  systems.

### Security incident and breach assessment records

- **Delivers:** a restricted incident record (`INC-000123`) with what happened, when and how it was
  discovered, the records and systems involved as links, containment and mitigation with dates, and
  the outcome; an assessment section with the four factors the rule lists, the conclusion, who
  reached it and when; the dates of notice to individuals, the Secretary of Health and Human
  Services and the media where given; corrective actions through a linked nonconformance; a yearly
  list of incidents for the laboratory's own reporting. The product records the assessment and
  decides nothing. Part of the privacy records module.
- **Basis:** 45 CFR §164.308(a)(6)(ii) (document security incidents and their outcomes, required);
  §164.402 (the risk assessment and its four factors); §164.404, §164.406, §164.408 and §164.410
  (notice without unreasonable delay and within 60 calendar days of discovery; a log of breaches
  that affect fewer than 500 people); §164.414(b); §164.530(j).
- **Depends on:** nonconformance and corrective action.
- **Leaves out:** drafting notices, counting a deadline by default, and detecting incidents.

### Encryption of sign-in secrets and patient information

- **Delivers:** authenticator secrets, recovery codes, person-level link keys and patient-marked
  answers stored encrypted with a key the deployment supplies outside the database, so that a
  database dump holds ciphertext; exact-key search keeps working through a keyed digest; a key
  identifier on each value and a command to rotate the key and re-encrypt; the security settings
  page reports whether a key is set. Existing values are converted by a new migration that respects
  the triggers protecting history.
- **Basis:** 45 CFR §164.312(a)(2)(iv) (encryption and decryption, addressable). Proposed and not in
  force: encryption of all patient information at rest and in transit (90 FR 898, proposed
  §164.312(b)(2)).
- **Depends on:** security settings and safeguards reference.
- **Leaves out:** encryption of file contents and free text, which rest on storage encryption by the
  host, and integration with a key management service.

### Tamper-evident audit records

- **Delivers:** change events, access records and sign-in history sealed in batches by the scheduled
  job, each seal carrying a hash of its records and of the seal before it; a command, and a line on
  the security settings page, that verify the chain and report any gap or alteration; a signed copy
  of the latest seal that an operator can keep outside the database.
- **Basis:** 45 CFR §164.312(c)(1), (c)(2) (integrity, and a mechanism to corroborate that
  information has not been altered, addressable). Today whoever controls the database can disable
  the triggers that protect history.
- **Depends on:** scheduled jobs and the notification outbox; access records for sensitive reads;
  sign-in history and re-authentication.
- **Leaves out:** an external timestamping or notary service.

### Email channel: replies out, inbound by webhook

- **Delivers:** staff replies reach a ticket's contact by email as well as the portal, by default as
  the notice sent today with no ticket content, and with the reply text only where the laboratory
  allows it for a category, never for a restricted category or a patient-marked value; inbound email
  through a provider webhook with a verified signature, threaded by message headers and then a
  signed reply address, with loop and sender checks; mail from an unverified sender is held for
  review, and a new message from a known contact can open a ticket; files that arrive are inspected
  like uploads.
- **Basis:** 45 CFR §164.312(e) (transmission security; encryption addressable). What an email may
  carry is the laboratory's decision.
- **Depends on:** scheduled jobs and the notification outbox.
- **Leaves out:** mailbox polling, which is a Later item, rich-text composing, and email to people
  who are not a recorded contact. Also a mail route on the public demonstration, which waits for the
  maintainers to connect a provider and a domain: build and test against synthetic payloads in the
  provider's documented format.

### Reminders and due lists

- **Delivers:** a Due page for each person and team that gathers dated work from every module
  switched on: nonconformance actions, document reviews, acknowledgements, training that expires,
  access reviews, privacy requests and laboratory certificates nearing their end date; reminders
  through the outbox at intervals the laboratory sets, and one notice to a supervisor when something
  passes its date. Each later item with a due date joins the same page.
- **Depends on:** scheduled jobs and the notification outbox.
- **Leaves out:** escalation chains, calendar feeds and text messages.

## Later

### Time worked on a ticket

- **Delivers:** staff record the time they worked on a ticket: the person, the date, the minutes and
  an optional note of what was done, entered on its own or together with a reply, a note or a logged
  contact; the ticket shows the total time worked, the time by person, and the elapsed time from
  opening to resolution; a laboratory setting can ask for time before a ticket is resolved, for all
  categories or for chosen ones, off by default; a time entry is corrected with a reason and never
  edited; time is for staff and is never shown to clients; the ticket list gains a column and a
  filter for time worked.
- **Depends on:** nothing unbuilt.
- **Leaves out:** a running timer, billing from time, payroll timesheets, and time on records other
  than tickets.

### Competency and task authorization

- **Delivers:** a competency assessment per person and area with the elements the laboratory lists,
  each with a date and method, the assessor and their role, the outcome, and the next due date from
  an interval the laboratory sets; task authorizations (person, test or area, authorized by, date,
  withdrawn on). Information only, never a gate on other work.
- **Basis:** 42 CFR §493.1413(b)(8)-(9), §493.1451(b)(8)-(9) and §493.1407(e)(14); ISO 15189:2022
  §6.2; ISO/IEC 17025:2017 §6.2. Training and performance reviews are not competency assessments
  (CMS, May 2025).
- **Depends on:** read-and-acknowledge and training records.
- **Leaves out:** checking anyone's qualifications, and authorization granted automatically from a
  competency. Also a starter assessment with the six federal elements and their timing, until the
  maintainers decide whether the product may ship one that names its source ([open
  decision](#open-decisions) 4).

### Client instructions library

- **Delivers:** a document type "instruction for clients" (collection, packaging, shipping,
  requisition help) shown in the portal from its approved effective version only, and linked from
  ticket forms.
- **Basis:** 42 CFR §493.1242(a), (d); ISO 15189:2022 §7.2.
- **Depends on:** sign-off record and controlled documents.
- **Leaves out:** search ranking, article feedback and templates.

### Reply templates

- **Delivers:** reply and note templates the laboratory writes, for one team or for all staff, with
  placeholders for the ticket number, the client's name and the laboratory's name; chosen in the
  composer and edited before sending; a retired template stays on record.
- **Depends on:** nothing unbuilt.
- **Leaves out:** automatic replies, and templates that pull data from other systems.

### Ticket tags, followers and mentions

- **Delivers:** tags from a list the laboratory keeps, usable in filters and saved views; staff can
  follow a ticket to receive its notices; naming a colleague in an internal note notifies them,
  within what they are allowed to read.
- **Depends on:** nothing unbuilt.
- **Leaves out:** tags that clients can see or set, and following by portal accounts beyond today's
  notices.

### Desk reports and list export

- **Delivers:** monthly counts of tickets by team, category, client organization and channel;
  first-response and resolution targets met and missed; time to first response and to resolution;
  time worked on tickets by person, team, category and client organization; complaints opened and
  closed; each report and the ticket list exported as CSV within the reader's scope, without
  patient-marked values or person-level keys, and with each export recorded.
- **Basis:** 42 CFR §493.1239 (ongoing quality assessment, documented); ISO 15189:2022 §5.5 d)
  (indicators such as turnaround achieved).
- **Depends on:** access records for sensitive reads, which record the exports; time worked on a
  ticket.
- **Leaves out:** charts beyond simple trends, delivery on a schedule, and a report builder.

### Sample and requisition problem reasons

- **Delivers:** a reason list the laboratory keeps (labelling, volume, container, transport,
  requisition and others) on tickets in the sample problem and requisition categories; counts per
  client organization, site and reason by month, in desk reports and on the client's record, so that
  repeated problems from one site are visible.
- **Basis:** 42 CFR §493.1249(a)-(c) (preanalytic problems monitored, the effect of corrective
  actions reviewed, and the activity documented); §493.1242(a) (policies for specimen acceptability
  and rejection).
- **Depends on:** desk reports and list export.
- **Leaves out:** specimen-level data and the decision to reject, which stay in the laboratory's
  systems.

### Accessibility conformance

- **Delivers:** an automated accessibility check of every staff and portal page at phone and desktop
  widths in both themes, run with the browser journeys; a review of the main journeys with a
  keyboard and a screen reader, with what fails fixed; an accessibility statement in `docs/` that
  lists what was checked, against which criteria, and the known gaps.
- **Basis:** 45 CFR §84.84(b) (web content of recipients of HHS funding meets WCAG 2.1 Level A and
  AA from 2027-05-11 for recipients with fifteen or more employees and from 2028-05-10 for smaller
  ones). Whether a laboratory is a recipient is its own question.
- **Depends on:** nothing unbuilt.
- **Leaves out:** a third-party audit or certification.

### Client notices with acknowledgement

- **Delivers:** a notice to chosen client organizations, sites or organization types, with a title,
  text, effective dates and whether an acknowledgement is asked for; shown in the portal and
  announced through the outbox with no content in the email; a list of who has acknowledged and who
  has not, from which staff can open follow-up tickets; a nonconformance can link a notice as the
  record that clients were told.
- **Basis:** 42 CFR §493.1291(k) (prompt notice of reporting errors) and §493.1234 (communication
  problems identified and documented); ISO 15189:2022 §6.7.1 (users told of changes that affect
  them).
- **Depends on:** scheduled jobs and the notification outbox.
- **Leaves out:** results, corrected reports and patient information in a notice, and text messages.

### External assessments, licences and certificates

- **Delivers:** the certificates already kept in the laboratory profile extended to licences and
  accreditations, each with its issuing body, number, scope, dates, an owner and a file, and an end
  date that appears in the due lists; an assessment record for an inspection or accreditation visit
  with its date, the body and its findings; each finding with its reference, due date, owner and
  plan of correction, opening a nonconformance for the corrective action.
- **Basis:** 42 CFR §493.1816(a)-(b) (a plan of correction acceptable in content and time frames;
  deficiencies corrected within 12 months) and §493.1773 (records available to inspectors).
- **Depends on:** nonconformance and corrective action; sign-off record and controlled documents.
- **Leaves out:** accreditors' portals, tables of accredited scope, and checking a certificate with
  its issuer.

### Proficiency testing

- **Delivers:** a proficiency testing event with its program, due date, analyst, attestation
  sign-offs by the analyst and the director, score, acceptable or not, and evaluation; an
  unacceptable result opens a nonconformance.
- **Basis:** 42 CFR §493.801(b), §493.803 and §493.1236.
- **Depends on:** nonconformance and corrective action; sign-off record and controlled documents.
- **Leaves out:** programs' scoring rules and charts.

### Internal audits and findings

- **Delivers:** an audit plan for the year, audit records, and findings that open nonconformances,
  with their follow-up.
- **Basis:** ISO/IEC 17025:2017 §8.8; ISO 15189:2022 §8.8; TNI 2016 Volume 1 Module 2 §4.14.
- **Depends on:** nonconformance and corrective action.
- **Leaves out:** checklist libraries and scoring.

### Quality indicators

- **Delivers:** indicators the laboratory defines, each with a name, what it counts, a target, an
  owner and a review interval; values calculated monthly from tickets, service clocks, complaints,
  sample problem reasons and nonconformances, or entered by hand with a note for figures held
  elsewhere; a review record per period with the action decided.
- **Basis:** 42 CFR §493.1239, §493.1249, §493.1289 and §493.1299 (quality assessment of each phase,
  documented); ISO 15189:2022 §5.5 d) and §8.8.2.
- **Depends on:** desk reports and list export; nonconformance and corrective action.
- **Leaves out:** statistical process control and figures pulled from other systems.

### Management review and service report

- **Delivers:** a review record with minutes, decisions and action items, and a summary of counts
  from the other modules; a service report of tickets, targets met and complaints by client
  organization and month.
- **Basis:** ISO/IEC 17025:2017 §8.9; ISO 15189:2022 §8.9.
- **Depends on:** nonconformance and corrective action; proficiency testing; internal audits and
  findings; quality indicators.
- **Leaves out:** dashboards beyond counts.

### Personnel credentials and position descriptions

- **Delivers:** the part of a personnel record a laboratory holds itself, on one page per staff
  member: credentials (licence, certification, degree or other) with the issuer, number, dates and a
  file, a position description linked to a controlled document, and the training, competency and
  authorization records already kept; end dates join the due lists. Visible to the person, their
  supervisor and administrators.
- **Basis:** 42 CFR Part 493 Subpart M (personnel qualifications); ISO 15189:2022 §6.2.5; 21 CFR
  §58.29(b).
- **Depends on:** read-and-acknowledge and training records; competency and task authorization.
- **Leaves out:** leave, appraisal, pay, health and disciplinary records, which belong in a
  personnel system, and checking a credential with its issuer.

### Change control and planned deviations

- **Delivers:** a change record with its description, reason, assessment of effect, approver,
  effective date, and the documents and training it touches, closed when those are done; a planned
  deviation as the same record marked as a departure, with its justification, its limit in time or
  scope, and its approver.
- **Basis:** 21 CFR §58.81(a) and §211.100(b) (changes authorized; deviations recorded and
  justified); ISO 15189:2022 §5.5 c).
- **Depends on:** sign-off record and controlled documents; nonconformance and corrective action.
- **Leaves out:** multi-stage routing and validation plans.

### Declarations of impartiality and confidentiality

- **Delivers:** a declaration the laboratory words (confidentiality, impartiality, interests to
  disclose, data integrity), assigned to staff at a repeat interval it sets and signed with a
  sign-off; a disclosed interest is reviewed by a supervisor.
- **Basis:** ISO 15189:2022 §4.1 and §4.2.3; ISO/IEC 17025:2017 §4.1 and §4.2.
- **Depends on:** read-and-acknowledge and training records.
- **Leaves out:** monitoring of relationships.

### Recurring checklists

- **Delivers:** checklist templates with items, an interval and an assignee or team; dated runs
  created by the scheduled job; each item marked done, failed or not applicable with a comment, and
  the run signed; overdue runs in the due lists; a failed item can open a ticket or a
  nonconformance. For daily, weekly and monthly maintenance, storage checks, safety rounds and
  opening or closing duties.
- **Basis:** 42 CFR §493.1254(a) (maintenance and function checks performed and documented) and
  §493.1252(b) (storage conditions monitored and documented).
- **Depends on:** scheduled jobs and the notification outbox; sign-off record and controlled
  documents.
- **Leaves out:** readings from sensors, charts, and limits that judge a reading.

### Client onboarding checklists

- **Delivers:** a checklist run against a client organization when an account is set up or changes,
  with the steps the laboratory lists (requisitions, supplies, courier schedule, portal accounts,
  agreement review), each with an assignee and a date, shown on the client's record.
- **Depends on:** recurring checklists.
- **Leaves out:** steps performed in other systems.

### Equipment register

- **Delivers:** an asset register with calibration and maintenance events and a next-due date the
  laboratory enters, in the due lists and linkable from a nonconformance.
- **Basis:** 42 CFR §493.1254 and §493.1255; ISO/IEC 17025:2017 §6.4.13; ISO 15189:2022 §6.4.7.
- **Depends on:** nonconformance and corrective action.
- **Leaves out:** intervals proposed by the product.

### Supplier and referral laboratory register

- **Delivers:** suppliers, service providers and referral laboratories with their approval status,
  evaluation date, a certificate number the laboratory types, and their agreements (such as a
  service or business associate agreement) with dates and a file.
- **Basis:** ISO/IEC 17025:2017 §6.6; ISO 15189:2022 §6.8; 42 CFR §493.1242(c); 45 CFR §164.308(b)
  and §164.504(e) (written agreements with business associates).
- **Depends on:** sign-off record and controlled documents.
- **Leaves out:** checking a certificate, and judging whether an agreement is needed or adequate.

### Client agreements and their review

- **Delivers:** on a client organization, its agreements with a title, term, review date, reviewer,
  outcome and a file, and a history of reviews; review dates in the due lists.
- **Basis:** ISO 15189:2022 §6.7.1; ISO/IEC 17025:2017 §7.1.
- **Depends on:** sign-off record and controlled documents.
- **Leaves out:** quotes, prices and contract templates.

### Service disruption log

- **Delivers:** a disruption record for an outage or limited service with its start, end, what was
  affected and what was done, linked to the client notice that announced it; a record of each
  exercise of the laboratory's continuity plan with its date, scenario and lessons.
- **Basis:** ISO 15189:2022 §7.8 and §7.6.4.
- **Depends on:** client notices with acknowledgement.
- **Leaves out:** monitoring of systems, and the continuity plan itself, which is a controlled
  document.

### Ticket forms offered by organization type

- **Delivers:** a ticket form can be offered to client organization types as well as to listed
  organizations, so a laboratory with many clients chooses "every clinic" and does not name each
  one.
- **Depends on:** nothing unbuilt.
- **Leaves out:** offering by site, by contact, or by rules over other fields.

### Merging duplicate tickets

- **Delivers:** a staff member marks a ticket as a duplicate of another in the same client
  organization; the duplicate closes with a link, its entries and files stay where they are and can
  be read from the ticket that continues, and whoever raised it is told which ticket continues.
  Nothing is moved or deleted.
- **Depends on:** nothing unbuilt.
- **Leaves out:** merging across organizations.

### Ticket record export

- **Delivers:** one ticket with its conversation, form answers, file list and history as a printable
  document, and with its files as an archive, for a records request or an inspection; patient-marked
  values and person-level keys appear only for staff with the capability and only when asked for;
  the output names who produced it and when, and each export is recorded.
- **Basis:** 42 CFR §493.1773 (records retrievable for inspection); 45 CFR §164.524 (copies for an
  access request, where the laboratory decides the ticket is in scope).
- **Depends on:** access records for sensitive reads.
- **Leaves out:** export of all tickets at once, and redaction tools.

### Inbound API

- **Delivers:** system tokens (hashed, scoped, expiring), idempotency keys, links created or updated
  one at a time and in bulk, a reverse look-up of tickets by record, and optional allow-listed
  snapshots that expire, so a laboratory system can push the links staff now enter by hand. Each
  call is recorded like any other change.
- **Depends on:** nothing unbuilt.
- **Leaves out:** reading from a laboratory system on demand, and HL7 or file parsing.

### Outbound webhooks

- **Delivers:** signed notices of ticket events (opened, status changed, closed) to addresses the
  laboratory registers, sent through the outbox with retries, carrying the ticket number and the
  event and no ticket content.
- **Depends on:** scheduled jobs and the notification outbox; inbound API.
- **Leaves out:** payloads with conversation text or patient information.

### Single sign-on for staff

- **Delivers:** staff sign-in with OpenID Connect, linked to existing accounts by issuer and
  subject, with local sign-in kept for the first administrator and for portal accounts; the
  laboratory chooses whether a sign-in through its identity provider satisfies its second-factor
  rule.
- **Depends on:** required two-factor sign-in and password rules.
- **Leaves out:** SAML, accounts or roles created from the identity provider, and single sign-on for
  portal accounts.

### Passkeys and security keys

- **Delivers:** a passkey or security key as a second factor and as a way to sign in, enrolled and
  removed by the person, with recovery codes as the fallback.
- **Depends on:** required two-factor sign-in and password rules.
- **Leaves out:** requiring a particular kind of key, and attestation of devices.

### Attachment scanning

- **Delivers:** a connection to a malware scanner the deployment runs; a file is held until scanned,
  a file that fails is quarantined with a reason, and the "not scanned" state stays visible where no
  scanner is set.
- **Basis:** 45 CFR §164.308(a)(5)(ii)(B) (protection from malicious software, addressable).
- **Depends on:** nothing unbuilt.
- **Leaves out:** removing content from files, and scanning by an outside service. Also a scanner on
  the public demonstration, which is new spending for the maintainers to approve: build and test
  against a scanner in the test environment.

### Retention schedule and review list

- **Delivers:** a retention period the laboratory sets for each class of record (tickets, files,
  quality records, privacy records, audit and access records); a hold that keeps named records
  regardless of period; a list of records past their period, for the laboratory to review. The
  application still deletes nothing.
- **Basis:** 42 CFR §493.1105 (minimum periods by record type); 45 CFR §164.316(b)(2)(i) and
  §164.530(j)(2) (six years for the documentation those rules require). The periods are the
  laboratory's to set.
- **Depends on:** nothing unbuilt.
- **Leaves out:** disposal, anonymization and archives outside the database. Whether the application
  ever disposes of records is [open decision](#open-decisions) 6.

### Inbound email by mailbox polling

- **Delivers:** inbound email from a mailbox the laboratory keeps, polled by the scheduled job, for
  laboratories that do not route mail through a provider webhook.
- **Depends on:** email channel.
- **Leaves out:** shared-mailbox rules and folders.
- **Skip until:** an application registration with a mail provider for a test mailbox, which is new
  access.

### Supply orders, fuller

- **Delivers:** catalogs per client organization, optional approval of an order before it is filled,
  and fulfilment status on top of the item list that ticket forms already record.
- **Depends on:** nothing unbuilt.
- **Leaves out:** stock levels, purchasing and shipping integrations.

### Standing courier pickups

- **Delivers:** recurring pickups per site with service windows, a ticket for each occurrence, and
  escalation of a missed pickup.
- **Basis:** 42 CFR §493.1242(a) (policies for specimen transport).
- **Depends on:** scheduled jobs and the notification outbox.
- **Leaves out:** route planning, courier tracking and custody of transport containers.

## Exploring

Not ready to build. Each waits for evidence that laboratories want it.

- A one-question rating after a ticket is resolved, and a periodic client survey.
- A risk register (owner, action, review date) and a staff suggestion list.
- Writing and redlining documents inside the application.
- Read-through look-ups in one laboratory system, built with a laboratory that runs it.
- A flag and a redisclosure notice for records under 42 CFR Part 2, for laboratories that handle
  them.
- A restricted record of safety events and sharps injuries (29 CFR §1910.1030(h)), without health
  details.
- Confidential reporting of data-integrity concerns, and client audit requests, as restricted
  categories.
- Stock levels for client supplies, and a calendar feed of due dates.
- A shift handover log.
- Disposal of records past their retention period.
- Records particular to one laboratory type (biorepository, environmental, food, workplace drug
  testing) beyond the presets.

## Not planned

| Left out | Why |
| --- | --- |
| Testing, results, corrected reports and critical-result notification | They stay in the laboratory's own systems. Lab Ops holds a reference, never a result |
| Method validation records, reagent lots and reagent inventory | They belong with the analytic data in the laboratory's LIS or LIMS |
| Chemical inventory, safety data sheet indexing and waste records | They belong in an environment, health and safety system. The plans themselves can be controlled documents |
| Leave, appraisal, pay, health and disciplinary records, and staff scheduling | They belong in a personnel system |
| HL7 or FHIR interfaces | The desk references records. The inbound API receives links from a relay the laboratory runs |
| Sales pipeline and quotes, invoicing and payments, payroll timesheets | A laboratory's work with its clients is service to existing accounts, and billing and payroll stay in their own systems. Time worked on a ticket is a Later item |
| Telephony and text messages | Calls are logged by hand. Automated texts raise consent rules that need their own design |
| AI features | Not asked for by evidence, and they add a data processor |
| A configurable workflow engine | Each record type has the few statuses its rules need |
| Hosting, backups, network controls and physical safeguards | They belong to whoever deploys. See [operations](operations.md) |

## Open decisions

Each changes the order or a design. Until the maintainers decide, the default applies.

| # | Decision | Default applied |
| --- | --- | --- |
| 1 | How far the product goes into quality records for the whole laboratory, beyond those that touch clients | The order on this page |
| 2 | Who approves controlled documents | An approver designation per document type and a laboratory director setting. The product never judges who qualifies |
| 3 | What a sign-off asks for, and what the product says about signatures | The password entered again at each signing. The wording under sign-off record and controlled documents, read by counsel before a stable release |
| 4 | Whether a preset may carry a rule's elements or periods when it names its source (competency elements and timing, privacy request periods, training intervals) | No. The laboratory enters them |
| 5 | Patient information in quality and privacy records | Reference an order or a ticket and warn on the form. Privacy and incident records are restricted |
| 6 | Whether the application ever disposes of records | Nothing is deleted. The retention item lists and holds only |
| 7 | Starting values for sign-out and passwords | 30 minutes without activity and 12 hours overall; 15 characters, or 8 with a required second factor |
| 8 | Which reads are recorded | Ticket, history and form detail opens, file downloads, patient-marked answers shown, and exports. Not each row of a list |
| 9 | Inbound email route first | Provider webhook first, mailbox polling second |

## Sources

Regulatory texts are United States federal sources. They explain why an item exists and say nothing
about what a particular laboratory must do. Each laboratory identifies the rules, accreditation and
contracts that apply to it.

| Source | Read | Note |
| --- | --- | --- |
| [45 CFR Part 164](https://www.ecfr.gov/current/title-45/part-164), HIPAA Security, Breach Notification and Privacy Rules | 2026-11-18 | eCFR text as of 2026-11-16. "Required" and "addressable" are the rule's own labels; an addressable specification is assessed, then implemented or the reason documented (§164.306(d)(3)) |
| [90 FR 898](https://www.federalregister.gov/d/2024-30983), proposed changes to the HIPAA Security Rule (2025-01-06, RIN 0945-AA22) | 2026-11-18 | A proposal, not in force. The [Unified Agenda](https://www.reginfo.gov/public/do/eAgendaViewRule?pubId=202510&RIN=0945-AA22) lists it as a long-term action with final action in July 2027 |
| [FR Doc. 2026-23429](https://www.federalregister.gov/d/2026-23429), HIPAA Privacy Rule final rule | 2026-11-18 | Read in the public inspection copy filed 2026-11-16 for publication on 2026-11-19. Effective 60 days and compliance 240 days after publication. Confirm the dates in the published rule |
| [OCR Cybersecurity Newsletter, January 2017](https://www.hhs.gov/sites/default/files/january-2017-cyber-newsletter.pdf), audit controls | 2026-11-18 | Guidance |
| [NIST SP 800-66 Rev. 2](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-66r2.pdf) (February 2024) and [NIST SP 800-63B-4](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf) (July 2025) | 2026-11-18; SP 800-63B-4 §2.2.3, §5.1, §5.2 and its usability guidance on timeouts again on 2026-11-21 | Recommendations, not HIPAA requirements |
| [45 CFR §84.84](https://www.ecfr.gov/current/title-45/section-84.84), accessibility of web content under Section 504 | 2026-11-18 | Applies to recipients of HHS funding |
| [42 CFR Part 493](https://www.ecfr.gov/current/title-42/part-493), CLIA | 2026-11-17 and 2026-11-18 | Applies to laboratories that test human specimens |
| [CMS State Operations Manual, Appendix C](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/som107ap_c_lab.pdf), Rev. 236 (2026-01-23) | 2026-11-17 | Guidance for surveyors |
| [29 CFR §1910.1030](https://www.ecfr.gov/current/title-29/section-1910.1030), [49 CFR §172.704](https://www.ecfr.gov/current/title-49/section-172.704), [21 CFR Part 11](https://www.ecfr.gov/current/title-21/part-11), [Part 58](https://www.ecfr.gov/current/title-21/part-58) and [Part 211](https://www.ecfr.gov/current/title-21/part-211) | 2026-11-17 and 2026-11-18 | Each applies only to the activities it names |
| ISO 15189:2022, ISO/IEC 17025:2017 and TNI 2016 Volume 1 Module 2 | 2026-11-17 and 2026-11-18 | Read in public final drafts and in accreditation bodies' assessment material. The licensed texts were not read, so clause numbers can differ |
