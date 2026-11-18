"""Opaque original-state tokens for forms editing existing configuration records, and one
reading of the database for pages whose parts must agree."""

import json
from contextlib import contextmanager

from django import forms
from django.core.exceptions import ValidationError
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
        yield
