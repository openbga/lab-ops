"""Second confirmation of kept typing on synthetic records through normal form pages.

Adopted from the second confirmation round (.local/review-history/kept-typing-round-1-
confirmation-2.md; the original file is kept beside it). Cases changed while adopting are marked
"Adopted:" with the reason.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import pytest
from django.core import signing
from django.db import connection
from django.db.models.functions import Now
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft, KeptDraftCutoff
from clients.models import ClientSite, PortalAccess
from desk.portal_scope import site_required
from desk.tests import test_kept_typing as pages
from request_forms.models import Audience, Catalog
from request_forms.models import FieldKind as K
from request_forms.services import update_field
from tests import test_review_kept_typing_confirm as first
from tests import test_review_kept_typing_verify as previous
from tests.test_review_session_list_verify import in_worker

case = previous.case
pytestmark = pytest.mark.django_db


def test_control_kinds_travel_from_the_drawn_form_to_the_first_copy(case, staff):
    catalog = Catalog.objects.create(key="cartons", name="Cartons")
    box = catalog.items.create(name="Small carton", unit="each")
    kinds = {
        K.SHORT_TEXT: "text",
        K.LONG_TEXT: "text",
        K.DATE: "date",
        K.DATETIME: "datetime",
        K.TIME: "time",
        K.NUMBER: "number",
        K.CHOICE: "choice",
        K.MULTIPLE_CHOICE: "choices",
        K.YES_NO: "choice",
        K.CONFIRMATION: "box",
        K.ITEMS: "number",
    }
    expected = {}
    for kind, family in kinds.items():
        settings = (
            {"choices": ["on", "off"]} if kind == K.CHOICE else first.definition(kind, catalog)
        )
        field = previous.add_field(case, kind.value, kind, **settings)
        suffix = f"_{box.pk}" if kind == K.ITEMS else ""
        expected[f"field-{field.key}{suffix}"] = family
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    signed = kept._read(token)
    assert signed["w"].keys() == set(signed["f"])
    assert expected.items() <= signed["w"].items()
    assert "field-reference" not in signed["w"]
    # An edit between draw and keep must not relabel the original answer's provenance.
    choice = case.template.fields.get(key=K.CHOICE.value)
    update_field(
        actor=staff("origin_editor", "admin"),
        field_id=choice.pk,
        expected_revision=choice.revision,
        kind=K.CONFIRMATION,
        reason="Require a tick.",
    )
    assert pages.send(browser, token, {"subject": "Courier", "field-choice": "on"}) == 1
    draft = KeptDraft.objects.get()
    assert draft.controls == signed["w"] and draft.controls.keys() == draft.values.keys()
    browser.force_login(case.portal)
    page = browser.get(url)
    assert " checked" not in page.context["form"]["field-choice"].as_widget()
    assert previous.missed(page)


@pytest.mark.parametrize("source", ["stored row", "open older form"])
@pytest.mark.parametrize(
    "before,answer", [(K.CHOICE, "on"), (K.SHORT_TEXT, "Gate three"), (K.CONFIRMATION, "on")]
)
def test_copies_predating_control_kinds_preserve_text_and_require_actual_confirmation(
    case, staff, monkeypatch, source, before, answer
):
    field = previous.add_field(
        case, "operation", before, **({"choices": ["on", "off"]} if before == K.CHOICE else {})
    )
    url = previous.raise_page(case)
    with monkeypatch.context() as patch:
        if source == "open older form":
            # The previous renderer signed exactly this field set. Use the real signer on draw.
            original = signing.dumps

            def old_draw(data, *args, **kwargs):
                if kwargs.get("salt") == kept.SALT:
                    data = {k: v for k, v in data.items() if k != "w"}
                return original(data, *args, **kwargs)

            patch.setattr(signing, "dumps", old_draw)
        browser, token = pages.typed_then_signed_out(case.portal, url)
    if source == "open older form":
        assert kept._read(token)["w"] == {}
    assert (
        pages.send(browser, token, {"field-operation": answer, "subject": "Synthetic collection"})
        == 1
    )
    if source == "stored row":
        KeptDraft.objects.update(controls={})
    assert KeptDraft.objects.get().controls == {}
    if before == K.CHOICE:
        update_field(
            actor=staff("legacy_editor", "admin"),
            field_id=field.pk,
            expected_revision=field.revision,
            kind=K.CONFIRMATION,
            label="I confirm the collection details",
            reason="Require acknowledgement.",
        )
    browser.force_login(case.portal)
    page = browser.get(url)
    current = page.context["form"]["field-operation"]
    assert page.context["form"]["subject"].value() == "Synthetic collection"
    if before == K.SHORT_TEXT:
        assert current.value() == answer
    else:
        # Adopted: a value whose control kind is not known never ticks a box, a genuine earlier
        # tick included (such copies exist only on a database that ran an unreleased candidate):
        # the box starts clear and the page says something was not put back.
        assert " checked" not in current.as_widget()
        assert previous.missed(page)


@pytest.mark.parametrize(
    "change",
    [
        "deleted",
        "retired",
        "staff only",
        "heading",
        "protected",
        "new catalog",
        "new kind",
        "item retired",
        "catalog retired",
    ],
)
def test_template_recheck_matches_the_controls_available_on_a_new_portal_form(case, staff, change):
    items = change in {"new catalog", "new kind", "item retired", "catalog retired"}
    catalog = Catalog.objects.create(key="pallets", name="Pallets")
    item = catalog.items.create(name="Return tray", unit="each")
    field = previous.add_field(
        case,
        "logistics",
        K.ITEMS if items else K.SHORT_TEXT,
        **({"catalog": catalog} if items else {}),
    )
    name = "field-logistics" + (f"_{item.pk}" if items else "")
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    assert name in kept._read(token)["f"]
    if change == "deleted":
        field.delete()
    elif change in {"item retired", "catalog retired"}:
        row = item if change == "item retired" else catalog
        row.is_active = False
        row.save(update_fields=["is_active"])
    else:
        changes = {
            "retired": {"is_active": False},
            "staff only": {"audience": Audience.STAFF},
            "heading": {"kind": K.HEADING},
            "protected": {"holds_patient_information": True},
            "new kind": {"kind": K.SHORT_TEXT},
        }
        change_data = (
            {"catalog": Catalog.objects.create(key="totes", name="Totes")}
            if change == "new catalog"
            else changes[change]
        )
        update_field(
            actor=staff("edit_definition", "admin"),
            field_id=field.pk,
            expected_revision=field.revision,
            reason="Update collection form.",
            **change_data,
        )
    # The original form was eligible. Compare with what a fresh draw actually signs.
    current = pages.browser_for(case.portal).get(url)
    assert name not in kept._read(pages.marker(current)[0])["f"]
    answer = "3" if items else "Bring the return tray"
    assert pages.send(browser, token, {"description": "Tuesday collection", name: answer}) == 1
    draft = KeptDraft.objects.get()
    assert name not in draft.values and name not in draft.controls


@pytest.mark.parametrize("which", ["general", "template"])
@pytest.mark.parametrize("scope", ["whole", "one listed", "no grant"])
def test_portal_site_rule_agrees_on_draw_and_post(case, which, scope):
    a = ClientSite.objects.create(client_id=case.portal.client_id, name="East office", code="E")
    b = ClientSite.objects.create(client_id=case.portal.client_id, name="West office", code="W")
    grant = PortalAccess.objects.get(user=case.portal)
    if scope == "no grant":
        grant.delete()
    elif scope == "one listed":
        grant.whole_organization = False
        grant.save(update_fields=["whole_organization"])
        grant.sites.set([a])
    expected = {"whole": {a.pk, b.pk}, "one listed": {a.pk}, "no grant": set()}[scope]
    browser = pages.browser_for(case.portal)
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    page = browser.get(url)
    form = page.context["form"]
    assert site_required(page.wsgi_request.user, site_count=len(expected)) == bool(expected)
    assert ("site" in form.fields) == bool(expected)
    if expected:
        assert form.fields["site"].required
        assert set(form.fields["site"].queryset.values_list("pk", flat=True)) == expected
    if scope == "one listed":
        assert form["site"].value() == a.pk
    data = {h.html_name: h.value() or "" for h in form.hidden_fields()}
    data.update(subject="Collection", description="Return boxes")
    if which == "general":
        data.update(category=case.category.pk, category_terms=page.context["category_terms"])
    if expected:
        rejected = browser.post(url, data)
        assert rejected.status_code == 200 and "site" in rejected.context["form"].errors
        data["site"] = a.pk
    created = browser.post(url, data)
    assert (
        created.status_code == 302
        and type(case.ticket).objects.filter(subject="Collection").exists()
    )


@pytest.mark.parametrize("which", ["general", "template"])
@pytest.mark.parametrize("scope,count", [("whole", 2), ("listed", 1)])
@pytest.mark.django_db(transaction=True)
def test_site_deactivation_during_open_leaves_a_coherent_site_control(
    case, which, scope, count, monkeypatch
):
    from desk.portal_forms import RaiseRequestForm
    from request_forms.forms import PortalRaiseForm

    rows = [
        ClientSite.objects.create(client_id=case.portal.client_id, name=f"Office {i}", code=f"O{i}")
        for i in range(count)
    ]
    if scope == "listed":
        grant = PortalAccess.objects.get(user=case.portal)
        grant.whole_organization = False
        grant.save(update_fields=["whole_organization"])
        grant.sites.set(rows)
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    form_type = RaiseRequestForm if which == "general" else PortalRaiseForm
    original = form_type.__init__
    browser = pages.browser_for(case.portal)
    with ThreadPoolExecutor(max_workers=1) as pool:

        def build(form, *args, **kwargs):
            original(form, *args, **kwargs)
            assert form.fields["site"].required
            assert (
                pool.submit(
                    in_worker,
                    lambda: ClientSite.objects.filter(pk=rows[-1].pk).update(is_active=False),
                ).result(timeout=10)
                == 1
            )

        with monkeypatch.context() as patch:
            patch.setattr(form_type, "__init__", build)
            page = browser.get(url)
    assert page.status_code == 200
    offered = [row for row in rows if row.name in page.text]
    form = page.context["form"]
    if not offered:
        assert "site" not in form.fields, "A required site selector offered no site"
    elif scope == "whole":
        assert form.fields["site"].required == (len(offered) > 1)


@pytest.mark.xfail(
    strict=True,
    reason="Adopted: accepted limitation stated in docs/architecture.md (a browser without the "
    "keep cookie, or one whose cookie is deleted, can end with one of two values and lose a copy "
    "kept under the other; the cookie is HttpOnly and nothing on the server can tell which the "
    "browser kept).",
)
def test_cookie_loss_for_two_sessionless_keeps_from_one_ended_session(case):
    # A returning browser can have two open forms after its 24-hour keep cookie expired.
    browser = pages.browser_for(case.portal)
    urls = [reverse("portal:request-create"), previous.raise_page(case)]
    tokens = [pages.marker(browser.get(url))[0] for url in urls]
    pages.end_open(case.portal)
    del browser.cookies[kept.BROWSER_COOKIE]
    tabs = [first.copy_tab(browser) for _ in urls]
    notes = ["East collection", "West collection"]
    for tab, token, note in zip(tabs, tokens, notes, strict=True):
        assert pages.send(tab, token, {"subject": note}) == 1
        browser.cookies[kept.BROWSER_COOKIE] = tab.cookies[kept.BROWSER_COOKIE].value
    browser.force_login(case.portal)
    restored = [pages.given_back(browser.get(url)) for url in urls]
    assert [record and record["subject"] for record in restored] == notes


@pytest.mark.xfail(
    strict=True,
    reason="Adopted: accepted limitation stated in docs/architecture.md (a browser without the "
    "keep cookie, or one whose cookie is deleted, can end with one of two values and lose a copy "
    "kept under the other; the cookie is HttpOnly and nothing on the server can tell which the "
    "browser kept).",
)
def test_two_cold_signins_can_deliver_in_request_order_without_losing_the_earlier_copy(case):
    url = reverse("portal:request-create")
    jar = pages.browser_for(case.portal)
    first_page = first.copy_tab(jar).get(url)
    pages.end_open(case.portal)
    jar.force_login(case.portal)
    second_page = first.copy_tab(jar).get(url)
    # Both requests were made without the cookie. Deliver the first page first.
    jar.cookies[kept.BROWSER_COOKIE] = first_page.cookies[kept.BROWSER_COOKIE].value
    assert (
        pages.send(jar, pages.marker(first_page)[0], {"subject": "Earlier sign-in's collection"})
        == 1
    )
    jar.cookies[kept.BROWSER_COOKIE] = second_page.cookies[kept.BROWSER_COOKIE].value
    assert "Earlier sign-in's collection" in jar.get(url).text


@pytest.mark.parametrize(
    "change,expected",
    [
        ("eligible", 1),
        ("taken", 0),
        ("expired", 0),
        ("cutoff", 0),
        ("deleted form", 0),
        ("other browser", 0),
    ],
)
def test_empty_receipt_matches_whether_the_browser_can_still_retrieve_the_copy(
    case, change, expected
):
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    assert pages.send(browser, token, {"subject": "Collection note"}) == 1
    browser.force_login(case.portal)
    if change == "taken":
        assert pages.given_back(browser.get(url))["subject"] == "Collection note"
    elif change == "expired":
        draft = KeptDraft.objects.get()
        AccountSession.objects.filter(pk=draft.session_id).update(
            ended_at=Now() - timedelta(days=1)
        )
    elif change == "cutoff":
        KeptDraftCutoff.objects.create(user=case.portal, discarded_at=Now())
    elif change == "deleted form":
        case.template.fields.all().delete()
        case.template.delete()
    elif change == "other browser":
        browser = pages.browser_for(case.portal)
    receipt = pages.send(browser, token, {})
    page = browser.get(url)
    assert (page.status_code == 200 and bool(pages.given_back(page))) == bool(expected)
    assert receipt == expected


@pytest.mark.parametrize("path", ["rendered action", "successful POST", "GET return"])
def test_only_queue_parameters_travel_through_the_staff_composer(case, path):
    browser = pages.browser_for(case.agent)
    detail = pages.ticket_page(case.ticket)
    pairs = [("q", "East & West"), ("page", "2"), ("sort", "number"), ("q", "Last + query")]
    old = urlencode([*pairs, ("display_hint", "compact")])
    page = browser.get(detail, {"list": old})
    action = page.context["composer_action"]
    if path == "successful POST":
        result = browser.post(
            action,
            {
                "kind": "note",
                "body": "Normal collection note",
                "expected_revision": case.ticket.revision,
                "list": old,
            },
        )
        assert result.status_code == 302
        target = result.url
    elif path == "GET return":
        result = browser.get(action)
        assert result.status_code == 302
        target = result.url
    else:
        target = action
    nested = parse_qs(urlsplit(target).query)["list"][0]
    # Adopted: the staff ticket page carries the queue's address as the person chose it
    # (desk.views._queue_address), as all its other links and forms do, so a parameter the queue
    # ignores travels too; its own parameters keep their values and order.
    assert parse_qs(nested) == parse_qs(old)
    assert [v for k, v in parse_qsl(nested) if k == "q"] == ["East & West", "Last + query"]


@pytest.mark.parametrize("which", ["general", "template"])
@pytest.mark.parametrize("scope,others", [("whole", 0), ("whole", 1), ("listed", 0)])
def test_post_of_a_selected_site_that_was_deactivated_preserves_the_ticket_scope(
    case, which, scope, others
):
    chosen = ClientSite.objects.create(
        client_id=case.portal.client_id, name="Original office", code="O"
    )
    if others:
        ClientSite.objects.create(client_id=case.portal.client_id, name="Other office", code="W")
    if scope == "listed":
        grant = PortalAccess.objects.get(user=case.portal)
        grant.whole_organization = False
        grant.save(update_fields=["whole_organization"])
        grant.sites.set([chosen])
    browser = pages.browser_for(case.portal)
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    page = browser.get(url)
    form = page.context["form"]
    assert form.fields["site"].queryset.filter(pk=chosen.pk).exists()
    data = {h.html_name: h.value() or "" for h in form.hidden_fields()}
    data.update(subject="Site-specific collection", description="Return boxes", site=chosen.pk)
    if which == "general":
        data.update(category=case.category.pk, category_terms=page.context["category_terms"])
    chosen.is_active = False
    chosen.save(update_fields=["is_active"])
    response = browser.post(url, data)
    made = type(case.ticket).objects.filter(subject=data["subject"])
    assert not made.exists(), f"A ticket was accepted with site {made.get().site_id!r}"
    assert response.status_code == 200 and response.context["form"].errors


@pytest.mark.django_db(transaction=True)
@pytest.mark.skip(
    reason="Adopted: not applicable. It upgrades a database that ran the unreleased candidate "
    "9ee7dc7's migration 0005; that migration was never merged or deployed, so no installation "
    "has it; the released upgrade path from df6e116 is tested in tests/test_review_kept_typing.py."
)
def test_migrate_reads_a_copy_written_by_the_previous_candidate():
    """Upgrade the real parent schema in a private schema of this test's disposable database."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from uuid import uuid4

    parent = os.environ.get("KEPT_CONFIRM2_PRIOR")
    if not parent:
        pytest.skip("Set KEPT_CONFIRM2_PRIOR to the 9ee7dc7 checkout for this candidate upgrade")
    db = connection.settings_dict["NAME"]
    assert db.startswith("test_") and db == os.environ["TEST_DATABASE_NAME"]
    schema = "kept_confirm2_" + uuid4().hex
    env = os.environ.copy()
    env.pop("TEST_DATABASE_NAME")
    env["DATABASE_URL"] = urlsplit(env["DATABASE_URL"])._replace(path="/" + db).geturl()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    env["PGOPTIONS"] = f"-c search_path={schema}"
    bootstrap = """
import sys, json, django
django.setup()
from django.core.management import call_command
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("select current_database(), current_schema()")
    assert cursor.fetchone() == tuple(sys.argv[1:])
call_command("migrate", interactive=False, verbosity=0)
"""
    old_writer = """
from django.utils import timezone
from accounts.models import User, AccountSession, KeptDraft
assert "controls" not in {field.name for field in KeptDraft._meta.fields}
person = User.objects.create_user("confirm_previous_candidate")
session = AccountSession.objects.create(user=person, ended_at=timezone.now(), ending="inactivity")
KeptDraft.objects.create(user=person,session=session,browser="b"*64,form="portal-ticket",
                         values={"subject":"Collection from previous candidate"})
"""
    new_reader = """
from django.db import ProgrammingError
from accounts.models import KeptDraft
with connection.cursor() as cursor:
    names = [col.name for col in connection.introspection.get_table_description(
        cursor, KeptDraft._meta.db_table)]
try:
    KeptDraft.objects.get()
    read_error = None
except ProgrammingError as error:
    read_error = error.__cause__.sqlstate
subjects = list(KeptDraft.objects.values_list("values__subject", flat=True))
print(json.dumps({"columns": names, "read_error": read_error, "subjects": subjects}))
"""
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    try:

        def run(root, body):
            return subprocess.run(
                [sys.executable, "-c", bootstrap + body, db, schema],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )

        run(parent, old_writer)
        current = run(Path(__file__).resolve().parents[1], new_reader)
        result = json.loads(current.stdout.strip().splitlines()[-1])
        assert result["subjects"] == ["Collection from previous candidate"]
        assert result["read_error"] is None and "controls" in result["columns"], result
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("change", ["heading", "new catalog"])
def test_reopened_form_reports_a_populated_answer_the_keep_recheck_had_to_drop(case, staff, change):
    catalog = Catalog.objects.create(key="packing", name="Packing")
    item = catalog.items.create(name="Box", unit="each")
    field = previous.add_field(case, "loading", K.ITEMS, catalog=catalog)
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    original_name = f"field-loading_{item.pk}"
    assert original_name in kept._read(token)["f"]
    changes = (
        {"kind": K.HEADING}
        if change == "heading"
        else {"catalog": Catalog.objects.create(key="supplies", name="Supplies")}
    )
    update_field(
        actor=staff("packing_editor", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        reason="Update the packing form.",
        **changes,
    )
    assert pages.send(browser, token, {"description": "Deliver at noon", original_name: "3"}) == 1
    assert original_name not in KeptDraft.objects.get().values
    browser.force_login(case.portal)
    page = browser.get(url)
    assert page.context["form"]["description"].value() == "Deliver at noon"
    assert previous.missed(page), "The restored form did not say the quantity was left out"
