"""Form definitions remain consistent from rendering through capture."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import OperationalError, connection, connections, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from accounts.models import User
from desk.models import EntryKind, ServiceRequest
from request_forms import definition, intake, portal_views, request_views, selectors, services
from request_forms.models import (
    Catalog,
    CatalogItem,
    FieldValue,
    FilledForm,
    RequestTemplate,
    TemplateField,
)
from taxonomy import terms
from taxonomy.models import RequestCategory
from taxonomy.services import update_category

from .conftest import PASSWORD, definition_of
from .test_definition_changes import drawn_token, edit_field
from .test_definition_changes import intake as post_intake
from .test_values import current, portal_raise

pytestmark = pytest.mark.django_db
MARK = "SYNTHETIC-FIRST-FILL"


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)


def category_change(admin, category, **values):
    category.refresh_from_db()
    return update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=category.revision,
        reason="Synthetic category change",
        **values,
    )


def other_connection(function):
    """Run `function` on a second database connection (another thread), as another person's
    request would."""

    def run():
        try:
            return function()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result(timeout=20)


def repurpose_patient(admin, pickup):
    target = TemplateField.objects.get(template=pickup, key="patient")
    services.update_field(
        actor=admin,
        field_id=target.pk,
        expected_revision=target.revision,
        reason="Repurpose an unused field",
        holds_patient_information=False,
        label="Ordinary shipping reference",
    )


# One read: the controls and the token come from the same definition


@pytest.mark.django_db(transaction=True)
def test_controls_and_signature_describe_the_same_definition(
    client, capable, agent, admin, pickup, portal_user, monkeypatch
):
    """A setup save lands while the correction page is being built. The
    page's controls and its token come from one read, so the page that still shows "Patient
    identifier" carries that definition's token, and sending it is refused."""
    record = portal_raise(portal_user, pickup, {"count": 1})
    url = reverse("request_details:edit", args=[record.number])
    login(client, capable)
    original = definition.read
    with monkeypatch.context() as patch:

        def read_then_setup_saves(*args, **kwargs):
            patch.setattr(definition, "read", original)
            drawn = original(*args, **kwargs)
            other_connection(lambda: repurpose_patient(admin, pickup))
            return drawn

        patch.setattr(definition, "read", read_then_setup_saves)
        page = client.get(url)
    assert page.status_code == 200 and b"Patient identifier" in page.content
    assert b"Ordinary shipping reference" not in page.content
    assert not definition.unchanged(pickup, drawn_token(page))  # the old definition's token
    response = client.post(
        url,
        {
            "form_definition": drawn_token(page),
            "field-patient": MARK,
            "seen_patient": "",
            "field-count": "1",
            "seen_count": current(record)["count"].pk,
        },
    )
    assert response.status_code == 200 and MARK.encode() not in response.content
    assert services.FORM_CHANGED.encode() in response.content
    assert "patient" not in current(record)
    lines = [line for row in selectors.staff_values(record, agent).rows for line in row.lines]
    assert MARK not in lines


@pytest.mark.django_db(transaction=True)
def test_no_setup_change_commits_in_the_middle_of_a_read(admin, pickup, catalog, monkeypatch):
    """While the definition is being read (after the form's and catalogs' locks, before the
    category's), a field change, an item change and a category change on another connection
    cannot commit: the read is one consistent snapshot."""
    original = definition._lock
    attempts = []

    def try_setup_changes():
        def attempt(change):
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '300ms'")
                    change()
            except OperationalError:
                return "blocked"
            return "committed"

        bag = CatalogItem.objects.get(code="TEST-BAG")
        return [
            attempt(lambda: edit_field(admin, pickup, "count", label="Changed meanwhile")),
            attempt(
                lambda: services.update_item(
                    actor=admin,
                    item_id=bag.pk,
                    expected_revision=bag.revision,
                    reason="Meanwhile",
                    max_quantity=2,
                )
            ),
            attempt(lambda: category_change(admin, pickup.category, is_restricted=True)),
        ]

    def lock_then_try(model, ids, mode):
        original(model, ids, mode)
        if model is RequestCategory and not attempts:
            attempts.extend(other_connection(try_setup_changes))

    monkeypatch.setattr(definition, "_lock", lock_then_try)
    drawn = definition.read(pickup)
    assert attempts == ["blocked", "blocked", "blocked"]
    monkeypatch.setattr(definition, "_lock", original)
    assert definition.read(pickup).digest == drawn.digest


