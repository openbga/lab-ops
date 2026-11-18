# ruff: noqa: F811 - fixtures borrowed from request_forms.tests.conftest
"""Files on a request raised with a request form: attached in the same
transaction as the request and its form values."""

from django.urls import reverse

from attachments.models import Attachment
from desk.models import ServiceRequest
from request_forms.models import FieldValue
from request_forms.tests.conftest import (  # noqa: F401 - fixtures
    PASSWORD,
    catalog,
    clinic,
    definition_of,
    group,
    logistics,
    make_user,
    pickup,
    pickup_category,
    plain_static_storage,
    portal_user,
)

from .conftest import PDF, SAMPLES, upload


def raise_form(client, portal_user, **extra):
    from request_forms.models import RequestTemplate

    pickup = RequestTemplate.objects.get(key="pickup")
    client.login(username=portal_user.username, password=PASSWORD)
    return client.post(
        reverse("portal_forms:raise", args=["pickup"]),
        {
            "field-count": "2",
            "subject": "",
            "description": "",
            "client_reference": "",
            # The form's definition as drawn (request_forms reviews 3 and 5).
            "form_definition": definition_of(pickup),
            **extra,
        },
    )


def test_request_form_raise_takes_files(client, portal_user, pickup):
    client.login(username=portal_user.username, password=PASSWORD)
    page = client.get(reverse("portal_forms:raise", args=["pickup"])).content.decode()
    assert 'enctype="multipart/form-data"' in page and 'name="files"' in page
    response = raise_form(client, portal_user, files=[upload("cooler.png", SAMPLES["photo.png"])])
    assert response.status_code == 302
    request = ServiceRequest.objects.get()
    attachment = Attachment.objects.get()
    assert attachment.request == request and attachment.entry.kind == "reply"
    assert FieldValue.objects.filter(key="count").exists()


def test_bad_file_or_bad_values_create_nothing(client, portal_user, pickup):
    response = raise_form(client, portal_user, files=[upload("cooler.png", b"<svg/>")])
    assert response.status_code == 200 and "cooler.png:" in response.content.decode()
    response = raise_form(
        client, portal_user, **{"field-count": "", "files": [upload("a.pdf", PDF)]}
    )
    assert response.status_code == 200
    assert not ServiceRequest.objects.exists() and not Attachment.objects.exists()
    assert not FieldValue.objects.exists()
