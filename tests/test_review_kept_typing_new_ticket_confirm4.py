"""Fourth confirmation of New ticket receipts and consistent reads, on synthetic PostgreSQL rows."""

from copy import deepcopy

import pytest
from django.db import connection, transaction
from django.db.models import F
from django.test import Client
from django.urls import reverse

from accounts import kept
from accounts.models import KeptDraft
from desk.models import ServiceRequest
from request_forms import intake
from request_forms.models import FieldKind, RequestTemplate
from taxonomy.models import RequestCategory
from tests import test_review_kept_typing as legacy
from tests import test_review_kept_typing_new_ticket as r
from tests import test_review_kept_typing_new_ticket_confirm as c
from tests import test_review_kept_typing_new_ticket_confirm2 as prior

pytestmark = pytest.mark.django_db(transaction=True)
agent = r.agent
make_user = r.make_user
group = r.group
logistics = r.logistics
pickup_category = r.pickup_category
catalog = r.catalog
pickup = r.pickup
clinic = r.clinic
other_org = r.other_org
review_form_cases = legacy.review_form_cases


def item(token, values):
    return {"token": token, "values": values}


def batch(browser, *items):
    answer = browser.post(
        reverse("account-session-keep"), {"forms": list(items)}, content_type="application/json"
    )
    assert answer.status_code == 200
    return answer.json()


def refused(browser, token, values):
    page = browser.post(
        r.PAGE,
        {**values, kept.FIELD: token},
        headers={"X-VR-Form": "1", "HX-Current-URL": "http://testserver/requests/"},
    )
    assert page.status_code == 401 and page["HX-Redirect"] == page.json()["sign_in"]
    return page.json()["kept"]


def test_a_text_copy_cannot_acknowledge_a_confirmation_tick_with_the_same_wire_value(agent, pickup):
    browser = r.pages.browser_for(agent)
    first = r.drawn_marker(r.section(browser, pickup))[0]
    pickup.fields.filter(key="courier").update(kind=FieldKind.CONFIRMATION)
    second = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    values = r.draft_values(pickup, **{"field-courier": "on"})
    assert batch(browser, item(first, values))["held"] == [True]
    stored = KeptDraft.objects.get()
    assert stored.controls["field-courier"] == "text"
    post = refused(browser, second, values)
    followup = batch(browser, item(second, values))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    field = page.context["intake_drawn"][1]["field-courier"]
    assert not field.value() and r.missed(page)
    assert (post, followup) == (False, {"kept": 1, "held": [False]})