def test_values_written_come_from_the_definition_compared(portal_user, pickup):
    """The service compares, validates and copies from one read: the stored label is the one
    the token named."""
    request = portal_raise(portal_user, pickup, {"count": 2})
    assert current(request)["count"].label == "Number of coolers"


# The category's protection is in the definition


# The ticket and the form agree on category protection.


def drawn_terms(page):
    match = re.search(r'name="category_terms" value="([^"]+)"', page.content.decode())
    assert match, "the page carries no category terms"
    return match.group(1)


def messages_on(page):
    body = page.content.decode()
    return [
        message
        for message in (escape(terms.CHANGED), escape(services.FORM_CHANGED))
        if message in body
    ]


def send_again(client, page, category, clinic, pickup):
    """Submit the redrawn page as the browser would: its own token and category terms."""
    return post_intake(
        client,
        category,
        client=clinic.pk,
        request_form=pickup.pk,
        form_definition=drawn_token(page),
        category_terms=drawn_terms(page),
        **{"field-count": "2"},
    )


def test_redrawn_intake_after_a_category_change_says_one_thing_and_is_accepted(
    client, capable, admin, clinic, pickup
):
    """The category changed while the page was open: the form's check notices first; one
    message (the desk's, beside the category), and the redrawn page's token and terms are
    current, so sending it again works."""
    category_change(admin, pickup.category, is_restricted=True)
    login(client, capable)
    page = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "client": clinic.pk, "request_form": pickup.pk},
    )
    category_change(admin, pickup.category, is_restricted=False)
    refused = send_again(client, page, pickup.category, clinic, pickup)
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert messages_on(refused) == [escape(terms.CHANGED)]
    assert b"members and supervisors see these tickets" not in refused.content
    assert send_again(client, refused, pickup.category, clinic, pickup).status_code == 302
    assert FilledForm.objects.count() == 1


def test_redrawn_intake_after_a_form_change_says_one_thing_and_is_accepted(
    client, capable, admin, clinic, pickup
):
    login(client, capable)
    page = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "client": clinic.pk, "request_form": pickup.pk},
    )
    edit_field(admin, pickup, "count", label="Coolers or boxes")
    refused = send_again(client, page, pickup.category, clinic, pickup)
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert messages_on(refused) == [escape(services.FORM_CHANGED)]
    assert send_again(client, refused, pickup.category, clinic, pickup).status_code == 302


@pytest.mark.django_db(transaction=True)
def test_desk_check_first_then_a_redrawn_form_is_accepted(
    client, capable, admin, clinic, pickup, monkeypatch
):
    """The category changes after the page's checks passed, before the write: the desk's check
    under the category lock refuses; the request form is drawn fresh with it, one message."""
    login(client, capable)
    page = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "client": clinic.pk, "request_form": pickup.pk},
    )
    original = intake.lock_for_intake

    def lock_after_category_change(template):
        other_connection(lambda: category_change(admin, pickup.category, is_restricted=True))
        return original(template)

    with monkeypatch.context() as patch:
        patch.setattr(intake, "lock_for_intake", lock_after_category_change)
        refused = post_intake(
            client,
            pickup.category,
            client=clinic.pk,
            request_form=pickup.pk,
            form_definition=drawn_token(page),
            category_terms=drawn_terms(page),
            **{"field-count": "7", "field-patient": MARK},
        )
    body = refused.content.decode()
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert messages_on(refused) == [escape(terms.CHANGED)]
    assert MARK not in body and 'value="7"' not in body
    assert "members and supervisors see these tickets" in body  # the current settings
    assert send_again(client, refused, pickup.category, clinic, pickup).status_code == 302


