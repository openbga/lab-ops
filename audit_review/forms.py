"""The filters of the audit review workspace's lists (audit_review.sources.FILTERS)."""

from django import forms
from django.contrib.auth import get_user_model

from clients.models import Client
from core.forms import StyledForm
from core.ids import whole_number
from core.lookups import RemoteSelect
from desk.models import ServiceRequest

from .models import Source
from .sources import FILTERS, action_choices

ACTION_LABELS = {
    Source.CHANGES: ("Action", "Any action"),
    Source.ACCESS: ("What", "Any kind of read"),
    Source.SIGN_INS: ("Event", "Any event"),
    Source.USES: ("What", "Any use"),
}
# The filters shown in the toolbar's row; the rest are behind More filters.
INLINE = ("person", "organization", "action")


class PersonField(forms.ModelChoiceField):
    def label_from_instance(self, account):
        name = account.get_full_name()
        return f"{name} ({account.username})" if name else account.username


class ReviewFilter(StyledForm, forms.Form):
    person = PersonField(
        required=False,
        queryset=get_user_model().objects.all(),
        widget=RemoteSelect("review-accounts"),
        empty_label="Anyone",
    )
    organization = forms.ModelChoiceField(
        required=False,
        queryset=Client.objects.all(),
        widget=RemoteSelect("clients"),
        label="Client organization",
        empty_label="Any organization",
    )
    action = forms.ChoiceField(required=False)
    ticket = forms.CharField(
        required=False,
        max_length=24,
        label="Ticket number",
        help_text="As shown on the ticket, such as REQ-000123.",
    )
    date_from = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    date_to = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, source, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        for name in list(self.fields):
            if name not in FILTERS[source]:
                del self.fields[name]
        label, empty = ACTION_LABELS[source]
        self.fields["action"].label = label
        self.fields["action"].choices = [("", empty), *action_choices(source)]

    def clean_ticket(self):
        raw = self.cleaned_data.get("ticket", "").strip()
        if not raw:
            return None
        number = whole_number(raw.upper().removeprefix("REQ-").lstrip("0"))
        ticket = ServiceRequest.objects.filter(pk=number).first() if number else None
        if ticket is None:
            raise forms.ValidationError("No ticket has this number.")
        return ticket

    def clean(self):
        data = super().clean()
        start, end = data.get("date_from"), data.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "End date must be on or after the start date.")
        return data

    def recorded(self):
        """The accepted filters as a use records them: ids, codes and dates as strings, never
        text as typed. Only for a valid form."""
        recorded = {}
        for name, value in self.cleaned_data.items():
            if value in (None, ""):
                continue
            recorded[name] = str(value.pk) if hasattr(value, "pk") else str(value)
        return recorded

    def inline(self):
        return [name for name in INLINE if name in self.fields]

    def more(self):
        return [name for name in self.fields if name not in INLINE]
