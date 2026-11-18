"""The demonstration seed of request forms (F7, DM1) once the category presets are there."""

import pytest
from django.core.management import call_command

from request_forms.models import FieldValue, FilledForm, RequestTemplate, TemplateField
from request_forms.presets import FORM_PRESETS
from taxonomy.models import RequestCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def categories(db):
    """Every category the form presets name, as the category presets would make them."""
    group = RequestCategory.objects.create(key="seed-group", name="Seed group")
    keys = {t.category for preset in FORM_PRESETS.values() for t in preset.templates}
    for key in sorted(keys):
        RequestCategory.objects.get_or_create(
            key=key,
            defaults={
                "name": key.replace("-", " ").capitalize(),
                "parent": group,
                "client_may_raise": True,
            },
        )


def test_seed_raises_requests_with_forms_and_runs_twice(monkeypatch, categories):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-test-password-only")
    call_command("seed_demo", verbosity=0)
    assert RequestTemplate.objects.count() == len(
        {t.key for preset in FORM_PRESETS.values() for t in preset.templates}
    )
    assert FilledForm.objects.count() >= 4
    assert FieldValue.objects.filter(superseded_at__isnull=False).exists()
    switched_on = TemplateField.objects.filter(holds_patient_information=True, is_active=True)
    assert list(switched_on.values_list("template__key", "key")) == [
        ("add-on", "patient-identifier")
    ]
    counts = (FilledForm.objects.count(), FieldValue.objects.count())
    call_command("seed_demo", verbosity=0)
    assert (FilledForm.objects.count(), FieldValue.objects.count()) == counts


# What each demonstration organization is offered --------------------------------------------

CLINIC_MENU = {"pickup", "supply-order", "result-status", "urgent-pickup"}
WATER_MENU = {"pickup", "supply-order", "result-status", "bottle-kit-order"}


def test_demo_offers_each_laboratory_types_forms_to_its_organizations(monkeypatch):
    from accounts.models import User
    from core.models import AuditEvent
    from request_forms.seed import DEMO_OFFERS, demo_offers
    from request_forms.selectors import portal_menu, portal_templates

    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-test-password-only")
    call_command("seed_demo", verbosity=0)
    clinic, water = (
        User.objects.get(username=name) for name in ("demo_client", "demo_client_water")
    )
    assert set(portal_menu(clinic).values_list("key", flat=True)) == CLINIC_MENU
    assert set(portal_menu(water).values_list("key", flat=True)) == WATER_MENU
    clinic_forms = set(portal_templates(clinic).values_list("key", flat=True))
    water_forms = set(portal_templates(water).values_list("key", flat=True))
    common = {t.key for t in FORM_PRESETS["all"].templates}
    for preset, accounts in DEMO_OFFERS.items():
        own = {t.key for t in FORM_PRESETS[preset].templates} - common
        seeded = own & set(RequestTemplate.objects.values_list("key", flat=True))
        assert seeded, preset  # the demo's categories carry each type's forms
        assert (seeded <= clinic_forms) == ("EXCLIN" in accounts), preset
        assert (seeded <= water_forms) == ("EXWATER" in accounts), preset
    assert "material-request" not in clinic_forms | water_forms
    assert common & set(RequestTemplate.objects.values_list("key", flat=True)) <= clinic_forms
    # Each offering is the organizations of its types, by account code.
    for template in RequestTemplate.objects.filter(key__in=demo_offers()):
        codes = set(template.clients.values_list("account_number", flat=True))
        assert codes == demo_offers()[template.key], template.key
    # Seeding again changes no offering.
    updates = AuditEvent.objects.filter(action="request_form.updated").count()
    call_command("seed_demo", verbosity=0)
    assert AuditEvent.objects.filter(action="request_form.updated").count() == updates