def test_different_removed_answer_does_not_claim_an_earlier_complete_copy(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    pickup.fields.filter(key="courier").update(is_active=False)
    r.pages.end_open(agent)
    first = r.draft_values(pickup, **{"field-courier": ""})
    second = r.draft_values(pickup)
    assert batch(browser, item(token, first))["held"] == [True]
    assert not KeptDraft.objects.get().dropped
    post = refused(browser, token, second)
    followup = batch(browser, item(token, second))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert not r.missed(page) and r.ROUTE not in page.text  # first delivery had nothing to omit
    assert (post, followup["held"]) == (False, [False])


def test_an_unanswered_new_optional_field_does_not_make_the_same_typing_another_copy(agent, pickup):
    browser = r.pages.browser_for(agent)
    first = r.drawn_marker(r.section(browser, pickup))[0]
    pickup.fields.create(key="bay", label="Optional collection bay", kind=FieldKind.SHORT_TEXT)
    second = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    values = r.draft_values(pickup)
    assert batch(browser, item(first, values))["held"] == [True]
    later = {**values, "field-bay": ""}
    post = refused(browser, second, later)
    followup = batch(browser, item(second, later))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert r.SUBJECT in page.text and r.ROUTE in page.text and not r.missed(page)
    assert not page.context["intake_drawn"][1]["field-bay"].value()
    assert (post, followup["held"]) == (True, [True])


def test_mixed_receipts_preserve_positions_and_first_insert_notice(review_form_cases):
    case = review_form_cases
    browser, _, composer = legacy._composer(case)
    ticket = r.drawn_marker(browser.get(r.PAGE))[0]
    r.pages.end_open(case.agent)
    note = item(composer, {"body": "Example morning collection"})
    original = item(ticket, {"subject": "Example afternoon collection"})
    other = item(ticket, {"subject": "Other collection"})
    result = batch(browser, original, note, other, None)
    assert result == {"kept": 3, "held": [True, True, False, False]}
    assert KeptDraft.objects.count() == 2
    assert browser.get(reverse("account_login")).text.count(kept.KEPT_MESSAGE) == 1


# Adopted as a strict xfail: the notice comes from the request that stores a copy; telling
# whether the sign-in page has already shown it would need what this release does not keep
# (documented in docs/architecture.md).
@pytest.mark.xfail(strict=True, reason="a copy stored after the notice was shown is announced")
def test_delayed_different_form_does_not_repeat_one_signouts_notice(review_form_cases):
    case = review_form_cases
    browser, _, composer = legacy._composer(case)
    new_ticket = r.drawn_marker(browser.get(r.PAGE))[0]
    r.pages.end_open(case.agent)
    login = reverse("account_login")
    assert batch(browser, item(new_ticket, {"subject": "Earlier collection"}))["held"] == [True]
    assert browser.get(login).text.count(kept.KEPT_MESSAGE) == 1
    assert batch(browser, item(composer, {"body": "Confirm receipt"}))["held"] == [True]
    assert KeptDraft.objects.count() == 2
    assert kept.KEPT_MESSAGE not in browser.get(login).text


# Adopted as a strict xfail: the notice comes from the request that stores a copy, and an
# identical repeat must not announce again after the sign-in page has shown it (the third
# confirmation's case); both cannot hold without knowing what the sign-in page has shown
# (documented in docs/architecture.md).
@pytest.mark.xfail(strict=True, reason="a repeat whose first answer was lost is not announced")
def test_identical_retry_after_lost_insert_response_still_brings_notice_to_signin(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    earlier = Client()
    earlier.cookies = deepcopy(browser.cookies)  # same browser; first response cookies never arrive
    delivery = item(token, r.draft_values(pickup))
    assert batch(earlier, delivery)["held"] == [True]
    assert batch(browser, delivery)["held"] == [True]
    assert KeptDraft.objects.count() == 1
    assert kept.KEPT_MESSAGE in browser.get(reverse("account_login")).text


@pytest.mark.parametrize("per_request", [False, True], ids=["default", "atomic_requests"])
def test_get_with_kept_copy_uses_one_committed_category_and_definition(
    agent, pickup, monkeypatch, per_request
):
    category = RequestCategory.objects.filter(pk=pickup.category_id)
    template = RequestTemplate.objects.filter(pk=pickup.pk)
    old, new = "Receive at desk A.", "Receive at desk B."
    earlier, later = "Label for bay A.", "Label for bay B."
    category.update(guidance=old)
    template.update(instructions=earlier)
    browser = r.pending(agent, pickup)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", per_request)
    original, changed = intake.chosen_template, []

    def edit():
        with transaction.atomic():
            category.update(guidance=new, revision=F("revision") + 1)
            template.update(instructions=later, revision=F("revision") + 1)

    def choose(*args, **kwargs):
        chosen = original(*args, **kwargs)
        if chosen is not None and not changed:
            prior.worker(edit)
            changed.append(True)
        return chosen

    monkeypatch.setattr(intake, "chosen_template", choose)
    page = browser.get(r.PAGE)
    assert page.status_code == 200 and changed and KeptDraft.objects.get().taken_at is not None
    assert r.SUBJECT in page.text and (old in page.text) != (new in page.text)
    assert (earlier in page.text) != (later in page.text)
    assert (new in page.text) == (later in page.text)


def test_revision_bracket_starts_with_the_category_object_actually_used(agent, pickup, monkeypatch):
    browser = r.pages.browser_for(agent)
    spare = c.alternate(pickup, active=False)
    category = RequestCategory.objects.filter(pk=pickup.category_id)
    old, new = "Bring cartons to the front desk.", "Bring cartons to the rear desk."
    category.update(guidance=old)
    values = prior.submission(browser, pickup)
    init, changed = intake.Intake.__init__, []

    def edit():
        with transaction.atomic():
            category.update(guidance=new, revision=F("revision") + 1)
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    def read(execute, sql, params, many, context):
        # Let Intake load its category object, then commit before its first independent
        # revision or option-list query. Neither worker runs inside the other's transaction.
        if not changed and (
            sql.startswith('SELECT "taxonomy_requestcategory"."revision"')
            or 'AS "offered_to_some"' in sql
        ):
            prior.worker(edit)
            changed.append(True)
        return execute(sql, params, many, context)

    def init_with_edit(self, *args, **kwargs):
        with connection.execute_wrapper(read):
            init(self, *args, **kwargs)

    monkeypatch.setattr(intake.Intake, "__init__", init_with_edit)
    page = browser.post(r.PAGE, values)
    assert changed and page.status_code == 200 and (old in page.text) != (new in page.text)
    assert (str(spare.pk) in c.options(page)) == (new in page.text)


def test_selected_unused_category_deleted_before_intake_leaves_a_validation_page(
    agent, pickup, monkeypatch
):
    category = RequestCategory.objects.create(
        key="spare-route", name="Spare route", parent_id=pickup.category.parent_id
    )
    browser = r.pages.browser_for(agent)
    browser.raise_request_exception = False
    values = r.draft_values(pickup, category=str(category.pk), request_form="")
    original = intake.bound_intake

    def gone(selected, data, user):
        prior.worker(lambda: RequestCategory.objects.filter(pk=category.pk).delete())
        return original(selected, data, user)

    monkeypatch.setattr(intake, "bound_intake", gone)
    response = browser.post(r.PAGE, values)
    assert response.status_code == 200 and response.context["form"].has_error("category")
    assert (
        r.SUBJECT in response.text and not ServiceRequest.objects.filter(subject=r.SUBJECT).exists()
    )


@pytest.mark.parametrize("edit", ["instructions", "retire", "withdraw_offer"])
def test_stale_refresh_handles_a_single_commit_after_the_definition_read(
    agent, pickup, clinic, other_org, monkeypatch, edit
):
    spare = c.alternate(pickup, active=False)
    if edit == "withdraw_offer":
        pickup.clients.set([clinic])
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup, client=str(clinic.pk))
    template = RequestTemplate.objects.filter(pk=pickup.pk)
    old, new = "Refresh at reception.", "Refresh at dispatch."
    template.update(
        instructions=old, revision=F("revision") + 1
    )  # the submitted definition is stale
    refresh, read = intake.Intake.refresh, intake.definition.read
    changed = []

    def change():
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)
            if edit == "retire":
                template.update(is_active=False)
            elif edit == "withdraw_offer":
                pickup.clients.set([other_org])
            else:
                template.update(instructions=new, revision=F("revision") + 1)

    def reread(*args, **kwargs):
        fresh = read(*args, **kwargs)
        if not changed:
            prior.worker(change)
            changed.append(True)
        return fresh

    def stale(self):
        with monkeypatch.context() as patch:
            patch.setattr(intake.definition, "read", reread)
            return refresh(self)

    monkeypatch.setattr(intake.Intake, "refresh", stale)
    page = browser.post(r.PAGE, values)
    assert changed and page.status_code == 200
    newer_list = str(spare.pk) in c.options(page)
    picker, fields = page.context["intake_drawn"]
    if edit == "instructions":
        assert (new in page.text) != (old in page.text)
        assert newer_list == (new in page.text)
    elif newer_list:
        assert fields is None and r.selected(page, "request_form") == ""
        reason = intake.NO_LONGER if edit == "retire" else intake.NOT_OFFERED
        assert reason in picker.errors["request_form"]
    else:
        assert fields is not None and r.selected(page, "request_form") == str(pickup.pk)


