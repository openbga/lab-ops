from django import forms
from django.db import models

from core.forms import REASON_HELP, ActiveFilter, GroupedForm, StaffChoiceField, StyledForm
from core.lookups import RemoteSelect
from core.vocabulary import US_STATES
from core.widgets import ListOrOtherField, TelInput

from .models import Client, ClientContact, ClientSite, OrganizationType, Responsibility

# Suggestions only: the role is free text, and "Other" takes any wording the laboratory uses.
CONTACT_ROLES = (
    "Primary contact",
    "Billing",
    "Results and reports",
    "Ordering and requisitions",
    "Technical or quality",
)


class ParentField(forms.ModelChoiceField):
    def __init__(self, **kwargs):
        super().__init__(
            queryset=Client.objects.all(),
            required=False,
            empty_label="No parent organization",
            widget=RemoteSelect("clients"),
            label="Parent organization",
            help_text="The health system, utility or sponsor this organization belongs to, if any.",
            **kwargs,
        )


class ClientForm(GroupedForm, StyledForm, forms.ModelForm):
    parent = ParentField()
    account_owner = StaffChoiceField(
        required=False,
        empty_label="No account owner",
        label="Account owner",
        help_text="The laboratory staff member who looks after the relationship.",
    )

    groups = (
        (
            "Organization",
            (
                ("name", "col-md-8"),
                ("account_number", "col-md-4"),
                ("organization_type", "col-md-6"),
                ("parent", "col-md-6"),
                ("account_owner", "col-md-6"),
            ),
        ),
        ("Reaching the organization", (("email", "col-md-7"), ("phone", "col-md-5"))),
        (
            "Mailing address",
            (
                ("address_line1", "col-md-7"),
                ("address_line2", "col-md-5"),
                ("city", "col-md-5"),
                ("state", "col-md-4"),
                ("postal_code", "col-md-3"),
            ),
        ),
        ("Notes", (("notes", "col-12"),)),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = Client
        fields = [
            "name",
            "account_number",
            "organization_type",
            "parent",
            "account_owner",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "notes",
        ]
        labels = {
            "name": "Organization name",
            "notes": "Internal notes",
            "organization_type": "Organization type",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3}), "phone": TelInput()}


