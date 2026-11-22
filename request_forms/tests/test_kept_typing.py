"""What a client was typing in a request form, kept when a session limit signs them out
(accounts.kept): the fields that hold patient information, item lists and files are never kept or
given back. All data is synthetic."""

import html
import json
import re

import pytest
from django.db.models.functions import Now
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft

from .conftest import PATIENT_SENTINEL

pytestmark = pytest.mark.django_db


def names_and_token(page):
    text = page.content.decode()
    found = re.search(
        r'name="_vr_kept" value="([^"]+)" data-vr-kept [^>]*data-vr-kept-fields="([^"]*)"', text
    )
    return found.group(1), json.loads(html.unescape(found.group(2)))


def test_a_request_form_keeps_typed_fields_but_never_patient_information(portal_user, pickup):
    page = reverse("portal_forms:raise", args=[pickup.key])
    browser = BrowserClient()
    browser.force_login(portal_user)
    token, names = names_and_token(browser.get(page))
    # Client fields a person types in or chooses from (each quantity box of the item list by its
    # own name), then the common fields; not the patient identifier, the files or the hidden
    # definition token.
    items = [
        f"field-items_{pk}"
        for pk in pickup.fields.get(key="items")
        .catalog.items.filter(is_active=True)
        .values_list("pk", flat=True)
    ]
    assert sorted(names) == sorted(
        [
            "client_reference",
            "description",
            "field-count",
            "field-handling",
            "field-ready-at",
            "subject",
            "urgent",
            *items,
        ]
    )
    assert "field-patient" not in kept._read(token)["f"]
    AccountSession.objects.filter(user=portal_user, ended_at__isnull=True).update(
        ended_at=Now(), ending=AccountSession.Ending.INACTIVITY
    )
    sent = {"field-count": "3", "description": "Two coolers", "field-patient": PATIENT_SENTINEL}
    response = browser.post(
        reverse("account-session-keep"),
        data=json.dumps({"forms": [{"token": token, "values": sent}]}),
        content_type="application/json",
    )
    assert response.json() == {"kept": 1, "held": [True]}
    assert PATIENT_SENTINEL not in json.dumps(KeptDraft.objects.get().values)
    browser.force_login(portal_user)
    again = browser.get(page).content.decode()
    assert "Two coolers" in again and PATIENT_SENTINEL not in again


def test_a_patient_field_posted_after_the_sign_out_is_not_kept(portal_user, pickup):
    page = reverse("portal_forms:raise", args=[pickup.key])
    browser = BrowserClient()
    browser.force_login(portal_user)
    token, _names = names_and_token(browser.get(page))
    AccountSession.objects.filter(user=portal_user, ended_at__isnull=True).update(
        last_active_at=Now() - kept.timedelta(minutes=31)
    )
    response = browser.post(
        page,
        {
            kept.FIELD: token,
            "field-count": "2",
            "field-patient": PATIENT_SENTINEL,
            "description": "Before the courier",
        },
    )
    assert response.status_code == 302
    values = KeptDraft.objects.get().values
    assert values["description"] == "Before the courier" and "field-patient" not in values
