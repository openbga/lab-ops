"""Form and catalog changes while a ticket page is open."""

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import pytest
from django.db import OperationalError, connection, connections, transaction
from django.urls import reverse
from django.utils.html import escape

from desk import services as desk_services
from desk.models import ServiceRequest
from request_forms import definition, intake, portal_views, selectors, services
from request_forms.models import Audience, FilledForm, RequestTemplate
from taxonomy import terms
from taxonomy.models import RequestCategory
from taxonomy.services import update_category

from .conftest import PASSWORD, definition_of
from .test_definition_changes import edit_field
from .test_values import current, portal_raise

pytestmark = pytest.mark.django_db
MARK = "SYNTHETIC-TYPED-ANSWER"


class Inputs(HTMLParser):
    """The named inputs of a page and their values, as a browser would send them."""

    def __init__(self, response):
        super().__init__()
        self.values = {}
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "name" in attrs:
            self.values[attrs["name"]] = attrs.get("value", "")


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)


def other_connection(function):
    """Run `function` on a second database connection, committed, as another person's request
    would; a lock it cannot take within half a second fails the test instead of hanging it."""

    def run():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '500ms'")
                return function()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result(timeout=20)


def guidance(http, category, clinic=None, template=None):
    params = {"category": category.pk, "request_form": template.pk if template else ""}
    if clinic is not None:
        params["client"] = clinic.pk
    return http.get(reverse("desk:category-guidance"), params, HTTP_HX_REQUEST="true")


def log_request(http, category, clinic, page, template=None, **values):
    """Send staff intake as the browser would from `page`: its own token and category terms."""
    carried = Inputs(page).values
    data = {
        "subject": "Sample pickup",
        "body": "Synthetic call",
        "first_entry_kind": "note",
        "category": category.pk,
        "channel": "phone",
        "client": clinic.pk if clinic is not None else "",
        "request_form": template.pk if template else "",
        terms.FIELD: carried[terms.FIELD],
        **values,
    }
    if "form_definition" in carried:
        data.setdefault("form_definition", carried["form_definition"])
    return http.post(reverse("desk:request-create"), data)


def set_instructions(admin, template, wording):
    revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=template.pk)
    services.update_template(
        actor=admin,
        template_id=template.pk,
        expected_revision=revision,
        reason="Clarify reference handling",
        instructions=wording,
    )


def retire(admin, template):
    revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=template.pk)
    services.update_template(
        actor=admin,
        template_id=template.pk,
        expected_revision=revision,
        reason="Retire the synthetic form",
        is_active=False,
    )


def second_form(category):
    """Another active form of the same category, so the picker stays on the page."""
    other = RequestTemplate.objects.create(
        key="second-pickup", name="Second pickup form", category=category
    )
    other.fields.create(key="note", label="Second form note", kind="short_text")
    return other


OLD = "Courier reference is for laboratory staff only."
NEW = "Courier reference is shown to the client."


def publish_client_visible(admin, pickup):
    set_instructions(admin, pickup, NEW)
    edit_field(admin, pickup, "courier", audience=Audience.SHOWN)


# Staff intake: the instructions are part of the one reading the page signs


@pytest.mark.django_db(transaction=True)
def test_intake_instructions_come_from_the_definition_the_page_signs(
    client, agent, admin, clinic, portal_user, pickup, monkeypatch
):
    """The setup changes between the picker's reading and the
    definition's. The page says what the token it carries says, and the answer is stored as
    that page described it."""
    set_instructions(admin, pickup, OLD)
    login(client, agent)
    original = definition.read

    with monkeypatch.context() as patch:

        def read_after_setup(*args, **kwargs):
            patch.setattr(definition, "read", original)
            other_connection(lambda: publish_client_visible(admin, pickup))
            return original(*args, **kwargs)

        patch.setattr(definition, "read", read_after_setup)
        page = guidance(client, pickup.category, clinic, pickup)
    body = page.content.decode()
    assert page.status_code == 200
    assert escape(NEW) in body and escape(OLD) not in body
    assert Inputs(page).values["form_definition"] == definition_of(pickup)
    saved = log_request(
        client, pickup.category, clinic, page, pickup, **{"field-count": "1", "field-courier": MARK}
    )
    assert saved.status_code == 302
    shown = selectors.client_values(ServiceRequest.objects.get(), portal_user)
    assert MARK in [line for row in shown.rows for line in row.lines]  # as the page said