def test_reversed_section_definition_is_checked_when_the_page_is_submitted(
    agent, pickup, monkeypatch
):
    spare = c.alternate(pickup)
    browser = r.pages.browser_for(agent)
    original = intake.definition.read
    sampled = []
    stable, transient = "Current carton instructions.", "Intervening carton instructions."

    def change(text, available):
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=pickup.pk).update(
                instructions=text, revision=F("revision") + 1
            )
            RequestTemplate.objects.filter(pk=spare.pk).update(
                is_active=available, revision=F("revision") + 1
            )

    change(stable, True)

    def read(*args, **kwargs):
        if sampled:
            return original(*args, **kwargs)
        prior.worker(lambda: change(transient, False))
        defn = original(*args, **kwargs)
        sampled.append(True)
        prior.worker(lambda: change(stable, True))
        return defn

    with monkeypatch.context() as patch:
        patch.setattr(intake.definition, "read", read)
        page = r.section(browser, pickup)
    assert sampled and page.status_code == 200
    values = r.draft_values(
        pickup,
        category_terms=r.hidden(page, "category_terms"),
        form_definition=r.hidden(page, "form_definition"),
    )
    result = browser.post(r.PAGE, values)
    if transient in page.text:
        assert result.status_code == 200 and not ServiceRequest.objects.exists()
        assert stable in result.text and intake.FORM_CHANGED in result.text
        values.update(
            form_definition=r.hidden(result, "form_definition"),
            category_terms=r.hidden(result, "category_terms"),
        )
        result = browser.post(r.PAGE, values)
    assert result.status_code == 302 and ServiceRequest.objects.get().subject == r.SUBJECT


