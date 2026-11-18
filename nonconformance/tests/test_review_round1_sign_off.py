"""Sign-off checks for the stale-form message and the request-specific URL configuration."""

import pytest
from django.http import HttpResponseNotFound
from django.urls import include, path, reverse

from desk import services
from desk.models import ComplaintReview

from .test_review_round1 import at_read, form_revision

STALE_TICKET = "This ticket changed. Reload it and review the latest entries."

# The same desk under a site prefix, selected per request by a normal Django middleware.
urlpatterns = [path("lab/", include("config.urls"))]


def prefixed_desk(get_response):
    def handle(request):
        request.urlconf = __name__
        return get_response(request)

    return handle


def handler404(request, exception):
    return HttpResponseNotFound("Sign-off desk page not found")


@pytest.mark.django_db
def test_disabled_slashless_address_uses_the_prefixed_desks_ordinary_404(client, settings):
    settings.DEBUG = False
    settings.MIDDLEWARE = [f"{__name__}.prefixed_desk", *settings.MIDDLEWARE]
    url = reverse("nonconformance:list", urlconf=__name__).rstrip("/")
    assert client.get(reverse("health", urlconf=__name__), secure=True).status_code == 200
    unknown = client.get("/lab/unknown-sign-off", secure=True)
    assert unknown.status_code == 404 and unknown.content == b"Sign-off desk page not found"
    off = client.get(url, secure=True)
    assert (off.status_code, off.get("Location")) == (unknown.status_code, unknown.get("Location"))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("when", "decision"),
    [("before_post", "investigate"), ("before_post", ""), ("during_post", "")],
    ids=["complete", "incomplete", "changed-during-post"],
)
def test_desk_shows_one_reload_message_for_the_revision_it_redraws(
    client, complaint, agent, lead, when, decision
):
    client.force_login(agent)
    url = reverse("desk:complaint-decision", args=[complaint.pk])
    revision = form_revision(client.get(url))
    draft = {
        "expected_revision": revision,
        "decision": decision,
        "rationale": "Check calibration records",
    }

    def advance_ticket():
        services.add_entry(
            actor=lead,
            request=complaint,
            expected_revision=revision,
            kind="note",
            body="Bench checked",
        )

    if when == "before_post":
        advance_ticket()
        response = client.post(url, draft)
    else:
        # The missing decision forces a redraw. A second connection commits after the first
        # ticket read, so the redraw can report the newer revision without saving the draft.
        with at_read(
            lambda sql: 'FROM "desk_servicerequest"' in sql and "opened_number" in sql,
            advance_ticket,
        ) as committed:
            response = client.post(url, draft)
        assert committed == [True]

    complaint.refresh_from_db()
    assert response.status_code == 200
    assert response.context["req"].revision == complaint.revision == revision + 1
    form = response.context["form"]
    assert int(form["expected_revision"].value()) == revision
    assert form["rationale"].value() == draft["rationale"]
    assert not ComplaintReview.objects.filter(
        request=complaint, rationale=draft["rationale"]
    ).exists()
    errors = list(form.non_field_errors())
    assert errors.count(STALE_TICKET) == 1
