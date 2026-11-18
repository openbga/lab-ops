"""The account the scheduled jobs act as.

Timeline entries and audit events always name an account. A change a scheduled job makes (a
resolved ticket closed after the laboratory's period, a breached service target noticed) names
this one, "Scheduled jobs", so nobody's name is put to a change they did not make.

It is created the first time a job needs it: inactive, with no usable password, no role, no
team and an address in the reserved .invalid domain (RFC 6761), so nobody can sign in as it, be
given work or receive mail as it. Sign-in refuses it even if someone activates it and sets a
password in the admin console (accounts.adapter). Its id is kept in JobSettings.system_account
and never looked up by name, so a person's account with a similar name is never taken for it.
Creating it is audited.
"""

from django.db import connection, transaction

from core.audit import record_event

from .models import JobSettings

USERNAME = "scheduled-jobs"
EMAIL_DOMAIN = "lab-ops.invalid"


def locked_settings():
    """The settings row, created with its defaults if absent, locked for this transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError("Lock the scheduled job settings inside a transaction.")
    JobSettings.objects.get_or_create(pk=1)
    return JobSettings.objects.select_for_update().get(pk=1)


def _free(field, base, template):
    from accounts.models import User

    value, n = base, 1
    while User.objects.filter(**{f"{field}__iexact": value}).exists():
        n += 1
        value = template.format(n=n)
    return value


def system_account():
    """The scheduled jobs' account, created on first use."""
    from accounts.models import User

    with transaction.atomic():
        settings_row = locked_settings()
        if settings_row.system_account_id is not None:
            return User.objects.get(pk=settings_row.system_account_id)
        user = User(
            username=_free("username", USERNAME, USERNAME + "-{n}"),
            email=_free("email", f"{USERNAME}@{EMAIL_DOMAIN}", USERNAME + "-{n}@" + EMAIL_DOMAIN),
            first_name="Scheduled",
            last_name="jobs",
            is_active=False,
        )
        user.set_unusable_password()
        user.save()
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


def is_system_account(user):
    """True only for the account recorded as the scheduled jobs' own."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    return JobSettings.objects.filter(pk=1, system_account_id=user.pk).exists()