def test_portal_category_withdrawal_refuses_valid_old_token(client, admin, portal_user, pickup):
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    token = drawn_token(client.get(url))
    category_change(admin, pickup.category, client_may_raise=False)
    assert client.post(url, {"form_definition": token, "field-count": "1"}).status_code == 302
    assert not FilledForm.objects.exists()


# A refused page is a fresh form, whichever layer refused


@pytest.mark.django_db(transaction=True)
def test_refused_portal_page_scrubs_on_late_definition_change(
    client, portal_user, admin, pickup, monkeypatch
):
    """The form validated, then the definition changed before the service
    compared."""
    url = reverse("portal_forms:raise", args=[pickup.key])
    login(client, portal_user)
    page = client.get(url)
    original = portal_views.create_request_with_form

    def write_after_setup(**kwargs):
        other_connection(lambda: repurpose_patient(admin, pickup))
        return original(**kwargs)

    monkeypatch.setattr(portal_views, "create_request_with_form", write_after_setup)
    response = client.post(
        url, {"form_definition": drawn_token(page), "field-count": "7", "field-patient": MARK}
    )
    body = response.content.decode()
    assert response.status_code == 200 and not FilledForm.objects.exists()
    assert "no-store" in response["Cache-Control"]
    assert services.FORM_CHANGED in body and MARK not in body and 'value="7"' not in body
    assert "Ordinary shipping reference" in body  # drawn from the current definition
    assert drawn_token(response) == definition_of(pickup)


@pytest.mark.django_db(transaction=True)
def test_refused_intake_page_scrubs_on_late_definition_change(
    client, capable, admin, clinic, pickup, monkeypatch
):
    login(client, capable)
    original = intake.lock_for_intake

    def lock_after_setup(template):
        other_connection(lambda: repurpose_patient(admin, pickup))
        return original(template)

    monkeypatch.setattr(intake, "lock_for_intake", lock_after_setup)
    response = post_intake(
        client,
        pickup.category,
        client=clinic.pk,
        request_form=pickup.pk,
        **{"field-count": "7", "field-patient": MARK},
    )
    body = response.content.decode()
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "no-store" in response["Cache-Control"]
    assert services.FORM_CHANGED in body and MARK not in body
    assert 'name="field-count"' in body and 'value="7"' not in body
    assert "Ordinary shipping reference" in body
    assert 'value="Synthetic intake"' in body  # the request's own fields are kept


@pytest.mark.django_db(transaction=True)
def test_refused_correction_page_scrubs_on_late_definition_change(
    client, capable, admin, portal_user, pickup, monkeypatch
):
    record = portal_raise(portal_user, pickup, {"count": 1})
    url = reverse("request_details:edit", args=[record.number])
    login(client, capable)
    page = client.get(url)
    original = request_views.correct_values

    def correct_after_setup(**kwargs):
        other_connection(lambda: repurpose_patient(admin, pickup))
        return original(**kwargs)

    monkeypatch.setattr(request_views, "correct_values", correct_after_setup)
    response = client.post(
        url,
        {
            "form_definition": drawn_token(page),
            "field-patient": MARK,
            "seen_patient": "",
            "field-count": "1",
            "seen_count": current(record)["count"].pk,
        },
    )
    body = response.content.decode()
    assert response.status_code == 200 and MARK not in body
    assert services.FORM_CHANGED in body and "no-store" in response["Cache-Control"]
    assert "patient" not in current(record)


# The token says nothing about the definition


def test_portal_token_omits_staff_only_field_keys(client, admin, portal_user, pickup):
    edit_field(admin, pickup, "courier", holds_patient_information=True)
    login(client, portal_user)
    page = client.get(reverse("portal_forms:raise", args=[pickup.key]))
    assert b'name="field-courier"' not in page.content
    token = drawn_token(page)
    assert re.fullmatch(r"[0-9a-f]{64}", token)  # an opaque keyed digest, nothing readable
    assert "courier" not in token and "patient" not in token


def test_token_needs_the_secret_key(pickup, settings):
    drawn = definition.read(pickup).token
    settings.SECRET_KEY = "another-synthetic-secret-key-for-this-test-only-0123456789"
    assert not definition.read(pickup).matches(drawn)