@pytest.mark.django_db(transaction=True)
def test_refused_intake_redraws_instructions_from_the_definition_it_signs(
    client, agent, admin, clinic, pickup, monkeypatch
):
    """The same rule on a refused page: the form changes after the submission was read and
    before the write; the page drawn again tells the current instructions with the current
    token, never the instructions read when the submission arrived."""
    set_instructions(admin, pickup, OLD)
    login(client, agent)
    page = guidance(client, pickup.category, clinic, pickup)
    original = intake.lock_for_intake

    def lock_after_setup(template):
        other_connection(lambda: publish_client_visible(admin, pickup))
        return original(template)

    monkeypatch.setattr(intake, "lock_for_intake", lock_after_setup)
    refused = log_request(
        client, pickup.category, clinic, page, pickup, **{"field-count": "1", "field-courier": MARK}
    )
    body = refused.content.decode()
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert escape(services.FORM_CHANGED) in body and MARK not in body
    assert escape(NEW) in body and escape(OLD) not in body
    assert Inputs(refused).values["form_definition"] == definition_of(pickup)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["retired", "moved", "withdrawn"])
def test_intake_draws_no_form_that_left_the_category_after_the_picker_listed_it(
    change, client, agent, admin, clinic, other_org, pickup, staff_category, monkeypatch
):
    """Retired, moved to another category or withdrawn from the chosen organization between the
    picker's list and the definition's reading: nothing of the form is drawn or signed."""
    second_form(pickup.category)
    set_instructions(admin, pickup, OLD)
    login(client, agent)
    original = definition.read

    def change_form():
        if change == "retired":
            retire(admin, pickup)
            return
        revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=pickup.pk)
        services.update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=revision,
            reason="Synthetic setup change",
            **(
                {"category": staff_category, "offered_in_portal": False, "featured": False}
                if change == "moved"
                else {"clients": [other_org], "instructions": NEW}
            ),
        )

    with monkeypatch.context() as patch:

        def read_after_change(*args, **kwargs):
            patch.setattr(definition, "read", original)
            other_connection(change_form)
            return original(*args, **kwargs)

        patch.setattr(definition, "read", read_after_change)
        page = guidance(client, pickup.category, clinic, pickup)
    body = page.content.decode()
    assert page.status_code == 200 and 'name="request_form"' in body
    assert 'name="field-count"' not in body and 'name="form_definition"' not in body
    assert escape(OLD) not in body and escape(NEW) not in body


# A refusal shows nothing that was typed, whatever refused it


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["portal", "staff", "staff-with-other-forms"])
def test_late_retirement_refusal_shows_nothing_typed(
    path, client, agent, admin, pickup, portal_user, clinic, monkeypatch
):
    """The form is retired
    after the page was validated and before the write."""
    if path == "staff-with-other-forms":
        second_form(pickup.category)
    staff = path != "portal"
    login(client, agent if staff else portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    page = guidance(client, pickup.category, clinic, pickup) if staff else client.get(url)
    module, name = (
        (intake, "lock_for_intake") if staff else (portal_views, "create_request_with_form")
    )
    original = getattr(module, name)

    def retire_then_write(*args, **kwargs):
        other_connection(lambda: retire(admin, pickup))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, retire_then_write)
    typed = {"field-count": "7", ("field-courier" if staff else "field-patient"): MARK}
    refused = (
        log_request(client, pickup.category, clinic, page, pickup, **typed)
        if staff
        else client.post(url, {"form_definition": Inputs(page).values["form_definition"], **typed})
    )
    assert not ServiceRequest.objects.exists()
    if staff:
        body = refused.content.decode()
        assert refused.status_code == 200 and "no-store" in refused["Cache-Control"]
        assert MARK not in body and 'value="7"' not in body
        assert 'name="field-count"' not in body and 'name="form_definition"' not in body
        assert 'value="Sample pickup"' in body  # the request's own fields are kept
        if path == "staff-with-other-forms":
            assert escape(intake.NO_LONGER) in body  # said once, on the picker
            assert escape(services.FORM_CHANGED) not in body
    else:
        assert refused.status_code == 302 and refused.url == reverse("portal_forms:choose")
        assert MARK.encode() not in client.get(refused.url).content


