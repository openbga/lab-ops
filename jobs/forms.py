"""The settings form of the Scheduled jobs page. jobs.services.update_settings validates the range
and the revision again; the form only reads the typed number."""

from django import forms

from core.forms import StyledForm
from core.ids import whole_number

CLOSE_AFTER_HELP = (
    "A resolved ticket is closed automatically this many days after it was resolved, from 1 to "
    "365. Leave it empty and resolved tickets stay resolved until someone closes them. A "
    "complaint closes only after its review."
)


class JobSettingsForm(StyledForm, forms.Form):
    close_resolved_after_days = forms.CharField(
        label="Close resolved tickets after",
        required=False,
        max_length=6,
        help_text=CLOSE_AFTER_HELP,
        widget=forms.NumberInput(attrs={"unit": "days", "inputmode": "numeric"}),
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)

    def clean_close_resolved_after_days(self):
        text = (self.cleaned_data.get("close_resolved_after_days") or "").strip()
        if not text:
            return None
        days = whole_number(text)
        if days is None:
            raise forms.ValidationError("Enter a whole number of days.")
        return days