# Meaning changed outside the services; one lock order


def test_revisionless_label_change_invalidates_definition(client, portal_user, pickup):
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    page = client.get(url)
    assert b"Number of coolers" in page.content
    pickup.fields.filter(key="count").update(label="Number of specimens")
    response = client.post(url, {"form_definition": drawn_token(page), "field-count": "2"})
    assert response.status_code == 200 and not ServiceRequest.objects.exists()


def test_unversioned_offer_change_invalidates_the_page(pickup, clinic, other_org):
    pickup.clients.set([clinic])
    old = definition.token(pickup)
    pickup.clients.add(other_org)
    assert not definition.unchanged(pickup, old)


def test_every_column_is_in_the_definition_or_left_out_on_purpose():
    """A column added to a form, field, catalog, item or category must be decided on: in the
    token's settings, or in definition.LEFT_OUT with the reason there."""
    settings = {
        RequestTemplate: definition.TEMPLATE_SETTINGS,
        TemplateField: definition.FIELD_SETTINGS,
        Catalog: definition.CATALOG_SETTINGS,
        CatalogItem: definition.ITEM_SETTINGS,
        RequestCategory: definition.CATEGORY_SETTINGS,
    }
    for model, names in settings.items():
        columns = {f.attname for f in model._meta.concrete_fields}
        decided = {"id" if name == "pk" else name for name in names}
        decided |= definition.LEFT_OUT[model.__name__]
        assert columns == decided, model.__name__


def _lock_order(queries, tables):
    order = []
    for query in queries:
        sql = query["sql"]
        if re.search(r"FOR (UPDATE|SHARE|NO KEY UPDATE)", sql):
            for table in tables:
                if f'"{table}"' in sql.split(" WHERE ")[0] and table not in order:
                    order.append(table)
    return order


def test_preset_and_capture_lock_shared_resources_in_the_same_order(pickup, catalog, admin):
    """Serial calls, the order of the first lock on each table."""
    from request_forms.presets import load_form_presets

    load_form_presets(actor=admin)
    tables = [RequestTemplate._meta.db_table, Catalog._meta.db_table]
    with CaptureQueriesContext(connection) as preset_queries:
        load_form_presets(actor=admin)
    with CaptureQueriesContext(connection) as capture_queries:
        definition.lock(pickup.pk)
    assert _lock_order(capture_queries, tables) == tables
    assert _lock_order(preset_queries, tables) == tables


def test_staff_intake_locks_the_form_before_the_desk_locks_the_category(
    client, capable, clinic, pickup
):
    login(client, capable)
    tables = [
        RequestTemplate._meta.db_table,
        Catalog._meta.db_table,
        RequestCategory._meta.db_table,
    ]
    with CaptureQueriesContext(connection) as queries:
        response = post_intake(
            client,
            pickup.category,
            client=clinic.pk,
            request_form=pickup.pk,
            **{"field-count": "1"},
        )
    assert response.status_code == 302
    writes = [q for q in queries if "INSERT INTO" in q["sql"] or "FOR " in q["sql"]]
    first_insert = next(i for i, q in enumerate(writes) if "INSERT INTO" in q["sql"])
    assert _lock_order(writes[:first_insert], tables) == tables


@pytest.mark.django_db(transaction=True)
def test_template_and_catalog_are_locked_until_capture_commit(pickup, catalog):
    def lock_available(model):
        try:
            with transaction.atomic():
                type(model).objects.select_for_update(nowait=True).get(pk=model.pk)
        except OperationalError as error:
            assert error.__cause__.sqlstate == "55P03"
            return False
        return True

    with transaction.atomic():
        definition.lock(pickup.pk)
        assert not other_connection(lambda: lock_available(pickup))
        assert not other_connection(lambda: lock_available(catalog))
        assert not other_connection(lambda: lock_available(pickup.category))
    assert other_connection(lambda: lock_available(catalog))


