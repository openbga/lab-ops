"""The filters of the Access review lists and the unused account period's form
(audit_review.access_report)."""

from django import forms
from django.contrib.auth import get_user_model

from accounts.roles import CAPABILITIES, ROLE_LABELS, ROLES
from clients.models import Client
from core.forms import StyledForm
from core.lookups import RemoteSelect
from teams.models import Team

from . import access_report
from .forms import PersonField

KINDS = [
    ("", "Any kind"),
    (access_report.KIND_STAFF, "Staff"),
    (access_report.KIND_PORTAL, "Client portal"),
]
ROLE_CHOICES = [("", "Any role"), *((role, ROLE_LABELS[role]) for role in ROLES)]
CAPABILITY_CHOICES = [("", "Any capability"), *CAPABILITIES.items()]


class ListFilter(StyledForm, forms.Form):
    INLINE = ()

    def recorded(self):
        """The accepted filters as a use records them: ids and codes as strings, never text as
        typed. Only for a valid form."""
        recorded = {}
        for name, value in self.cleaned_data.items():
            if value in (None, ""):
                continue
            recorded[name] = str(value.pk) if hasattr(value, "pk") else str(value)
        return recorded

    def inline(self):
        return [name for name in self.INLINE if name in self.fields]

    def more(self):
        return [name for name in self.fields if name not in self.INLINE]


class AccountFilter(ListFilter):
    """The Accounts tab's filters (access_report.accounts)."""

    kind = forms.ChoiceField(required=False, choices=KINDS)
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status"), ("active", "Active"), ("deactivated", "Deactivated")],
    )
    role = forms.ChoiceField(required=False, choices=ROLE_CHOICES)
    capability = forms.ChoiceField(
        required=False,
        choices=CAPABILITY_CHOICES,
        help_text="Granted to the account or through one of its groups.",
    )
    person = PersonField(
        required=False,
        queryset=get_user_model().objects.all(),
        widget=RemoteSelect("review-accounts"),
        empty_label="Anyone",
    )
    team = forms.ModelChoiceField(
        required=False, queryset=Team.objects.order_by("name", "pk"), empty_label="Any team"
    )
    organization = forms.ModelChoiceField(
        required=False,
        queryset=Client.objects.all(),
        widget=RemoteSelect("clients"),
        label="Client organization",
        empty_label="Any organization",
        help_text="Its client portal accounts.",
    )
    console = forms.ChoiceField(
        required=False,
        label="Admin console",
        choices=[
            ("", "Any"),
            ("console", "Can open the admin console"),
            ("superuser", "Superuser"),
        ],
    )
    second_factor = forms.ChoiceField(
        required=False,
        label="Second factor",
        choices=[
            ("", "Any"),
            ("set_up", "Set up"),
            ("none", "Not set up"),
            ("required_missing", "Required by the rule and not set up"),
        ],
    )

    INLINE = ("kind", "status", "role")


class UnusedFilter(ListFilter):
    """The Unused accounts tab's filters (access_report.unused_accounts)."""

    kind = forms.ChoiceField(required=False, choices=KINDS)
    role = forms.ChoiceField(required=False, choices=ROLE_CHOICES)
    capability = forms.ChoiceField(
        required=False,
        choices=CAPABILITY_CHOICES,
        help_text="Granted to the account or through one of its groups.",
    )

    INLINE = ("kind", "role", "capability")


class PeriodForm(StyledForm, forms.Form):
    """Set after how many days without a sign-in an active account is unused
    (access_report.set_period), with a reason. `expected_revision` is the period's revision the
    form showed."""

    days = forms.IntegerField(
        label="Unused after (days)",
        required=False,
        min_value=1,
        max_value=access_report.MOST_DAYS,
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
        help_text="An active account is listed as unused when it has not signed in for this many "
        "days. Leave it empty to list none.",
    )
    reason = forms.CharField(
        label="Reason",
        max_length=access_report.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Kept with the change and shown under Changes to the period on this form.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
