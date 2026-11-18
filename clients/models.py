"""Client organizations the laboratory serves, and the people the laboratory deals with there.

A client portal account is an accounts.User whose `client` points here (clients.portal scopes
everything such an account reads). Staff management pages are in clients.views; changes go
through clients.services so each one is audited.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F
from django.db.models.functions import Upper

from core.vocabulary import US_STATES


class OrganizationType(models.TextChoices):
    HEALTHCARE = "healthcare", "Healthcare facility or system"
    PRACTICE = "practice", "Physician practice or clinic"
    REFERRING_LAB = "referring_lab", "Referring laboratory"
    EMPLOYER = "employer", "Employer or program administrator"
    UTILITY = "utility", "Utility or municipality"
    CONSULTING = "consulting", "Engineering or consulting firm"
    MANUFACTURER = "manufacturer", "Manufacturer or food producer"
    RESEARCH = "research", "Research sponsor or institution"
    INTERNAL = "internal", "Internal department"
    OTHER = "other", "Other"


class Responsibility(models.TextChoices):
    """What a contact deals with. They route work; they are not a record of legal authority to
    receive results, which stays in the laboratory's procedures and source systems."""

    ORDERING = "ordering", "Ordering"
    RESULTS = "results", "Results and reports"
    BILLING = "billing", "Billing"
    LOGISTICS = "logistics", "Supplies and pickups"
    QUALITY = "quality", "Quality"
    TECHNICAL = "technical", "Technical"
    GENERAL = "general", "General"


class Client(models.Model):
    name = models.CharField(max_length=160)
    organization_type = models.CharField(
        max_length=20, blank=True, choices=OrganizationType.choices
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        verbose_name="parent organization",
        help_text="A health system, utility or sponsor this organization belongs to.",
    )
    account_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_clients",
        help_text="The laboratory staff member who looks after the relationship.",
    )
    account_number = models.CharField(
        "Account code",
        max_length=40,
        blank=True,
        help_text="The laboratory's short code for this client. Unique when given.",
    )
    email = models.EmailField(
        "General email", blank=True, help_text="The organization's shared address, if any."
    )
    phone = models.CharField("Main phone", max_length=40, blank=True)
    address_line1 = models.CharField("Street address", max_length=160, blank=True)
    address_line2 = models.CharField("Suite, building or PO box", max_length=160, blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=2, blank=True, choices=US_STATES)
    postal_code = models.CharField("ZIP code", max_length=12, blank=True)
    notes = models.TextField(blank=True, help_text="Internal notes. Not shown to the client.")
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        verbose_name = "client organization"
        constraints = [
            models.UniqueConstraint(
                fields=["account_number"],
                condition=~models.Q(account_number=""),
                name="client_account_number",
                violation_error_message="Another client organization already uses this account "
                "code.",
            ),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("pk")),
                name="client_not_own_parent",
                violation_error_message="An organization cannot be its own parent.",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def address_lines(self):
        """Mailing address as display lines; empty when no part is recorded."""
        locality = " ".join(
            part
            for part in (
                f"{self.city}," if self.city and self.state else self.city,
                self.state,
                self.postal_code,
            )
            if part
        )
        return [line for line in (self.address_line1, self.address_line2, locality) if line]


class ClientContact(models.Model):
    """A person at a client organization the laboratory deals with. A contact is a record the
    laboratory keeps; it is not a sign-in account (portal accounts are accounts.User)."""

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="contacts")
    name = models.CharField(max_length=160)
    role = models.CharField(
        max_length=80, blank=True, help_text="What the laboratory contacts this person about."
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    responsibilities = ArrayField(
        models.CharField(max_length=20, choices=Responsibility.choices),
        default=list,
        blank=True,
        help_text="What this person deals with. Used to route work; not a record of authority "
        "to receive results.",
    )
    sites = models.ManyToManyField(
        "ClientSite",
        blank=True,
        related_name="contacts",
        verbose_name="sites served",
        help_text="No sites means the whole organization.",
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-is_active", "name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(email="") | ~models.Q(phone=""),
                name="client_contact_reachable",
                violation_error_message="Give an email address or a phone number.",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.client})"

    def get_responsibilities_display(self):
        labels = dict(Responsibility.choices)
        return [labels.get(code, code) for code in self.responsibilities]


class ClientSite(models.Model):
    """A place a client organization works from or the laboratory serves: an office, a hospital
    department, a collection site, a plant, a sampling location. Requests and portal access can
    be narrowed to sites. Deactivated, never deleted."""

    class Kind(models.TextChoices):
        OFFICE = "office", "Office"
        FACILITY = "facility", "Facility or department"
        COLLECTION_SITE = "collection_site", "Collection site"
        PLANT = "plant", "Plant"
        SAMPLING_LOCATION = "sampling_location", "Sampling location"
        OTHER = "other", "Other"

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="sites")
    code = models.CharField(
        "Site code",
        max_length=40,
        blank=True,
        help_text="The laboratory's short code for this site. Unique within the organization "
        "when given.",
    )
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OFFICE)
    address_line1 = models.CharField("Street address", max_length=160, blank=True)
    address_line2 = models.CharField("Suite, building or PO box", max_length=160, blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=2, blank=True, choices=US_STATES)
    postal_code = models.CharField("ZIP code", max_length=12, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    instructions = models.TextField(
        blank=True, help_text="Internal instructions. Not shown to the client."
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["client", "-is_active", "name", "pk"]
        verbose_name = "client site"
        constraints = [
            models.UniqueConstraint(
                F("client"),
                Upper("code"),
                condition=~models.Q(code=""),
                name="client_site_code",
                violation_error_message="Another site of this organization already uses this code.",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.client})"


class PortalAccess(models.Model):
    """What one client portal account may see of its organization's requests: the whole
    organization or listed sites (plus, always, the requests it raised itself). Exactly one per
    portal account; an account without one sees only the requests it raised. Same-organization
    rules for the contact and the sites are checked in clients.services.set_portal_access."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="portal_access"
    )
    # The organization this access was granted for. The access applies only while it is still the
    # account's organization (applies_to): an account moved to another organization by any path,
    # even one that bypasses clients.services, keeps no grant from the old one.
    client = models.ForeignKey(
        Client,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="portal_accesses",
        verbose_name="granted for",
    )
    contact = models.ForeignKey(
        ClientContact,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="portal_accesses",
        verbose_name="linked contact",
    )
    whole_organization = models.BooleanField(default=False)
    sites = models.ManyToManyField(ClientSite, blank=True, related_name="portal_accesses")
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "portal access"
        verbose_name_plural = "portal access"

    def __str__(self):
        return f"Portal access of {self.user}"

    def applies_to(self, user):
        """Whether this access is the account's current grant: same account, same organization."""
        return (
            self.user_id == user.pk
            and user.client_id is not None
            and self.client_id == user.client_id
        )