def test_preset_addition_invalidates_drawn_definition(pickup, admin, portal_user):
    from request_forms.presets import load_form_presets

    before = definition.token(pickup)
    counts = load_form_presets(actor=admin)
    assert counts["fields"] > 0 and not definition.unchanged(pickup, before)
    with pytest.raises(ValidationError, match="changed after the page was opened"):
        services.create_request_with_form(
            actor=portal_user,
            template=pickup,
            expected_definition=before,
            values={"count": 1},
            channel="portal",
            first_entry_kind=EntryKind.REPLY,
        )
    assert not FilledForm.objects.exists()


# Replay: a token describes a definition, never authority


def test_replayed_tokens_remain_subject_to_scope(
    client, agent, portal_user, outsider, clinic, other_org, pickup, staff_template
):
    pickup.clients.set([clinic])
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    valid = drawn_token(client.get(url))
    denied = client.post(
        url, {"form_definition": definition.token(staff_template), "field-count": "2"}
    )
    assert denied.status_code == 200 and not ServiceRequest.objects.exists()
    login(client, outsider)
    assert client.post(url, {"form_definition": valid, "field-count": "2"}).status_code == 302
    assert not ServiceRequest.objects.exists()
    login(client, agent)
    chosen = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "client": clinic.pk, "request_form": pickup.pk},
    )
    response = post_intake(
        client,
        pickup.category,
        client=other_org.pk,
        request_form=pickup.pk,
        form_definition=drawn_token(chosen),
        **{"field-count": "2"},
    )
    assert response.status_code == 200 and not ServiceRequest.objects.exists()


def test_same_form_token_is_not_request_authority(admin, agent, capable, portal_user, pickup):
    first = portal_raise(portal_user, pickup, {"count": 1})
    second = portal_raise(portal_user, pickup, {"count": 1})
    category_change(admin, pickup.category, is_restricted=True)
    token = definition_of(first)
    with pytest.raises(ServiceRequest.DoesNotExist):
        services.correct_values(
            actor=agent,
            request=second,
            expected_definition=token,
            values={"count": 2},
            expected={"count": current(second)["count"].pk},
            reason="Recount",
        )
    with pytest.raises(ValidationError):
        services.correct_values(
            actor=capable,
            request=second,
            expected_definition=token,
            values={"count": 2},
            expected={"count": current(first)["count"].pk},
            reason="Recount",
        )
    assert FieldValue.objects.filter(filled_form__request=second, key="count").count() == 1


# The demo seed offers by identity, not by a code looked up again


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-shared-fixture")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")


def test_seed_checks_offer_identity_when_a_code_is_reassigned(demo_env, make_user, client):
    from clients.services import create_client, update_client

    call_command("seed_demo", verbosity=0)
    template = RequestTemplate.objects.get(name="Material request")
    original = template.clients.get()
    code = original.account_number
    owner = User.objects.get(username="demo_admin")
    update_client(
        actor=owner,
        client_id=original.pk,
        expected_revision=original.revision,
        account_number="TEST-RENUMBERED",
        reason="Synthetic account renumbering",
    )
    replacement = create_client(
        actor=owner, name="Synthetic distinct organization", account_number=code
    )
    account = make_user("unrelated_account", client=replacement)
    assert template not in selectors.portal_templates(account)
    call_command("seed_demo", verbosity=0)
    assert list(template.clients.all()) == [original]  # by identity, untouched
    client.force_login(account)
    assert client.get(reverse("portal_forms:raise", args=[template.key])).status_code == 404
    assert template not in selectors.portal_templates(account)


def test_seed_offers_to_the_organizations_it_created(demo_env):
    """A fresh seed: each laboratory type's forms go to the organization objects the clients
    seed created in the same run."""
    from clients.models import Client
    from request_forms.seed import demo_offers

    call_command("seed_demo", verbosity=0)
    for key, codes in demo_offers().items():
        template = RequestTemplate.objects.filter(key=key).first()
        if template is None:
            continue
        expected = set(Client.objects.filter(account_number__in=codes).values_list("pk", flat=True))
        assert set(template.clients.values_list("pk", flat=True)) == expected, key
