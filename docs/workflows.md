# OpenBGA Lab Ops workflows

This guide follows a ticket from the first call to closure. The Help drawer on each staff and
portal page lists its fields, actions, permissions and messages. A demonstration installation
uses synthetic accounts and records.

## Signing in and accounts

Sign in with the account the laboratory provides. A superuser creates accounts in **Accounts &
roles**. Staff and client portal accounts are distinct. Users can enroll an authenticator and
save recovery codes; where the laboratory requires one of their account, they set it up at sign-in
before anything else. Password resets and sign-in codes depend on the deployment's email settings.
Saving a change to an account or a role, the session limits or the second-factor rule asks the
person making it for their password again, and a code where their account has an authenticator
app, unless they confirmed in that browser in the last 5 minutes.
[Sign-in configuration](operations.md#sign-in-limits-behind-a-proxy)

## Roles and capabilities

Staff can work tickets within their scope. Supervisors can work across restricted teams and
review eligible complaints. Client portal accounts belong to one organization. The separate
**Can view patient information** grant controls staff access to person-level links and marked
form answers. The **Can review access and audit records** grant opens **Audit review**, under
Oversight in the sidebar: choose **Changes**, **Access records**, **Sign-ins** or **Workspace use**,
narrow it by person, organization, action, ticket number, days or **Outside the hours of** a
business calendar, and select **Export CSV** to keep what was reviewed. **People** counts each
account's sign-ins, failed sign-ins, tickets opened, files downloaded, patient information shown
and changes over a period, with how much fell outside business hours; select a count to open the
rows behind it. **Reviews** keeps the signed record of each review (journey 13). Each page opened
there and each export is itself recorded. The same grant opens **Access review**: **Accounts** lists every
account with its roles and capabilities and who gave them, and **Unused accounts** lists active
accounts with no sign-in for the laboratory's period.
[Full access table](product.md#roles-and-capabilities)

## Staff journeys

### 1. Log a ticket from a phone call

In **Tickets**, choose **New ticket**. To start with an organization filled in, use **New ticket
for this organization** from its record. Choose a category to see its guidance, proposed team and
priority. Choose a ticket form if one fits the call.

Enter a subject and what the caller asked. Choose the caller's organization, site or contact if
known. Put an order or sample number in **Reference given**. Staff intake defaults to an internal
note. Choose a reply if the client should see it. If a form asks for a required detail the caller
cannot give, select **Some required details are not known yet**. Choose **Log ticket** and give the
caller the ticket number. It will look like `REQ-000123`.

### 2. Find a ticket

Open **Tickets** and select a queue such as **My open**, **Unassigned in my teams** or **Waiting on
client**. Apply filters for status, assignee, organization, dates or service clock. On a phone
**Filter and sort** opens the list controls. Use **Find** to search by ticket number, organization,
contact, subject or a linked record identifier. For an exact patient, participant or donor key,
use the separate patient-information search. The application audits that search.

### 3. Read and answer a ticket

Open the ticket number to see its details and conversation. Use **Reply** for a message the
client should read, **Note** for an internal entry, or **Log contact** for an exchange outside
the application. Select the matching outcome when logging a call. A reply is visible to clients
who can read the ticket. A notice on the composer tells you if it will reach nobody.

Attach files to a reply or note before posting. To correct a posted message, add a new entry.
A supervisor can withdraw a reply or attachment from client view with a reason. To update an
answer on a ticket form, use **Edit form details**. Give a reason when replacing or clearing a
value. A protected answer requires the patient-information grant.

Opening a ticket or its form details, downloading a file and revealing a protected answer are
each recorded with the person and the time, never what they saw; so is a client portal account
opening its own ticket. A supervisor reads these records, newest first, under **More**, **Who
opened it**, and the changes under **Audit history**.

### 4. Assign, take or move a ticket

Choose **Take it** to assign a ticket to yourself if you belong to its team. **Assign** lists
teams and their active members. Select the team alone to leave the ticket in its unassigned
queue, or select a person under that team. The change appears in the ticket history. The ticket
list also supports assignment of several selected tickets.

### 5. Change status, resolve, close or reopen

Use **Status** when responsibility for the next action changes. Waiting on client pauses the
resolution clock. Waiting on third party pauses it if the target is configured that way.
Resolve with an internal summary. Closing is final and is available only after resolution.
Reopen a resolved ticket with a reason. A client reply also reopens one that was resolved or
waiting on the client. A closed ticket can have a new linked follow-up. If an administrator has
set a period on the Scheduled jobs page, resolved tickets close by themselves that many days
after they were resolved; complaints wait for their review.

### 6. Handle a complaint

A complaint category marks the ticket at intake. Staff can also mark a ticket as a complaint
with a reason. The assignee or a supervisor records the investigation decision and rationale,
findings, actions, corrective-action reference and communication of the outcome. Resolve the
immediate issue when appropriate. Before closure, a supervisor who never held the assignment or
recorded or revised the investigation decision must review the complaint. The **Complaints
awaiting review** queue helps supervisors find this work. With the nonconformance module on, open the corrective action
as a nonconformance from the complaint's record instead of typing a reference.

### 7. Record a nonconformance

When the laboratory uses the nonconformance module, open **Nonconformances** and choose **New
nonconformance**, or choose **Open nonconformance** in a complaint's record. Describe what happened
and refer to orders, samples or reports by their numbers, never by patient. A supervisor chooses
the owner. The owner records containment, root cause and similar cases, the decision on results
already reported and whether the client was told. Add actions with a person and a due date and
move the record through its statuses. In Verification, record the effectiveness check. Another
supervisor then closes it. **Corrective actions** lists open and overdue actions.

### 8. Link an external record

From a ticket or organization, add a link, choose its source system and type, then enter the
source identifier. If clients should see a non-person link, select **Show to client** where
offered. Web addresses must use HTTPS on the configured system host. For a patient, participant
or donor link, staff with the explicit grant can enter the source key and a masked label.
Revealing it records an audit event and an access record. Open the source system separately to look up that person.

### 9. Look up a client or contact

Find the organization by name or account code. Its page brings together open tickets, contact
details, sites, portal access and links to other systems. Use **Timeline** to review recent
activity across the organization. Staff see only tickets within their scope.

### 10. Maintain organizations, sites and portal access

Account managers and client services staff can create or update these records. From an organization
add a site or contact, or use **Set access** beside a portal account to choose its linked contact
and its organization-wide or site-specific scope. Without an access grant, the account can read
only tickets it raised in its current organization.

For a bulk update, open **Spreadsheet import**. Download the corresponding CSV template. Load
organizations before their sites, contacts or external identifiers. Check the preview for creates,
updates, unchanged rows and errors. Confirm the file only when the changes match your intent.

### 11. Save a view

Set the filters and sort on **Tickets**, then select **Save view**. Give it a name.
A supervisor can share a view with a team or all staff. Each reader sees only tickets their
account may open.

### 12. Set up the desk

Administrators set up teams and ticket categories first. Category presets provide starting
points for different kinds of laboratories. Adjust routing, client labels and restrictions, then
load standard forms for the categories in use. Configure catalogs, form fields and which clients
may use each form. Set business calendars and service targets where a clock is needed. Register
external systems before using their identifiers in links or imports.

### 13. Review the activity records

Holders of the **Can review access and audit records** grant open **Audit review** and then
**Reviews**. **Where reviews stand** shows when the next review is due and from which day activity
is not reviewed yet. The first time, a reviewer selects **Set interval** and enters the laboratory's
interval in days with a reason; nothing is set until then.

Examine the period on **Changes**, **Access records**, **Sign-ins**, **People** and **Workspace
use**, setting **From** and **To**, and open a row's record where something needs a closer look.
Then select **Record a review**. The period starts at the day after the last day reviewed. Tick the
lists you went through, note any records examined outside the application, choose the outcome and
write the findings and the follow-up, referring to tickets and accounts by number or username and
never to a patient. Enter your password and select **Sign review**. The signed review cannot be
changed. To correct it, or to record what the follow-up came to, open it and choose **Add a note**.
Use **Export CSV** to keep a copy of the reviews with their checksums.

To review who holds access, open **Access review**. On **Accounts**, narrow by **Role**,
**Capability** or **Second factor** and read under each role and capability who gave it and when;
"Not in the change history" means no recorded change gave it, or a later recorded change took it
away and it came back by a path that records nothing, so find out how the account got it.
On **Unused accounts**, the first time, select **Set the period** and enter the laboratory's
number of days with a reason. Deactivate an account that is no longer needed in the account
administration; Access review changes no account, role or grant, only its own period. Export
either list with **Export CSV** to keep it with the review.

## Client journeys

### 14. Sign in and see open tickets

The portal home offers services and lists open tickets, including those waiting for you. Choose
**Tickets** for the fuller list. Access depends on the organization and sites assigned to your
account. A restricted ticket appears only to the client account that raised it.

### 15. Raise a ticket

Select a service, such as **Request a pickup**, or use **New ticket** for another topic. Choose
a site when asked. Fill the required details, include an order or sample number in **Your
reference** if relevant and send. Keep the ticket number for follow-up. If a form is still
available, **Request again** on a previous ticket starts another with eligible answers copied.
Check them and enter any dates, times, confirmations or patient details again.

### 16. Follow a ticket and reply

Open a ticket to see its status, the conversation and any form details, files or references
shared with you. Send a reply to answer the laboratory. Replying to a resolved ticket or one
waiting for you returns it to the laboratory. To continue a closed ticket, select **New
follow-up ticket**. The new record links back to the closed one.

## Time zones and notifications

Forms and pages use the deployment's time zone. Its business calendars use the same zone.
The laboratory profile records the zone selected at setup. An administrator can synchronize the
profile copy with `setup_lab --sync-timezone`.

When email is configured, a notice identifies the ticket and directs the recipient to sign in.
Read the message and download files inside the application. Staff can change their own email
notice preference. The assignee and the team's leads also get a notice when a ticket passes a
service target. The demonstration sends no email.

## Laboratory information

Staff can consult the laboratory profile, its recorded certificates, its security and privacy
contacts and its retention statement. An administrator keeps the contacts current from the profile
or from Setup > Security settings, giving a reason for each change. Security settings also lists
what protects sign-in, sessions and patient information and who sets each item, and there an
administrator sets after how long without activity, and after how long at most, staff and client
portal sessions are signed out. Pages warn before signing out and offer Stay signed in. Each
person sees where their account is signed in, from the account menu, and signs out the sessions
they do not recognize; Sign-in history, also in the account menu, lists their own recent sign-ins,
sign-outs and failed attempts. A superuser ends a person's sessions from the admin console, with a reason,
and marks a password someone else may know as compromised, which ends its sessions and makes the
person choose a new one.
Administrators can publish announcements for staff, clients or the sign-in page. Anyone visiting
sign-in can read an announcement addressed there. The Help drawer describes these audiences and
the controls on each page. [Product scope and record keeping](product.md#record-keeping-and-laboratory-policy)
