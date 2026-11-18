"""Demonstration seed for request forms (core.seeding), after the category presets.

Setup: every standard form preset, loaded as an admin would load them (forms whose category is
not set up are skipped).

Records: each laboratory type's forms are offered only to the demonstration organizations of that
type (DEMO_OFFERS), through the audited template service, so a clinic's service menu shows
clinical forms and never the biorepository's; the forms for all laboratories stay offered to every
organization. This runs before the desk's story (core.seeding.SEED_APPS), so the story's portal
requests go through the same offering. A form two types share (a requisition or chain of custody
correction) is offered to the organizations of both.

The organizations are the ones the clients seed created in this same run, held as objects
(core.seeding.SeedContext.created), never looked up again by account code: a renumbered
organization, or another one given its old code, can neither lose nor draw an offer (reviews 5
and 6). Seeding again creates no organizations and leaves every offer as it is. An empty list
means every organization, so the seed never writes one.

The desk's demonstration story (desk/story.py) raises the requests with forms inside its timeline:
pickups (routine and urgent), a supply and a bottle-kit order with items, a result question, a
technical-support request, an add-on with the patient identifier field switched on with a reason,
and a phone call logged with required details not known yet; staff fill shown and staff-only
fields and correct a value with its reason. All values are synthetic.
"""

from django.core.management.base import CommandError

from accounts.models import User

from .models import RequestTemplate
from .presets import FORM_PRESETS, load_form_presets
from .services import update_template

ADMIN = "demo_admin"
# Laboratory-type preset -> account codes of the demonstration organizations of that type
# (clients/seed.py).
DEMO_OFFERS = {
    "clinical": ("EXCLIN", "EXHS", "EXGEN"),
    "toxicology": ("EXREC", "EXFRT"),
    "environmental": ("EXWATER",),
    "food-manufacturing": ("EXMFG",),
    "biorepository": ("EXRES",),
}
OFFER_REASON = "Demonstration: offered to the organizations that use this kind of laboratory work."


def seed_setup(ctx):
    load_form_presets(actor=User.objects.get(username=ADMIN), keys=list(FORM_PRESETS))


def demo_offers():
    """{template key: set of account codes} for the laboratory-type forms; a form of the
    preset for all laboratories is not listed (offered to everyone)."""
    common = {template.key for template in FORM_PRESETS["all"].templates}
    offers = {}
    for preset, accounts in DEMO_OFFERS.items():
        for template in FORM_PRESETS[preset].templates:
            if template.key not in common:
                offers.setdefault(template.key, set()).update(accounts)
    return offers


def seed_records(ctx):
    """Offer each laboratory type's forms to its organizations, identified by the objects the
    clients seed created in this same run (ctx.created), never by looking an account code up
    again. Renumbered and reassigned account codes cannot change an offer. Existing organizations
    keep their offers on subsequent runs."""
    created = ctx.created.get("clients") or {}
    if not created:
        return
    admin = User.objects.get(username=ADMIN)
    offers = demo_offers()
    missing = sorted({code for codes in offers.values() for code in codes} - set(created))
    if missing:
        # The two seeds disagree: offering to the rest would narrow an offer wrongly, and to none
        # would offer it to everyone. Stop, and nothing of the seed is kept.
        raise CommandError(
            "Demonstration organizations not created by this seed: " + ", ".join(missing) + "."
        )
    for template in RequestTemplate.objects.filter(key__in=offers).order_by("pk"):
        wanted = {created[code].pk for code in offers[template.key]}
        if set(template.clients.values_list("pk", flat=True)) == wanted:
            continue
        update_template(
            actor=admin,
            template_id=template.pk,
            expected_revision=template.revision,
            reason=OFFER_REASON,
            clients=sorted(wanted),
        )
