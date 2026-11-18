from django import forms
from django.core.exceptions import ValidationError

from core.forms import StyledForm

from .importing import MAX_BYTES, MAX_ROWS, decode_payload


class UploadForm(StyledForm, forms.Form):
    step = forms.CharField(widget=forms.HiddenInput, initial="preview")
    file = forms.FileField(
        label="CSV file (UTF-8)",
        help_text="Save the sheet as CSV UTF-8, comma separated, with the header row first. "
        f"Up to {MAX_BYTES // 1024} KiB and {MAX_ROWS} rows.",
    )


class PayloadField(forms.CharField):
    """The uploaded file as it travels from preview to import: base64 of the original bytes.
    Cleaned to bytes, never trimmed or re-encoded. Altering it only uploads another file: the
    import parses it again and checks it against the preview's state token."""

    def __init__(self, **kwargs):
        super().__init__(
            widget=forms.HiddenInput, strip=False, max_length=4 * ((MAX_BYTES + 2) // 3), **kwargs
        )

    def clean(self, value):
        value = super().clean(value)
        try:
            return decode_payload(value)
        except ValidationError as exc:
            raise ValidationError(exc.messages) from None


class CommitForm(StyledForm, forms.Form):
    step = forms.CharField(widget=forms.HiddenInput, initial="commit")
    filename = forms.CharField(widget=forms.HiddenInput, max_length=200)
    payload = PayloadField()
    state_token = forms.CharField(widget=forms.HiddenInput, max_length=64)
    reason = forms.CharField(
        required=False,
        max_length=2000,
        label="Reason for changing existing records",
        help_text="Required because this file changes existing records. Kept in each changed "
        "record's history together with the file name and its SHA-256.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )
