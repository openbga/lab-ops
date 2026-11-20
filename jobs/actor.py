"""The account the scheduled jobs act as.

Timeline entries and audit events always name an account. A change a scheduled job makes (a
resolved ticket closed after the laboratory's period, a breached service target noticed) names
this one, "Scheduled jobs", so nobody's name is put to a change they did not make.

It is created the first time a job needs it: inactive, with no usable password, no role, no
team and an address in the reserved .invalid domain (RFC 6761), so nobody can sign in as it, be
given work or receive mail as it. Sign-in refuses it even if someone activates it and sets a
password in the admin console (accounts.adapter). Its id is kept in JobSettings.system_account
and never looked up by name, so a person's account with a similar name is never taken for it.
Its name and address are a base name with a random suffix chosen when it is created, so no
person's account, saved already or being created at that moment, holds either of them in any
letter case: the first run never waits for a person's account being created, and the two never
differ only in case. Creating it is audited.
"""

import secrets

from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Q

from core.audit import record_event

from .locks import is_lock_unavailable
from .models import JobSettings

USERNAME = "scheduled-jobs"
EMAIL_DOMAIN = "lab-ops.invalid"


# The advisory lock under which the settings row is created (PostgreSQL's two-key form; the first
# key is this lock's own): taken by every creation, so a job asked not to wait can tell that one is
# under way without waiting for its insert.
SETTINGS_CREATION = (19557, 0)


class SettingsBusy(Exception):
    """The job settings row is being saved or created by another transaction right now."""


def locked_settings(nowait=False):
    """The settings row, created with its defaults if absent, locked for this transaction. With
    `nowait`, a row someone is saving or creating raises SettingsBusy at once instead of waiting
    (the transaction must then be rolled back)."""
    if not connection.in_atomic_block:
        raise RuntimeError("Lock the scheduled job settings inside a transaction.")
    if not JobSettings.objects.filter(pk=1).exists():
        with connection.cursor() as cursor:
            if nowait:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", list(SETTINGS_CREATION))
                if not cursor.fetchone()[0]:
                    raise SettingsBusy()
            else:
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(SETTINGS_CREATION))
        JobSettings.objects.get_or_create(pk=1)
    try:
        return JobSettings.objects.select_for_update(nowait=nowait).get(pk=1)
    except OperationalError as exc:
        if nowait and is_lock_unavailable(exc):
            raise SettingsBusy() from exc
        raise


def _identity():
    """A name and address for the jobs' account that no account holds, in any letter case. The
    suffix is random, so no person's account being created at this moment holds them: the check
    below cannot see an account that is not committed yet, and an insert with its name would wait
    for it. The check covers the accounts already saved."""
    from accounts.models import User

    for _attempt in range(5):
        username = f"{USERNAME}-{secrets.token_hex(4)}"
        email = f"{username}@{EMAIL_DOMAIN}"
        taken = Q(username__iexact=username) | Q(email__iexact=email)
        if not User.objects.filter(taken).exists():
            return username, email
    raise RuntimeError("No free name for the scheduled jobs' account.")


def system_account(nowait=False):
    """The scheduled jobs' account, created on first use. With `nowait`, None at once when it
    must be created while someone is saving or creating the job settings, instead of waiting."""
    from accounts.models import User

    # Once created, its id never changes: read it without the settings row's lock, which an
    # administrator saving the job settings holds, so a job does not wait for them.
    existing = system_account_id()
    if existing is not None:
        user = User.objects.filter(pk=existing).first()
        if user is not None:
            return user
    try:
        return _create_system_account(nowait)
    except SettingsBusy:
        if not nowait:
            raise
        return None


def _create_system_account(nowait):
    from accounts.models import User

    with transaction.atomic():
        settings_row = locked_settings(nowait=nowait)
        if settings_row.system_account_id is not None:
            return User.objects.get(pk=settings_row.system_account_id)
        for attempt in range(5):
            # A name and address nobody holds (_identity); should an account saved since the
            # check hold one of them, the next attempt chooses again.
            username, email = _identity()
            user = User(
                username=username,
                email=email,
                first_name="Scheduled",
                last_name="jobs",
                is_active=False,
            )
            user.set_unusable_password()
            try:
                with transaction.atomic():
                    user.save()
                break
            except IntegrityError:
                if attempt == 4:
                    raise
        settings_row.system_account = user
        settings_row.save(update_fields=["system_account"])
        record_event(
            actor=user,
            action="jobs.system_account_created",
            obj=user,
            after={"username": user.username, "is_active": False},
            reason="Created for the scheduled jobs; nobody signs in as it.",
        )
        return user


def system_account_id():
    """The id of the scheduled jobs' account, or None before a job has needed it."""
    return JobSettings.objects.filter(pk=1).values_list("system_account_id", flat=True).first()


def is_system_account(user):
    """True only for the account recorded as the scheduled jobs' own."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    return JobSettings.objects.filter(pk=1, system_account_id=user.pk).exists()