@pytest.mark.django_db(transaction=True)
def test_portal_refusal_checks_the_offer_again_before_showing_the_form(
    client, admin, pickup, portal_user, other_org, monkeypatch
):
    """The form is restricted to another organization, with
    new instructions, between the page's checks and the write. The refusal shows nothing of the
    replacement."""
    url = reverse("portal_forms:raise", args=[pickup.key])
    login(client, portal_user)
    page = client.get(url)
    private = "Updated instructions for the other organization."
    original = portal_views.create_request_with_form

    def offer_elsewhere():
        services.update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=RequestTemplate.objects.get(pk=pickup.pk).revision,
            reason="Change synthetic organization offer",
            clients=[other_org],
            instructions=private,
        )

    def write(**kwargs):
        other_connection(offer_elsewhere)
        return original(**kwargs)

    monkeypatch.setattr(portal_views, "create_request_with_form", write)
    refused = client.post(
        url, {"form_definition": Inputs(page).values["form_definition"], "field-count": "1"}
    )
    assert not ServiceRequest.objects.exists()
    assert refused.status_code == 302 and refused.url == reverse("portal_forms:choose")
    assert private.encode() not in client.get(refused.url).content
    assert client.get(url).status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("method", ["get", "post"])
def test_portal_page_checks_the_offer_in_the_reading_it_draws(
    method, client, admin, pickup, portal_user, other_org, monkeypatch
):
    """Withdrawn from this organization between the list of offered forms and the definition's
    reading: the page answers as for any form not offered, and shows nothing of it."""
    url = reverse("portal_forms:raise", args=[pickup.key])
    login(client, portal_user)
    token = Inputs(client.get(url)).values["form_definition"]
    private = "Instructions for another organization only."
    original = definition.read

    def withdraw():
        services.update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=RequestTemplate.objects.get(pk=pickup.pk).revision,
            reason="Offer the synthetic form elsewhere",
            clients=[other_org],
            instructions=private,
        )

    with monkeypatch.context() as patch:

        def read_after_withdrawal(*args, **kwargs):
            patch.setattr(definition, "read", original)
            other_connection(withdraw)
            return original(*args, **kwargs)

        patch.setattr(definition, "read", read_after_withdrawal)
        response = (
            client.get(url)
            if method == "get"
            else client.post(
                url, {"form_definition": token, "field-count": "1", "field-patient": MARK}
            )
        )
    assert private.encode() not in response.content and MARK.encode() not in response.content
    if method == "get":
        assert response.status_code == 404
    else:
        assert response.status_code == 302 and response.url == reverse("portal_forms:choose")
        assert not ServiceRequest.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["portal", "again", "staff", "correction"])
