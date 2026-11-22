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
force in a running deployment: password rules, failed sign-in limits, the session limits, who can
view patient information and the connection settings, with who sets each one. Administrators set
the session limits there. The statements below on
passwords, failed sign-ins, sessions and re-authentication describe this release's default
configuration (`config/settings.py`). The Security settings page follows the deployment settings
read from the environment; where a deployment has changed a setting in `config/settings.py` that
a statement depends on, the page says so and names the setting instead of describing it. The laboratory's
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
| Information system activity review (Required), (a)(1)(ii)(D) | Changes are recorded as attributed, append-only change events. Revealing a patient-marked answer or a person-level key, and an exact-key search, are recorded too; opening a ticket or downloading a file is not. Supervisors read the change history of one ticket, which does not list reveals. Completed sign-ins, refused passwords, wrong codes entered for an existing account, sign-outs made with Sign out and sessions ended by the session limits, a deactivation, a new password, the person from another session or a superuser are passed to the application's sign-in log, with the username when the account is known; some refusals are not (a code entered after its emailed sign-in attempt expired, a code for an address with no account, a request stopped by the request limit). There is no view across records. | The review procedure, its interval and its records. Retention and review of the host's and proxy's logs. | Access records for sensitive reads; sign-in history and re-authentication; audit review workspace |
| Authorization and supervision, workforce clearance, termination procedures (Addressable), (a)(3)(ii)(A)-(C) | Each person has a separate account. Work is limited by role, and patient information by an explicit grant that a superuser gives to a person or to a role group; no role has it otherwise. Deactivating an account stops its sign-in, refuses it at its next request and returns its open tickets to their team queues; a change the person is saving at that moment finishes first, and one they start afterwards is refused. Removing a role, a grant, a team membership or a portal site waits the same way; taking a capability from a role waits for its members one at a time, so a member it has not reached yet can still save a change before it. Deactivating also ends every session of the account at once, and they stay ended if it is made active again. A superuser can also end every session of an account from the admin console, with a reason, without deactivating it (on their own account, every one but the one in use). Changes to accounts, roles and grants in the admin console are recorded with the values before and after; an account page opened before another change of that account refuses to save over it. | Who may be given access, the clearance decision, and removing access when a person leaves. | Access report, time-limited grants and access review |
| Access authorization; access establishment and modification (Addressable), (a)(4)(ii)(B)-(C) | Roles and the patient-information capability are granted by a superuser in the admin console, and each grant or removal is a recorded change. Restricted ticket categories limit a ticket to the responsible team's members and supervisors. Portal accounts are limited to their organization or listed sites. | The access policy and periodic review of who holds what. | Access report and access review; staff access by team |
| Protection from malicious software (Addressable), (a)(5)(ii)(B) | Uploaded files are checked for size, count, extension and the structure of their type. This is a format check, not a malware scan. | Malware protection on workstations, the host and any file scanning. | Attachment scanning (Later) |
| Log-in monitoring (Addressable), (a)(5)(ii)(C) | By default, failed sign-ins are limited per username or email address typed and per network address, and each server process counts separately. Completed sign-ins, refused passwords, wrong codes entered for an existing account, sign-outs made with Sign out and sessions ended by the session limits, a deactivation, a new password, the person from another session or a superuser are passed to the application's sign-in log with the username when the account is known; a code entered after its emailed sign-in attempt expired, a code for an address with no account and a request stopped by the request limit are not, and this release's logging configuration writes them to the console for the host to keep. The database keeps each account's last sign-in time and, for each session, when it started, the browser and address it reported at sign-in, when it was last active and when and why it ended. Each person sees their own open sessions on Where you are signed in and can sign out the others. Ended sessions stay in the database with when and why they ended, and no page lists them; failed sign-ins are not stored. | Monitoring those logs and reporting discrepancies. | Sign-in history and re-authentication |
| Password management (Addressable), (a)(5)(ii)(D) | By default, passwords must have at least 8 characters, must not be a common password or only digits, and must not be close to the account's username, name or email address. A person resets a forgotten password by email when the deployment sends email. A new password ends the account's other sessions at once. | The password policy and training. | Required two-factor sign-in and password rules |
| Security incident procedures: response and reporting (Required), (a)(6)(ii) | Nothing specific. A nonconformance record, where that module is on, can describe a problem and its actions. | The incident procedure, its records and any breach notification. | Security incident and breach assessment records |
| Evaluation, (a)(8) | The Security settings page and this reference show what the application does, for the laboratory's evaluation. The [security policy](../SECURITY.md#what-is-checked) lists the checks the source and the container image go through, and each published release carries a software bill of materials of its image once that SBOM has passed its completeness check. | The periodic technical and nontechnical evaluation itself. | Each safeguards item updates both |
| Business associate contracts, (b) | Nothing. | Agreements with the host, the mail provider, backup storage and any other service that handles patient information. | Supplier and referral laboratory register (Later) |

