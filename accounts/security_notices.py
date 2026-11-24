"""Email notices to the account holder about changes to how their account signs in.

Events, each a notice to the account whose sign-in factors changed:

- `password_marked_compromised`: a superuser marked the account's password as compromised
  (accounts.compromised.mark_compromised);
- `authenticator_removed`: a superuser removed the account's authenticator app or its recovery
  codes in the account administration (accounts.admin.AuthenticatorAdmin), or the person removed
  their own authenticator app, which removes its recovery codes with it (accounts.mfa_views);
- `authenticator_added`: the person added an authenticator app (accounts.mfa_views);
- `recovery_codes_replaced`: a new set of recovery codes was issued, replacing any earlier set,
  which no longer works: on the person's request, or with an authenticator app they added
  (accounts.mfa_views).

Recording: `record()` is called inside the change's transaction, after its writes and audit
event, and records one notice in the outbox (jobs.outbox) with every event the change raised for
the account: adding an app that also issues recovery codes is one notice with both events, worded
as the app added. A change that rolls back or is refused records nothing. The notice row carries
codes only (Notice.data): `at`, the time of the change as an ISO timestamp; `by`, "administrator"
or "self"; `types`, the kinds of factor added or removed ("totp", "recovery_codes"); `mark`, the
compromised mark's id. Its reference is "Account", never a name or an address. Email off: the
outbox records the notice as Not sent, and nothing else is sent.

Sending (`prepare_security_notice`, at each attempt): the account's row is held shared until the
notice is sent, and the notice no longer applies when the account no longer exists, is inactive,
or has no email address. Otherwise it is worded from the allow-list below: what changed (by an
administrator, when one made it; a change made while signed in to the account is not said to be
the person's own), the account's username, when (in UTC), and whom to contact if it was not the
person: the laboratory's security contact as its profile records it at sending
(core.models.LabContact), or "your laboratory's administrator" when none is recorded. Never a
code, secret, network address, browser or ticket content, and no link but the site's address.
These notices are sent wherever the deployment sends email, whatever the person chose for their
own email notices (Email notices, desk.notifications.OPT_OUT_KEY): they are how the person learns
of a change they did not make.

Basis (recommendations this product follows, not requirements it claims to meet): NIST SP
800-63B-4 (2025) §4.1.2.1 (notify the subscriber, by a mechanism independent of the transaction,
when an authenticator is bound), §4.2 (issuing replacement recovery codes is an account recovery
event that is notified), §4.5 (notify when an authenticator is invalidated) and §4.6 (the
notification gives clear instructions, with contact information, in case the recipient
repudiates the event).
"""

from datetime import UTC, datetime

from django.utils import timezone

TOPIC = "accounts.security"  # jobs.registry.NOTICE_TOPICS
REFERENCE = "Account"  # Notice.reference: shown in place of a ticket number, never identifying

COMPROMISED = "password_marked_compromised"
REMOVED = "authenticator_removed"
ADDED = "authenticator_added"
CODES_REPLACED = "recovery_codes_replaced"
# When one change raises several events, the notice is worded for the first of them here.
PRECEDENCE = (COMPROMISED, REMOVED, ADDED, CODES_REPLACED)
BY = frozenset({"administrator", "self"})

TOTP = "totp"
RECOVERY_CODES = "recovery_codes"

SUBJECTS = {
    COMPROMISED: "your password was marked as compromised",
    (REMOVED, TOTP): "an authenticator app was removed from your account",
    (REMOVED, RECOVERY_CODES): "your recovery codes were removed from your account",
    (REMOVED, None): "a sign-in factor was removed from your account",
    ADDED: "an authenticator app was added to your account",
    CODES_REPLACED: "new recovery codes were issued for your account",
}
FALLBACK_CONTACT = "your laboratory's administrator"


# Recording (in the change's transaction) -----------------------------------------------------


def record(account, events, *, by, types=(), mark=None):
    """Record the notice of `events` (codes above) for `account` in the current transaction, and
    give it its first attempt once the transaction commits. `by` is "administrator" or "self";
    `types` the kinds of factor the change added or removed; `mark` a compromised mark's id."""
    from jobs import outbox

    if by not in BY:
        raise ValueError(f"Unknown actor kind: {by!r}")
    data = {"at": timezone.now().astimezone(UTC).isoformat(), "by": by}
    if types:
        data["types"] = sorted(set(types))
    if mark is not None:
        data["mark"] = mark
    notice = outbox.record(
        recipient=account,
        topic=TOPIC,
        object_id=account.pk,
        reference=REFERENCE,
        events=events,
        data=data,
    )
    outbox.send_after_commit([notice.pk])
    return notice


# Checked and worded at each sending attempt (jobs.outbox) --------------------------------------


