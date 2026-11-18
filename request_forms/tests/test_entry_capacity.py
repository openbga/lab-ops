"""The timeline event a staff correction of form values writes carries the laboratory's
capacity (desk.models.AuthorCapacity), recorded when it is written."""

import pytest

from desk.models import AuthorCapacity, EntryKind
from request_forms.services import correct_values, create_request_with_form

from .conftest import definition_of

pytestmark = pytest.mark.django_db


def test_corrected_values_event_is_the_laboratorys(portal_user, agent, pickup):
    request = create_request_with_form(
        actor=portal_user,
        template=pickup,
        expected_definition=definition_of(pickup),
        values={"count": 2},
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
    )
    assert request.entries.get(kind=EntryKind.REPLY).author_capacity == AuthorCapacity.CLIENT
    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"courier": "Route 4"},
        reason="",
        expected={"courier": None},
    )
    event = request.entries.get(event_action="form_values_changed")
    assert event.author_capacity == AuthorCapacity.LABORATORY