## Technical safeguards, §164.312

| Safeguard | Lab Ops today | Stays with the laboratory and its host | Planned |
| --- | --- | --- | --- |
| Unique user identification (Required), (a)(2)(i) | Every account has its own username, and every change event names the account that made it. Shared demonstration accounts exist only on the public demonstration. | Giving each person their own account. | |
| Emergency access procedure (Required), (a)(2)(ii) | Nothing. Access follows roles and grants at all times. | The procedure for obtaining information in an emergency, including when the application is unavailable. | Emergency access |
| Automatic logoff (Addressable), (a)(2)(iii) | A session is signed out after a period without activity and after a longest session, both set by an administrator on the Security settings page, separately for staff and for client portal accounts, from 5 to 60 minutes and from 1 to 24 hours (30 minutes and 12 hours until set). The server checks both on every request. Opening a page, saving, searching or choosing Stay signed in is activity; reading or typing on a page is not. Pages warn 2 minutes before the sign-out for no activity and 5 minutes before the longest session ends; the admin console does not warn. Signing in again returns to the page. When a limit ends the session, what the person was typing in a ticket's reply, note or logged contact, a new ticket logged by staff, a portal reply or a new portal ticket is kept in the database for 24 hours, for that account in that browser only (a random value in a cookie of its own, holding nothing typed), never in the browser's storage, and given back once in the same form; files, fields that hold patient information and other forms are not kept, and Sign out discards it. Every page shown to a signed-in person is marked not to be stored by the browser. Without Remember me at a password sign-in, the session cookie also ends when the browser closes. Each person can see where they are signed in and sign out their other sessions, and a superuser can end a person's sessions with a reason. | The assessment of this specification, the periods chosen, and workstation locking. | Automatic sign-out and session control: keeping what was typed in the nonconformance record and its actions, opened in a dialog |
| Encryption and decryption (Addressable), (a)(2)(iv) | Nothing inside the application. Authenticator secrets and recovery codes are stored unencrypted in the database. | Encryption of the database, its storage and its backups at the host, and protection of database dumps. | Encryption of sign-in secrets and patient information |
| Audit controls, (b) | Change events record who did what, when, to which record and why. Revealing a person-level key or a patient-marked answer, and an exact-key search, are recorded without the value. Opening a ticket, downloading a file and a portal account being shown a patient-marked answer are not recorded. | Log collection and retention at the host. | Access records for sensitive reads |
| Integrity; mechanism to authenticate (Addressable), (c)(1)-(2) | Database triggers refuse changes and deletions of history, form values and file bytes. Files are stored with a SHA-256 checksum. Someone who controls the database can disable the triggers, so this is not tamper evidence. | Restricting database administration and keeping protected backups. | Tamper-evident audit records |
| Person or entity authentication, (d) | A password, and an optional authenticator app with recovery codes, which each account's holder can add; shared demonstration accounts cannot. By default, adding or changing the second factor needs a recent re-authentication. Where the deployment sends email, a code sent to the account's address can take the place of the password, and the second factor is still asked for. | Whether to require a second factor until the application can. | Required two-factor sign-in and password rules |
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