class ClientEditForm(ClientForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:  # the service refuses deeper loops; this keeps itself off the list
            self.fields["parent"].queryset = Client.objects.exclude(pk=self.instance.pk)

    class Meta(ClientForm.Meta):
        fields = [*ClientForm.Meta.fields, "is_active"]
        labels = {**ClientForm.Meta.labels, "is_active": "Active"}
        help_texts = {
            "is_active": "Clear to stop working with this organization. Its records and history "
            "stay readable, and its portal accounts keep their own Active setting."
        }


class SiteChoices(forms.ModelMultipleChoiceField):
    """Sites of one organization as checkboxes: its active sites plus any already chosen."""

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", ClientSite.objects.none())
        kwargs.setdefault("required", False)
        kwargs.setdefault(
            "widget", forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"})
        )
        super().__init__(**kwargs)

    def limit_to(self, client, chosen=()):
        self.queryset = ClientSite.objects.filter(
            models.Q(client=client) & (models.Q(is_active=True) | models.Q(pk__in=chosen))
        ).order_by("name", "pk")

    def label_from_instance(self, site):
        label = f"{site.name} ({site.code})" if site.code else site.name
        return label if site.is_active else f"{label}, inactive"


class ContactForm(StyledForm, forms.ModelForm):
    role = ListOrOtherField(
        CONTACT_ROLES,
        max_length=80,
        required=False,
        help_text="What the laboratory contacts this person about.",
    )
    responsibilities = forms.MultipleChoiceField(
        choices=Responsibility.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="What this person deals with. Responsibilities route work; they do not "
        "record authority to receive results.",
    )
    sites = SiteChoices(
        label="Sites served",
        help_text="Leave all clear when the contact serves the whole organization.",
    )

    class Meta:
        model = ClientContact
        fields = ["name", "role", "email", "phone", "responsibilities"]
        widgets = {"phone": TelInput()}

    def __init__(self, *args, client, **kwargs):
        super().__init__(*args, **kwargs)
        chosen = list(self.instance.sites.values_list("pk", flat=True)) if self.instance.pk else []
        self.fields["sites"].limit_to(client, chosen)
        if self.instance.pk and not self.is_bound:
            self.initial["sites"] = chosen
        if not self.fields["sites"].queryset.exists():
            del self.fields["sites"]


class ContactEditForm(ContactForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    class Meta(ContactForm.Meta):
        fields = [*ContactForm.Meta.fields, "is_active"]
        labels = {"is_active": "Active"}


class SiteForm(GroupedForm, StyledForm, forms.ModelForm):
    groups = (
        (
            "Site",
            (("name", "col-md-6"), ("code", "col-md-3"), ("kind", "col-md-3")),
        ),
        (
            "Address and phone",
            (
                ("address_line1", "col-md-7"),
                ("address_line2", "col-md-5"),
                ("city", "col-md-5"),
                ("state", "col-md-4"),
                ("postal_code", "col-md-3"),
                ("phone", "col-md-5"),
            ),
        ),
        ("Instructions", (("instructions", "col-12"),)),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = ClientSite
        fields = [
            "name",
            "code",
            "kind",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "phone",
            "instructions",
        ]
        labels = {"name": "Site name", "instructions": "Internal instructions"}
        widgets = {
            "phone": TelInput(),
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "code": forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
        }
        help_texts = {
            "instructions": "For laboratory staff: access, opening hours, where to leave kits. "
            "Not shown to the client.",
        }


class SiteEditForm(SiteForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    class Meta(SiteForm.Meta):
        fields = [*SiteForm.Meta.fields, "is_active"]
        labels = {**SiteForm.Meta.labels, "is_active": "Active"}
        help_texts = {
            **SiteForm.Meta.help_texts,
            "is_active": "Clear when the organization no longer works from this site. Its "
            "tickets and history are kept; it is no longer offered for new tickets.",
        }


class ClientFilter(ActiveFilter):
    organization_type = forms.ChoiceField(
        required=False,
        choices=[("", "Any type"), *OrganizationType.choices],
        label="Organization type",
    )
    state = forms.ChoiceField(required=False, choices=[("", "Any state"), *US_STATES])
    contacts = forms.ChoiceField(
        required=False,
        choices=[("", "Any contacts"), ("none", "No active contact")],
        label="Contacts",
    )


SCOPE_CHOICES = (
    ("organization", "The whole organization"),
    ("sites", "Listed sites only"),
)


class PortalAccessForm(StyledForm, forms.Form):
    """Linked contact and scope of one portal account (clients.services.set_portal_access)."""

    contact = forms.ModelChoiceField(
        queryset=ClientContact.objects.none(),
        required=False,
        empty_label="No linked contact",
        label="Linked contact",
        help_text="The contact this account belongs to. The account is told about tickets "
        "raised for this contact when it can see them.",
    )
    scope = forms.ChoiceField(
        choices=SCOPE_CHOICES,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
        label="Tickets this account sees",
        help_text="Besides these, an account always sees the tickets it raised itself.",
    )
    sites = SiteChoices(
        label="Listed sites",
        help_text="With Listed sites only: the sites whose tickets this account sees.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, account, access=None, **kwargs):
        super().__init__(*args, **kwargs)
        contacts = ClientContact.objects.filter(client_id=account.client_id)
        current = access.contact_id if access and access.client_id == account.client_id else None
        self.fields["contact"].queryset = contacts.filter(
            models.Q(is_active=True) | models.Q(pk=current)
        ).order_by("name", "pk")
        self.fields["contact"].label_from_instance = lambda contact: contact.name
        chosen = (
            list(access.sites.filter(client_id=account.client_id).values_list("pk", flat=True))
            if access
            else []
        )
        self.fields["sites"].limit_to(account.client, chosen)
        if not self.is_bound:
            self.initial.setdefault("sites", chosen)
        if access is not None:
            self.fields["expected_revision"].required = True  # editing a grant: never blind
            self.fields["reason"] = forms.CharField(
                label="Reason for change",
                help_text=REASON_HELP,
                widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
            )

    def clean(self):
        data = super().clean()
        data["whole_organization"] = data.get("scope") == "organization"
        if data["whole_organization"]:
            data["sites"] = []
        elif "scope" in data and not data.get("sites"):
            self.add_error("sites", "Choose at least one site, or the whole organization.")
        return data


class TimelineFilter(StyledForm, forms.Form):
    """Filters of the per-client timeline (clients.timeline)."""

    kind = forms.ChoiceField(required=False, label="Kind")
    site = forms.ModelChoiceField(
        queryset=ClientSite.objects.none(), required=False, empty_label="All sites"
    )

    def __init__(self, *args, client, **kwargs):
        from .timeline import KINDS

        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = [("", "All kinds"), *KINDS]
        self.fields["site"].queryset = client.sites.order_by("-is_active", "name", "pk")