@pytest.mark.parametrize("timing", ["during_draw", "while_open"])
def test_concurrent_setup_refuses_the_drawn_form_and_clears_answers(
    timing, path, client, capable, admin, pickup, clinic, portal_user, monkeypatch
):
    """A setup change while the page is drawn or open
    refuses the save, and the page drawn again carries none of the answers and the current
    token."""
    record = (
        portal_raise(portal_user, pickup, {"count": 1}) if path in {"again", "correction"} else None
    )
    count = ServiceRequest.objects.count()
    login(client, capable if path in {"staff", "correction"} else portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    if path == "correction":
        url = reverse("request_details:edit", args=[record.number])
    original = definition.read
    reads = []

    def relabel():
        edit_field(admin, pickup, "count", label="Updated cooler count")

    with monkeypatch.context() as patch:

        def draw_then_edit(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            reads.append(snapshot.token)
            if timing == "during_draw" and len(reads) == 1:
                other_connection(relabel)
            return snapshot

        patch.setattr(definition, "read", draw_then_edit)
        page = (
            guidance(client, pickup.category, clinic, pickup)
            if path == "staff"
            else client.get(url, {"again": record.number} if path == "again" else {})
        )
    assert page.status_code == 200 and b"Number of coolers" in page.content
    fields = Inputs(page).values
    assert reads == [fields["form_definition"]]
    if timing == "while_open":
        other_connection(relabel)
    values = {"field-count": "2", "field-patient": MARK}
    if path == "correction":
        values.update(seen_count=fields["seen_count"], reason="Synthetic recount")
    response = (
        log_request(client, pickup.category, clinic, page, pickup, **values)
        if path == "staff"
        else client.post(url, {"form_definition": fields["form_definition"], **values})
    )
    assert response.status_code == 200 and MARK.encode() not in response.content
    assert b"Updated cooler count" in response.content and "no-store" in response["Cache-Control"]
    assert Inputs(response).values["form_definition"] == definition.token(pickup)
    assert ServiceRequest.objects.count() == count
    if record:
        assert current(record)["count"].value == "1" and "patient" not in current(record)


# A correction signs the items it still shows


@pytest.fixture
def moved_items(admin, pickup, catalog, portal_user):
    """A request holding two transport bags, after the item list moved to another catalog."""
    item = catalog.items.get(code="TEST-BAG")
    record = portal_raise(portal_user, pickup, {"count": 1, "items": {item.pk: 2}})
    replacement = services.create_catalog(actor=admin, key="replacement", name="Replacement")
    edit_field(admin, pickup, "items", catalog=replacement)
    return record, item


@pytest.mark.parametrize("change", ["service", "direct"])
def test_rename_of_an_item_a_correction_still_shows_refuses_the_page(
    change, client, agent, admin, catalog, moved_items
):
    """The page drew the kept item from outside the signed
    state, so a rename left the page valid and the correction was stored under the new name."""
    record, item = moved_items
    login(client, agent)
    url = reverse("request_details:edit", args=[record.number])
    opened = client.get(url)
    old = Inputs(opened).values
    assert item.name.encode() in opened.content and f"field-items_{item.pk}" in old
    renamed = "Updated supply label"
    if change == "direct":
        catalog.items.filter(pk=item.pk).update(name=renamed)
    else:
        item.refresh_from_db()
        services.update_item(
            actor=admin,
            item_id=item.pk,
            expected_revision=item.revision,
            reason="Synthetic rename",
            name=renamed,
        )
    response = client.post(
        url,
        {
            "form_definition": old["form_definition"],
            "kept_rows": old["kept_rows"],
            "seen_items": old["seen_items"],
            f"field-items_{item.pk}": "1",
            "reason": "Correct quantity from two to one",
        },
    )
    assert response.status_code == 200
    assert escape(services.FORM_CHANGED) in response.content.decode()
    assert current(record)["items"].value[0]["quantity"] == 2


def test_retiring_the_catalog_of_a_kept_item_refuses_the_page(
    client, agent, admin, catalog, moved_items
):
    """A kept item is marked retired from its catalog's state, so that state is signed too."""
    record, item = moved_items
    login(client, agent)
    url = reverse("request_details:edit", args=[record.number])
    old = Inputs(client.get(url)).values
    services.update_catalog(
        actor=admin,
        catalog_id=catalog.pk,
        expected_revision=catalog.revision,
        reason="Synthetic retirement",
        is_active=False,
    )
    response = client.post(
        url,
        {
            "form_definition": old["form_definition"],
            "kept_rows": old["kept_rows"],
            "seen_items": old["seen_items"],
            f"field-items_{item.pk}": "1",
            "reason": "Correct quantity from two to one",
        },
    )
    assert response.status_code == 200 and current(record)["items"].value[0]["quantity"] == 2


def test_a_correction_still_keeps_and_changes_items_from_the_old_catalog(
    client, agent, moved_items
):
    """The signed kept items still work: a quantity of a kept item is corrected, and a change to
    another field keeps the item list as it is."""
    record, item = moved_items
    login(client, agent)
    url = reverse("request_details:edit", args=[record.number])
    page = Inputs(client.get(url)).values
    saved = client.post(
        url,
        {
            "form_definition": page["form_definition"],
            "kept_rows": page["kept_rows"],
            "seen_items": page["seen_items"],
            f"field-items_{item.pk}": "1",
            "reason": "Correct quantity from two to one",
        },
    )
    assert saved.status_code == 302
    assert current(record)["items"].value[0]["quantity"] == 1
    page = Inputs(client.get(url)).values
    saved = client.post(
        url,
        {
            "form_definition": page["form_definition"],
            "kept_rows": page["kept_rows"],
            "seen_count": page["seen_count"],
            "field-count": "3",
            "reason": "Recount",
        },
    )
    assert saved.status_code == 302
    assert current(record)["count"].value == "3"
    assert current(record)["items"].value[0]["quantity"] == 1


def test_a_definition_offers_only_kept_items_it_read(pickup, moved_items):
    """A kept item is offered only from a reading that signed it."""
    _record, item = moved_items
    items_field = next(f for f in definition.read(pickup).fields if f.key == "items")
    assert definition.read(pickup).orderable_items(items_field, [item.pk]) == []
    signed = definition.read(pickup, retained=[item.pk])
    assert [i.pk for i in signed.orderable_items(items_field, [item.pk])] == [item.pk]
    assert signed.token != definition.read(pickup).token


def test_a_correction_page_carries_the_rows_it_keeps_items_from(client, agent, moved_items):
    """The page names the value rows its kept items come from, and a submission without them
    is read as a page that kept none: refused as changed, since this page kept two bags."""
    record, item = moved_items
    login(client, agent)
    url = reverse("request_details:edit", args=[record.number])
    page = Inputs(client.get(url)).values
    assert page["kept_rows"] == str(current(record)["items"].pk)
    response = client.post(
        url,
        {
            "form_definition": page["form_definition"],
            "seen_count": page["seen_count"],
            "field-count": "5",
            "reason": "Recount",
        },
    )
    assert (
        response.status_code == 200 and escape(services.FORM_CHANGED) in response.content.decode()
    )
    assert current(record)["count"].value == "1"


# One reading per page and per save


def test_correction_missing_answer_notice_matches_the_drawn_fields(
    client, capable, admin, pickup, portal_user, monkeypatch
):
    """A field made required after the definition was read is not
    reported missing beside controls that show it optional."""
    record = portal_raise(portal_user, pickup, {"count": 1})
    login(client, capable)
    original = definition.read
    with monkeypatch.context() as patch:

        def draw_then_require(*args, **kwargs):
            patch.setattr(definition, "read", original)
            drawn = original(*args, **kwargs)
            edit_field(admin, pickup, "tracking", required=True)
            return drawn

        patch.setattr(definition, "read", draw_then_require)
        page = client.get(reverse("request_details:edit", args=[record.number]))
    form = page.context["form"]
    assert not next(f for f in form.definition.fields if f.key == "tracking").required
    assert page.context["details"].missing == [] and b"1 required missing" not in page.content
    # The same page drawn now reports it, from its own reading.
    page = client.get(reverse("request_details:edit", args=[record.number]))
    assert page.context["details"].missing == ["Tracking"] and b"1 required missing" in page.content


@pytest.mark.parametrize("path", ["staff", "portal", "again", "correction"])
def test_one_reading_for_writing_per_save(
    path, client, agent, clinic, portal_user, pickup, monkeypatch
):
    """Each capture reads its form once before writing."""
    record = (
        portal_raise(portal_user, pickup, {"count": 1}) if path in {"again", "correction"} else None
    )
    login(client, agent if path in {"staff", "correction"} else portal_user)
    url = (
        reverse("request_details:edit", args=[record.number])
        if path == "correction"
        else reverse("portal_forms:raise", args=[pickup.key])
    )
    page = (
        guidance(client, pickup.category, clinic, pickup)
        if path == "staff"
        else client.get(url, {"again": record.number} if path == "again" else {})
    )
    carried = Inputs(page).values
    original = definition.read
    writes = []

    def trace(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("for_write"):
            writes.append(result.token)
        return result

    monkeypatch.setattr(definition, "read", trace)
    data = {"field-count": "2"}
    if path == "correction":
        data.update(seen_count=carried["seen_count"], reason="Correct count")
    response = (
        log_request(client, pickup.category, clinic, page, pickup, **data)
        if path == "staff"
        else client.post(url, {"form_definition": carried["form_definition"], **data})
    )
    assert response.status_code == 302
    assert writes == [carried["form_definition"]]
    assert FilledForm.objects.count() == (2 if path == "again" else 1)


def test_values_are_stored_only_from_a_definition_read_for_writing(agent, clinic, pickup):
    """attach_values takes the reading staff intake locked, never one read for a page."""
    from desk.services import create_request

    page_reading = definition.read(pickup)
    with pytest.raises(RuntimeError, match="read for writing"), transaction.atomic():
        create_request(
            actor=agent,
            category=pickup.category,
            subject="Synthetic",
            first_entry_body="Synthetic",
            first_entry_kind="note",
            channel="phone",
            client=clinic,
            on_created=lambda request: services.attach_values(
                actor=agent,
                request=request,
                template=pickup,
                expected_definition=page_reading.token,
                values={"count": 1},
                locked=page_reading,
            ),
        )
    assert not ServiceRequest.objects.exists()


def test_correction_page_reads_its_definition_once(client, agent, portal_user, pickup, monkeypatch):
    """One reading for the controls, the token and the missing-answer notice."""
    record = portal_raise(portal_user, pickup, {"count": 1})
    login(client, agent)
    reads = []
    original = definition.read

    def count(*args, **kwargs):
        reads.append(kwargs.get("for_write", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(definition, "read", count)
    page = client.get(reverse("request_details:edit", args=[record.number]))
    assert page.status_code == 200 and reads == [False]


# A colleague's correction of the values is not a change of the form


@pytest.mark.parametrize("has_kept_rows", [True, False])
def test_correction_compares_kept_items_to_the_quantities_the_page_showed(
    client, agent, capable, moved_items, has_kept_rows
):
    """A conflicting item quantity refuses the entire save. Leaving that quantity untouched
    on the same old page still lets staff correct a different field."""
    record, item = moved_items
    login(client, agent)
    url = reverse("request_details:edit", args=[record.number])
    opened = Inputs(client.get(url)).values
    row_ids = {"kept_rows": opened["kept_rows"]} if has_kept_rows else {}
    colleague = type(client)()
    login(colleague, capable)
    seen = Inputs(colleague.get(url)).values
    cleared = colleague.post(
        url,
        {
            "form_definition": seen["form_definition"],
            "kept_rows": seen["kept_rows"],
            "seen_items": seen["seen_items"],
            f"field-items_{item.pk}": "",
            "reason": "Bags were not needed",
        },
    )
    assert cleared.status_code == 302 and current(record)["items"].value is None
    assert opened["kept_rows"] != Inputs(client.get(url)).values["kept_rows"]  # a later row
    stale = client.post(
        url,
        {
            "form_definition": opened["form_definition"],
            **row_ids,
            "seen_items": opened["seen_items"],
            f"field-items_{item.pk}": "1",
            "seen_count": opened["seen_count"],
            "field-count": "6",
            "reason": "Correct quantity from two to one",
        },
    )
    assert stale.status_code == 200 and escape(services.STALE) in stale.content.decode()
    assert Inputs(stale).values[f"field-items_{item.pk}"] == "1"
    assert Inputs(stale).values["field-count"] == "6"
    assert current(record)["items"].value is None
    assert current(record)["count"].value == "1"
    saved = client.post(
        url,
        {
            "form_definition": opened["form_definition"],
            **row_ids,
            "seen_items": opened["seen_items"],
            f"field-items_{item.pk}": opened[f"field-items_{item.pk}"],
            "seen_count": opened["seen_count"],
            "field-count": "6",
            "reason": "Recount with the courier",
        },
    )
    assert saved.status_code == 302 and current(record)["count"].value == "6"
    assert current(record)["items"].value is None


def update_group(admin, group, **data):
    group.refresh_from_db()
    return update_category(
        actor=admin,
        category_id=group.pk,
        expected_revision=group.revision,
        reason="Synthetic group change",
        **data,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["portal", "again", "staff"])
def test_the_category_group_is_not_retired_during_a_capture_write(
    path, client, capable, admin, group, pickup, clinic, portal_user, monkeypatch
):
    """The group's active flag is signed, so a capture's write holds the group
    until it commits; a retirement tried in between waits (here: times out) and the request is
    saved as the page showed it, never refused with the typed answers on the page."""
    record = portal_raise(portal_user, pickup, {"count": 1}) if path == "again" else None
    before = ServiceRequest.objects.count()
    staff = path == "staff"
    login(client, capable if staff else portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    page = (
        guidance(client, pickup.category, clinic, pickup)
        if staff
        else client.get(url, {"again": record.number} if record else {})
    )
    original = desk_services.create_request
    blocked = []

    def create_while_the_group_is_retired(**kwargs):
        try:
            other_connection(lambda: update_group(admin, group, is_active=False))
        except OperationalError:
            blocked.append(True)
        return original(**kwargs)

    monkeypatch.setattr(desk_services, "create_request", create_while_the_group_is_retired)
    typed = {"field-count": "4"}
    response = (
        log_request(client, pickup.category, clinic, page, pickup, **typed)
        if staff
        else client.post(
            url + (f"?again={record.number}" if record else ""),
            {"form_definition": Inputs(page).values["form_definition"], **typed},
        )
    )
    assert blocked == [True] and RequestCategory.objects.get(pk=group.pk).is_active
    assert response.status_code == 302 and ServiceRequest.objects.count() == before + 1


@pytest.mark.django_db(transaction=True)
def test_a_group_retired_before_the_write_returns_the_portal_to_the_chooser(
    client, admin, group, pickup, portal_user
):
    """The same retirement committed before the save: the form is no longer offered, so the
    portal returns to the chooser and shows nothing typed."""
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    token = Inputs(client.get(url)).values["form_definition"]
    other_connection(lambda: update_group(admin, group, is_active=False))
    refused = client.post(
        url, {"form_definition": token, "field-count": "4", "field-patient": MARK}
    )
    assert refused.status_code == 302 and not ServiceRequest.objects.exists()
    assert MARK.encode() not in client.get(refused.url).content


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("moment", ["drawn", "refused"])
def test_the_portal_subject_hint_names_the_label_the_page_signs(
    moment, client, admin, pickup, portal_user, monkeypatch
):
    """The hint under Subject (the subject an empty one becomes) is the form's
    label as the signed reading has it, on the first drawing and on a refused page."""
    new = "Book a courier collection"
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    token = Inputs(client.get(url)).values["form_definition"]
    if moment == "refused":
        edit_field(admin, pickup, "count", label="Updated cooler count")
    original = definition.read

    def rename():
        revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=pickup.pk)
        services.update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=revision,
            reason="Rename the synthetic form",
            client_label=new,
        )

    with monkeypatch.context() as patch:

        def rename_then_read(*args, **kwargs):
            patch.setattr(definition, "read", original)
            other_connection(rename)
            return original(*args, **kwargs)

        patch.setattr(definition, "read", rename_then_read)
        response = (
            client.get(url)
            if moment == "drawn"
            else client.post(url, {"form_definition": token, "field-count": "1"})
        )
    body = response.content.decode()
    assert response.status_code == 200
    assert Inputs(response).values["form_definition"] == definition_of(pickup)
    assert f"Empty: “{escape(new)}”" in body and "Empty: “Request a pickup”" not in body


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("timing", ["while_open", "during_save"])
def test_a_staff_refusal_does_not_redraw_a_form_withdrawn_from_the_organization(
    timing, client, agent, admin, pickup, clinic, other_org, monkeypatch
):
    """Restricted to another organization while the page is open or during the
    save, the form is not drawn again for the organization chosen; the picker is set to No form
    and says why, once."""
    second_form(pickup.category)
    login(client, agent)
    page = guidance(client, pickup.category, clinic, pickup)

    def withdraw():
        revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=pickup.pk)
        services.update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=revision,
            reason="Offer the synthetic form elsewhere",
            clients=[other_org],
        )

    if timing == "while_open":
        other_connection(withdraw)
    else:
        original = intake.lock_for_intake

        def withdraw_then_lock(template):
            other_connection(withdraw)
            return original(template)

        monkeypatch.setattr(intake, "lock_for_intake", withdraw_then_lock)
    refused = log_request(
        client, pickup.category, clinic, page, pickup, **{"field-count": "3", "field-courier": MARK}
    )
    body = refused.content.decode()
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert MARK not in body and 'value="Sample pickup"' in body
    assert 'name="field-count"' not in body and 'name="form_definition"' not in body
    assert body.count(escape(services.NOT_OFFERED)) == 1
    assert escape(services.FORM_CHANGED) not in body


def test_the_category_section_is_never_cached(client, agent, clinic, pickup):
    """It carries a request form's controls and token, like the pages that capture values."""
    login(client, agent)
    response = guidance(client, pickup.category, clinic, pickup)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]


def test_the_demo_story_corrects_through_the_correction_reading(moved_items, agent):
    """A correction token from outside a page (the demo story) is the correction's own reading,
    so a request keeping items from a former catalog can still be corrected."""
    record, item = moved_items
    filled = FilledForm.objects.get(request=record)
    changed = services.correct_values(
        actor=agent,
        request=record,
        expected_definition=definition.read_for_correction(filled).token,
        values={"count": 2},
        reason="Recount",
        expected={"count": current(record)["count"].pk},
    )
    assert changed == ["count"]
    assert definition.read_for_correction(filled).token != definition.token(filled.template)


@pytest.mark.parametrize("change", ["rename-item", "retire-former-catalog"])
def test_a_setup_change_to_an_item_no_longer_held_leaves_a_correction_valid(
    change, client, agent, capable, admin, catalog, moved_items
):
    """Only the items on the value rows the page drew are signed, so a setup
    change to an item an earlier value held, and the page no longer keeps, refuses nothing."""
    record, item = moved_items
    login(client, capable)
    url = reverse("request_details:edit", args=[record.number])
    seen = Inputs(client.get(url)).values
    cleared = client.post(
        url,
        {
            "form_definition": seen["form_definition"],
            "kept_rows": seen["kept_rows"],
            "seen_items": seen["seen_items"],
            f"field-items_{item.pk}": "",
            "reason": "Bags were not needed",
        },
    )
    assert cleared.status_code == 302 and current(record)["items"].value is None
    login(client, agent)
    page = Inputs(client.get(url)).values
    assert page["kept_rows"] == str(current(record)["items"].pk)
    if change == "rename-item":
        item.refresh_from_db()
        services.update_item(
            actor=admin,
            item_id=item.pk,
            expected_revision=item.revision,
            reason="Synthetic rename",
            name="Updated supply label",
        )
    else:
        catalog.refresh_from_db()
        services.update_catalog(
            actor=admin,
            catalog_id=catalog.pk,
            expected_revision=catalog.revision,
            reason="Synthetic retirement",
            is_active=False,
        )
    saved = client.post(
        url,
        {
            "form_definition": page["form_definition"],
            "kept_rows": page["kept_rows"],
            "seen_count": page["seen_count"],
            "field-count": "6",
            "reason": "Recount with the courier",
        },
    )
    assert saved.status_code == 302 and current(record)["count"].value == "6"


@pytest.mark.django_db(transaction=True)
def test_a_staff_refusal_with_another_error_does_not_redraw_a_withdrawn_form(
    client, agent, admin, pickup, clinic, other_org
):
    """The organization is read from the desk's form even when another of its
    fields is refused, so the form withdrawn from it is not drawn again."""
    second_form(pickup.category)
    login(client, agent)
    page = guidance(client, pickup.category, clinic, pickup)
    revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=pickup.pk)
    other_connection(
        lambda: services.update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=revision,
            reason="Offer the synthetic form elsewhere",
            clients=[other_org],
        )
    )
    refused = log_request(
        client,
        pickup.category,
        clinic,
        page,
        pickup,
        subject="",
        **{"field-count": "3", "field-courier": MARK},
    )
    body = refused.content.decode()
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert MARK not in body and 'name="field-count"' not in body
    assert body.count(escape(services.NOT_OFFERED)) == 1
    assert escape(services.FORM_CHANGED) not in body


