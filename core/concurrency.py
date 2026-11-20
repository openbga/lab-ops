"""Opaque original-state tokens for forms editing existing configuration records, and one
reading of the database for pages whose parts must agree."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils.crypto import constant_time_compare, salted_hmac

from .audit import snapshot


def record_token(instance):
    payload = [instance._meta.label_lower, instance.pk, snapshot(instance)]
    return salted_hmac(
        "configuration.original_state", json.dumps(payload, sort_keys=True), algorithm="sha256"
    ).hexdigest()


def protect_form(form, instance):
    if instance and instance.pk:
        form.fields["original_state"] = forms.CharField(
            widget=forms.HiddenInput, initial=record_token(instance)
        )


def check_original_state(instance, expected):
    # Direct domain callers can omit a token; browser edit forms always require one.
    if expected is not None and (
        instance is None or not constant_time_compare(record_token(instance), expected)
    ):
        raise ValidationError(
            "This record changed after you opened it. Your input has been kept; "
            "reload and compare the current values before saving again."
        )


# Whether this thread's reads are inside a reading one_reading opened (its own transaction).
_IN_OWN_READING = ContextVar("in_own_reading", default=False)


@contextmanager
def one_reading():
    """Reads that must agree with each other, such as a page's panels and its history: one
    snapshot of the database. Outside a transaction this opens a REPEATABLE READ transaction, so
    every query inside sees the same committed state, whatever commits meanwhile. Inside a
    transaction already (a write, a test) it adds nothing and that transaction decides."""
    if connection.in_atomic_block:
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # Marked only while the reading's transaction is open: cleared before it commits, so
        # what runs once it has committed (after_reading's writes) is outside it.
        opened = _IN_OWN_READING.set(True)
        try:
            yield
        finally:
            _IN_OWN_READING.reset(opened)


def after_reading(write):
    """Run `write`, something a page saves while it is drawn, once the reading one_reading opened
    for the page has ended, or at once when none is open. A page drawn inside its reading thus
    writes nothing inside it, where a row changed since the snapshot would fail the write."""
    if _IN_OWN_READING.get():
        transaction.on_commit(write)
    else:
        write()


def reading_account(request):
    """The signed-in account as it stands in the current reading (core.concurrency.one_reading):
    its status, organization and roles decide access together with what the page shows. It
    becomes the request's user, with its groups, so every part of the page drawn after the
    reading (the account menu, the sidebar) shows the same account."""
    account = get_user_model().objects.prefetch_related("groups").filter(pk=request.user.pk).first()
    if account is None:
        raise PermissionDenied("Your account is no longer available.")
    request.user = account
    return account
