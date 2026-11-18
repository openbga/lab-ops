# OpenBGA Lab Ops safeguards reference

This page lists the safeguards that the [roadmap](roadmap.md) cites. For each one it states what
the application records or enforces in this release and what stays with the laboratory and its
host. It describes the software. It does not say that a laboratory complies with any rule, and no
part of the application scores or certifies compliance. Each laboratory decides which rules apply
to it and makes its own assessment.

The citations are United States federal text: 45 CFR Part 164, the HIPAA Security and Privacy
Rules, in the [eCFR](https://www.ecfr.gov/current/title-45/part-164) as of 2026-11-16, read on
2026-11-18. "Required" and "Addressable" are the rule's own labels. For an addressable
specification the laboratory assesses whether it is reasonable and appropriate. It then implements
it, or documents why not and implements an equivalent alternative where that is reasonable and
appropriate (§164.306(d)(3)). The proposed changes to the Security Rule (90 FR 898, January 2025)
are not in force and are not described here.

The **Security settings** page in the application (Setup, administrators) shows the values in
force in a running deployment: password rules, failed sign-in limits, session length, who can view
patient information and the connection settings, with who sets each one. The statements below on
passwords, failed sign-ins, sessions and re-authentication describe this release's default
configuration (`config/settings.py`). A deployment that changes that configuration should read the
values on the Security settings page, which are worked out from the configuration it runs. The laboratory's
security contact and privacy contact are recorded on the laboratory profile, where every staff
member can read them.

Each roadmap item that changes a safeguard updates its row here in the same change and adds the
laboratory's settings for it to the Security settings page.

## Who is responsible

| Safeguard | Lab Ops today | Stays with the laboratory and its host |
| --- | --- | --- |
| Assigned security responsibility, §164.308(a)(2) | The laboratory profile records a security contact: a name or office, a title, an email address and a phone number. Every staff member reads it. An administrator records or changes it with a reason, and the change is kept in the profile history. | Identifying the security official and documenting the designation. The application names no one and checks nothing against the contact. |
| Personnel designations, §164.530(a) | The laboratory profile records a privacy contact in the same way. | Designating the privacy official and the contact person or office for complaints, and documenting both (§164.530(a)(2), (j)). |

## Administrative safeguards, §164.308

| Safeguard | Lab Ops today | Stays with the laboratory and its host | Planned |
| --- | --- | --- | --- |
| Information system activity review (Required), (a)(1)(ii)(D) | Changes are recorded as attributed, append-only change events. Revealing a patient-marked answer or a person-level key, and an exact-key search, are recorded too; opening a ticket or downloading a file is not. Supervisors read the change history of one ticket, which does not list reveals. Sign-ins, failures and sign-outs go to the application log, with the username. There is no view across records. | The review procedure, its interval and its records. Retention and review of the host's and proxy's logs. | Access records for sensitive reads; sign-in history and re-authentication; audit review workspace |
| Authorization and supervision, workforce clearance, termination procedures (Addressable), (a)(3)(ii)(A)-(C) | Each person has a separate account. Work is limited by role, and patient information by an explicit grant that no role includes. Deactivating an account stops its sign-in, refuses it at its next request and returns its open tickets to their team queues. Its sessions are not deleted: made active again before they expire, the account is signed in again on those browsers. Changes to accounts, roles and grants in the admin console are recorded with the values before and after. | Who may be given access, the clearance decision, and removing access when a person leaves. | Access report, time-limited grants and access review |
| Access authorization; access establishment and modification (Addressable), (a)(4)(ii)(B)-(C) | Roles and the patient-information capability are granted by a superuser in the admin console, and each grant or removal is a recorded change. Restricted ticket categories limit a ticket to the responsible team's members and supervisors. Portal accounts are limited to their organization or listed sites. | The access policy and periodic review of who holds what. | Access report and access review; staff access by team |
| Protection from malicious software (Addressable), (a)(5)(ii)(B) | Uploaded files are checked for size, count, extension and the structure of their type. This is a format check, not a malware scan. | Malware protection on workstations, the host and any file scanning. | Attachment scanning (Later) |
| Log-in monitoring (Addressable), (a)(5)(ii)(C) | By default, failed sign-ins are limited per username or email address typed and per network address, and each server process counts separately. Sign-ins, failures and sign-outs are written to the application log. The database keeps only each account's last sign-in time. | Monitoring those logs and reporting discrepancies. | Sign-in history and re-authentication |
| Password management (Addressable), (a)(5)(ii)(D) | By default, passwords must have at least 8 characters, must not be a common password or only digits, and must not be close to the account's username, name or email address. A person resets a forgotten password by email when the deployment sends email. A new password signs out the account's other sessions. | The password policy and training. | Required two-factor sign-in and password rules |
| Security incident procedures: response and reporting (Required), (a)(6)(ii) | Nothing specific. A nonconformance record, where that module is on, can describe a problem and its actions. | The incident procedure, its records and any breach notification. | Security incident and breach assessment records |
| Evaluation, (a)(8) | The Security settings page and this reference show what the application does, for the laboratory's evaluation. | The periodic technical and nontechnical evaluation itself. | Each safeguards item updates both |
| Business associate contracts, (b) | Nothing. | Agreements with the host, the mail provider, backup storage and any other service that handles patient information. | Supplier and referral laboratory register (Later) |

## Technical safeguards, §164.312

| Safeguard | Lab Ops today | Stays with the laboratory and its host | Planned |
| --- | --- | --- | --- |
| Unique user identification (Required), (a)(2)(i) | Every account has its own username, and every change event names the account that made it. Shared demonstration accounts exist only on the public demonstration. | Giving each person their own account. | |
| Emergency access procedure (Required), (a)(2)(ii) | Nothing. Access follows roles and grants at all times. | The procedure for obtaining information in an emergency, including when the application is unavailable. | Emergency access |
| Automatic logoff (Addressable), (a)(2)(iii) | By default, no sign-out after a period without activity. A session lasts two weeks from when it was last saved. Without Remember me at a password sign-in, the session cookie ends when the browser closes; a sign-in with an emailed code keeps its cookie. | The assessment of this specification, and workstation locking. | Automatic sign-out and session control |
| Encryption and decryption (Addressable), (a)(2)(iv) | Nothing inside the application. Authenticator secrets and recovery codes are stored unencrypted in the database. | Encryption of the database, its storage and its backups at the host, and protection of database dumps. | Encryption of sign-in secrets and patient information |
| Audit controls, (b) | Change events record who did what, when, to which record and why. Revealing a person-level key or a patient-marked answer, and an exact-key search, are recorded without the value. Opening a ticket, downloading a file and a portal account being shown a patient-marked answer are not recorded. | Log collection and retention at the host. | Access records for sensitive reads |
| Integrity; mechanism to authenticate (Addressable), (c)(1)-(2) | Database triggers refuse changes and deletions of history, form values and file bytes. Files are stored with a SHA-256 checksum. Someone who controls the database can disable the triggers, so this is not tamper evidence. | Restricting database administration and keeping protected backups. | Tamper-evident audit records |
| Person or entity authentication, (d) | A password, and an optional authenticator app with recovery codes, which each person chooses. By default, adding or changing the second factor needs a recent re-authentication. Where the deployment sends email, a code sent to the account's address can take the place of the password, and the second factor is still asked for. | Whether to require a second factor until the application can. | Required two-factor sign-in and password rules |
| Transmission security; integrity controls and encryption (Addressable), (e) | With debug mode off, plain HTTP is redirected to HTTPS, browsers are told to keep using HTTPS (for one hour unless the deployment sets `SECURE_HSTS_SECONDS`), and cookies are sent over HTTPS only. Email notices carry the ticket number, a fixed event description, the laboratory's name and a sign-in link, never ticket content. | TLS at the host and proxy, and the mail route's encryption. | Email channel: replies out, inbound by webhook |

## Privacy Rule items the roadmap cites

| Rule | Lab Ops today | Stays with the laboratory | Planned |
| --- | --- | --- | --- |
| Minimum necessary, §164.502(b) and §164.514(d) | Patient-marked answers are revealed one at a time to staff who hold the explicit grant. Person-level links are masked. Restricted categories and portal scope limit who reads a ticket. Patient information is kept out of email, lists, logs, audit details and general search; staff who hold the grant can find a person-level link by its exact key. Any staff member can read an unrestricted ticket. | Which classes of staff need which information. | Staff access by team |
| Individual rights, accounting and complaints, §164.522-528 and §164.530(d) | Nothing specific. | The procedures, their records and their deadlines. | Privacy requests and identity verification; disclosure log |

## Outside the application

These stay with the laboratory and whoever hosts the deployment, whatever the application does:
the risk analysis and risk management (§164.308(a)(1)(ii)(A)-(B)), the sanction policy, security
awareness and training, the contingency plan with backups, disaster recovery and emergency mode
operation (§164.308(a)(7)), physical safeguards for facilities, workstations and media (§164.310),
policies and their documentation (§164.316), and the retention of required documentation for six
years (§164.316(b)(2), §164.530(j)). [Operations](operations.md) covers backups, restores, logs
and the deployment settings the application reads.