def test_failed_atomic_request_get_leaves_the_taken_draft_for_the_next_get(
    agent, pickup, monkeypatch
):
    from desk import views

    browser = r.pending(agent, pickup)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    render = views.render
    attempts = []

    def interrupted(request, name, context, *args, **kwargs):
        if name == "desk/request_form.html" and not attempts:
            attempts.append(KeptDraft.objects.get().taken_at is not None)
            raise RuntimeError("Synthetic interrupted ticket draw")
        return render(request, name, context, *args, **kwargs)

    monkeypatch.setattr(views, "render", interrupted)
    with pytest.raises(RuntimeError, match="Synthetic interrupted ticket draw"):
        browser.get(r.PAGE)
    assert attempts == [True] and KeptDraft.objects.get().taken_at is None
    page = browser.get(r.PAGE)
    assert page.status_code == 200 and r.SUBJECT in page.text and r.ROUTE in page.text
    assert KeptDraft.objects.get().taken_at is not None


def test_refused_form_preserves_the_options_of_the_preselected_organization(
    agent, pickup, clinic, other_org
):
    spare = c.alternate(pickup)
    spare.clients.set([other_org])
    browser = r.pages.browser_for(agent)
    shown = r.section(browser, pickup, client=clinic.pk)
    available = c.options(shown)
    assert set(available) == {str(pickup.pk)}
    values = r.draft_values(
        pickup,
        client=str(clinic.pk),
        subject="",
        category_terms=r.hidden(shown, "category_terms"),
        form_definition=r.hidden(shown, "form_definition"),
    )
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200 and page.context["form"].has_error("subject")
    assert r.selected(page, "request_form") == str(pickup.pk) and r.ROUTE in page.text
    assert c.options(page) == available


def test_reordering_checkbox_options_preserves_ownership_of_the_same_selections(agent, pickup):
    handling = pickup.fields.filter(key="handling")
    handling.update(kind=FieldKind.MULTIPLE_CHOICE, choices=["Chilled", "Dry"])
    browser = r.pages.browser_for(agent)
    first = r.drawn_marker(r.section(browser, pickup))[0]
    handling.update(choices=["Dry", "Chilled"])
    second = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    earlier = r.draft_values(pickup, **{"field-handling": ["Chilled", "Dry"]})
    later = r.draft_values(pickup, **{"field-handling": ["Dry", "Chilled"]})
    assert batch(browser, item(first, earlier))["held"] == [True]
    own = refused(browser, second, later)
    followup = batch(browser, item(second, later))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    returned = page.context["intake_drawn"][1]["field-handling"].value()
    assert set(returned) == {"Dry", "Chilled"} and not r.missed(page)
    assert (own, followup["held"]) == (True, [True])
