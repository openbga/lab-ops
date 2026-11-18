# OpenBGA Lab Ops workflows

This guide follows a ticket from the first call to closure. The Help drawer on each staff and
portal page lists its fields, actions, permissions and messages. A demonstration installation
uses synthetic accounts and records.

## Signing in and accounts

Sign in with the account the laboratory provides. A superuser creates accounts in **Accounts &
roles**. Staff and client portal accounts are distinct. Users can enroll an authenticator and
save recovery codes. Password resets and sign-in codes depend on the deployment's email settings.
[Sign-in configuration](operations.md#sign-in-limits-behind-a-proxy)

## Roles and capabilities

Staff can work tickets within their scope. Supervisors can work across restricted teams and
review eligible complaints. Client portal accounts belong to one organization. The separate
**Can view patient information** grant controls staff access to person-level links and marked
form answers. [Full access table](product.md#roles-and-capabilities)

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
Revealing it records an audit event. Open the source system separately to look up that person.

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

## Client journeys

### 13. Sign in and see open tickets

The portal home offers services and lists open tickets, including those waiting for you. Choose
**Tickets** for the fuller list. Access depends on the organization and sites assigned to your
account. A restricted ticket appears only to the client account that raised it.

### 14. Raise a ticket

Select a service, such as **Request a pickup**, or use **New ticket** for another topic. Choose
a site when asked. Fill the required details, include an order or sample number in **Your
reference** if relevant and send. Keep the ticket number for follow-up. If a form is still
available, **Request again** on a previous ticket starts another with eligible answers copied.
Check them and enter any dates, times, confirmations or patient details again.

### 15. Follow a ticket and reply

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

Staff can consult the laboratory profile, its recorded certificates and its retention statement.
Administrators can publish announcements for staff, clients or the sign-in page. Anyone visiting
sign-in can read an announcement addressed there. The Help drawer describes these audiences and
the controls on each page. [Product scope and record keeping](product.md#record-keeping-and-laboratory-policy)