def _when(notice):
    """The time of the change, as recorded, in UTC: 2026-11-24 14:05 UTC."""
    try:
        moment = datetime.fromisoformat(str(notice.data.get("at")))
        if timezone.is_naive(moment):
            raise ValueError
    except TypeError, ValueError:
        moment = notice.created_at
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def security_contact():
    """Whom to contact about a change the person did not make: the laboratory's security contact,
    with its email address and phone number when recorded, or FALLBACK_CONTACT."""
    from core.models import LabContact

    contact = LabContact.objects.filter(kind=LabContact.Kind.SECURITY).first()
    if contact is None or not contact.is_recorded:
        return FALLBACK_CONTACT
    reach = ", ".join(value for value in (contact.email, contact.phone) if value)
    return f"the laboratory's security contact, {contact.name}" + (f" ({reach})" if reach else "")


def _compromised_lines(account, notice, username, when):
    from allauth.account import app_settings as account_settings

    from core.second_factor import has_second_factor

    from .compromised import open_mark

    lines = [
        f"An administrator marked the password of your account {username} as compromised on "
        f"{when}, and signed out every session of the account."
    ]
    mark = open_mark(account.pk)
    if mark is None or mark.pk != notice.data.get("mark"):
        lines.append("A new password has been chosen for the account since.")
    elif has_second_factor(account):
        lines.append(
            "Sign in with your password and your second sign-in factor: you will be asked to "
            "choose a new password before anything else."
        )
    else:
        by_code = (
            ', or choose "Email me a sign-in code" and then a new password'
            if account_settings.LOGIN_BY_CODE_ENABLED
            else ""
        )
        lines.append(
            "Your password no longer signs you in. Choose a new one with "
            f'"Forgot your password?" on the sign-in page{by_code}.'
        )
    return lines


def _removed_line(types, by, username, when):
    if TOTP in types and RECOVERY_CODES in types:
        what, verb = "the authenticator app and its recovery codes", "were"
    elif TOTP in types:
        what, verb = "the authenticator app", "was"
    elif RECOVERY_CODES in types:
        what, verb = "the recovery codes", "were"
    else:
        what, verb = "a sign-in factor", "was"
    if by == "administrator":
        return f"An administrator removed {what} from your account {username} on {when}."
    # Said of the account, not of the person: they may not be the one who did it.
    return f"{what[0].upper()}{what[1:]} {verb} removed from your account {username} on {when}."


def _subject_key(event, types):
    if event != REMOVED:
        return event
    return (REMOVED, TOTP if TOTP in types else RECOVERY_CODES if RECOVERY_CODES in types else None)


def word(account, notice, events):
    """(subject, body) of the notice for `account`, worded for the most significant of `events`
    (PRECEDENCE) from the allow-list."""
    from desk.notifications import base_url, lab_name

    event = next(e for e in PRECEDENCE if e in events)
    types = [t for t in notice.data.get("types") or [] if t in (TOTP, RECOVERY_CODES)]
    by = notice.data.get("by")
    username = account.get_username()
    when = _when(notice)
    laboratory = lab_name()
    if event == COMPROMISED:
        lines = _compromised_lines(account, notice, username, when)
    elif event == REMOVED:
        lines = [_removed_line(types, by, username, when)]
    elif event == ADDED:
        lines = [f"An authenticator app was added to your account {username} on {when}."]
        if CODES_REPLACED in events:
            lines.append("New recovery codes were issued with it.")
    else:
        lines = [
            f"New recovery codes were issued for your account {username} on {when}. Any earlier "
            "recovery codes no longer work."
        ]
    lines.append(
        f"If you did not make or expect this change, contact {security_contact()} straight away."
    )
    subject = f"{laboratory}: {SUBJECTS[_subject_key(event, types)]}"
    footer = (
        f"{laboratory}\n{base_url()}\n\nThis notice is about how your account signs in, so it is "
        "sent whatever you chose for your email notices."
    )
    return subject, "\n\n".join([*lines, footer]) + "\n"


def prepare_security_notice(notice):
    """`notice` (topic accounts.security) worded for sending, or None when it no longer applies:
    the account no longer exists, is inactive or has no email address. The account's row is held
    shared until the notice is sent (jobs.outbox)."""
    from jobs.locks import share_lock
    from jobs.outbox import Prepared

    from .models import User

    events = [e for e in PRECEDENCE if e in (notice.events or [])]
    if not events or notice.object_id != notice.recipient_id:
        return None
    if not share_lock(User, [notice.recipient_id]):
        return None
    account = User.objects.filter(pk=notice.recipient_id).first()
    if account is None or not account.is_active or not account.email:
        return None
    subject, body = word(account, notice, events)
    return Prepared(to=account.email, subject=subject, body=body, event=events[0])