@pytest.mark.django_db(transaction=True)
def test_a_staff_refusal_without_organization_does_not_redraw_a_form_for_selected_ones(
    client, agent, admin, pickup, clinic
):
    """A request with no organization takes only forms offered to every
    organization, so a changed form offered to selected ones is not drawn again for it."""
    second_form(pickup.category)
    revision = RequestTemplate.objects.values_list("revision", flat=True).get(pk=pickup.pk)
    services.update_template(
        actor=admin,
        template_id=pickup.pk,
        expected_revision=revision,
        reason="Offer the synthetic form to one organization",
        clients=[clinic],
    )
    login(client, agent)
    page = guidance(client, pickup.category, None, pickup)
    assert 'name="field-count"' in page.content.decode()  # not chosen yet: any form
    edit_field(admin, pickup, "count", label="Updated cooler count")
    refused = log_request(
        client, pickup.category, None, page, pickup, **{"field-count": "3", "field-courier": MARK}
    )
    body = refused.content.decode()
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert MARK not in body and 'name="field-count"' not in body
    assert body.count(escape(services.NOT_OFFERED)) == 1
    assert escape(services.FORM_CHANGED) not in body


BIG = "9" * 5000  # past int()'s 4300-digit limit


@pytest.mark.parametrize(
    "where",
    [
        "kept-rows",
        "correction-seen",
        "section-client",
        "section-category",
        "create-category",
        "list-page-size",
        "find",
        "new-category-group",
    ],
)
def test_long_digit_inputs_are_refused_or_ignored_never_a_server_error(
    where, client, agent, admin, clinic, pickup, portal_user
):
    """A whole number longer than a record id is not one (core.ids), wherever
    an address or form takes one."""
    login(client, admin if where == "new-category-group" else agent)
    if where in {"kept-rows", "correction-seen"}:
        record = portal_raise(portal_user, pickup, {"count": 1})
        url = reverse("request_details:edit", args=[record.number])
        page = Inputs(client.get(url)).values
        data = {
            "form_definition": page["form_definition"],
            "kept_rows": BIG if where == "kept-rows" else page["kept_rows"],
            "seen_count": BIG if where == "correction-seen" else page["seen_count"],
            "field-count": "6",
            "reason": "Recount",
        }
        response = client.post(url, data)
        if where == "correction-seen":  # no value row has that id: stale, nothing saved
            assert current(record)["count"].value == "1"
    elif where == "section-client":
        response = client.get(
            reverse("desk:category-guidance"), {"category": pickup.category_id, "client": BIG}
        )
    elif where == "section-category":
        response = client.get(reverse("desk:category-guidance"), {"category": BIG})
    elif where == "create-category":
        response = client.post(reverse("desk:request-create"), {"category": BIG, "subject": "x"})
        assert not ServiceRequest.objects.exists()
    elif where == "list-page-size":
        response = client.get(reverse("desk:request-list"), {"per_page": BIG})
    elif where == "find":
        response = client.get(reverse("desk:request-find"), {"q": BIG})
    else:
        response = client.get(reverse("taxonomy:category-create"), {"group": BIG})
    assert response.status_code in (200, 302, 400)
