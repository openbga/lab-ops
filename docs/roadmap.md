# OpenBGA Lab Ops roadmap

This roadmap distinguishes the public release from planned work. The order may change as
laboratories use the software. There are no committed release dates.

## Built

The staff desk and client portal support tickets, conversations, attachments, assignments,
complaint review, service targets and saved views. Laboratories can manage client directories,
import them from CSV files, link tickets to external records and publish forms for pickups,
supply orders and other services. See the [product scope](product.md) and [workflows](workflows.md).

## Next

| Planned work | What it would add |
| --- | --- |
| Nonconformance and corrective action | An optional record linked to complaints, with containment, impact assessment, an action owner and due date, effectiveness review and closure. |
| Scheduled jobs and a notification outbox | Delivery retries, service clock breach notices and an optional interval for automatically closing resolved tickets. |
| Email channel | Send replies by email and receive messages through a provider webhook, associating a verified reply with its ticket. |
| Sign-off and controlled documents | Versioned documents, approval, effective dates, review dates and an attributed sign-off. |
| Read-and-acknowledge and training records | Assign a particular document version to people or teams and record training attendance and acknowledgement. |

## Later

| Area | Planned work |
| --- | --- |
| Desk and portal | Offer forms by organization type, show approved client instructions and support inbound email from a laboratory mailbox. |
| Integrations | Accept external record links through an authenticated, idempotent API. Support OpenID Connect sign-in for staff. |
| Quality records | Add competency assessments, task authorizations, proficiency testing, audit findings, management review and service reports. |
| Logistics | Add organization-specific catalogs, approval and fulfilment for supply orders, and recurring courier pickups with service windows. |
| Laboratory registers | Record equipment calibration and maintenance, and supplier or referral laboratory evaluations. |

Planned quality modules are optional. A laboratory would choose which modules to enable and set
its own intervals, approvers and retention rules. The desk would continue to work independently.
Controlled documents would come before training acknowledgements, which need a document version
to refer to.

## Exploring

Other proposals include ratings after a ticket is resolved, announcements targeted to selected
organizations, risk registers, document editing and lookup of live records in a laboratory
system. They have no scheduled implementation.
