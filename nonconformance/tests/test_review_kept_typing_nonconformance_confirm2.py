"""Compare the returned New ticket with a fresh rendering after its old choices disappear."""

import pytest
from django import forms
from django.urls import reverse

from desk import views as desk
from desk.tests.test_kept_typing import browser_for
from nonconformance.tests.test_review_kept_typing_nonconformance import PostedControls, keep_page

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("old_answer", ["removed-box", "withdrawn-choice", "surviving-subject"])
def test_new_ticket_restores_its_defaults_when_no_answer_can_be_drawn(
    agent, clinic, monkeypatch, old_answer
):
    # The old layout offered a priority that has since been withdrawn, a dispatch box,
    # and explicit empty options for the channel and first entry. Render and keep it normally.
    class EarlierTicket(desk.RequestCreateForm):
        dispatch = forms.CharField(required=False)
        priority = forms.ChoiceField(choices=[("", "Choose"), ("on-call", "On call")])
        channel = forms.ChoiceField(choices=[("", "Choose")])
        first_entry_kind = forms.ChoiceField(choices=[("", "Choose")])
        groups = (*desk.RequestCreateForm.groups, ("Dispatch", (("dispatch", "col-12"),)))

    url = reverse("desk:request-create") + f"?client={clinic.pk}"
    browser = browser_for(agent)
    with monkeypatch.context() as previous:
        previous.setattr(desk, "RequestCreateForm", EarlierTicket)
        opened = browser.get(url)
        answers = {"client": "", "dispatch": "Call the loading dock."}
        if old_answer == "withdrawn-choice":
            answers["priority"] = "on-call"
        if old_answer == "surviving-subject":
            answers["subject"] = "Leave the cartons at reception."
        keep_page(browser, agent, opened, answers)

    returned = browser.get(url)
    fresh = browser.get(url)
    restored = PostedControls(returned)
    defaults = PostedControls(fresh)
    assert defaults.values["client"] == str(clinic.pk)
    assert defaults.values["channel"] and defaults.values["first_entry_kind"]
    assert "Call the loading dock." not in returned.text
    assert returned.context["kept"]["missed"]
    if old_answer == "surviving-subject":
        assert not returned.context["kept"]["nothing"]
        assert restored.values["client"] == ""
        assert restored.values["subject"] == answers["subject"]
    else:
        assert returned.context["kept"]["nothing"]
        assert {name: restored.values.get(name) for name in restored.keep_names} == {
            name: defaults.values.get(name) for name in defaults.keep_names
        }
