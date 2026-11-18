"""Forms for external systems (L1) and record links (L2 to L5)."""

from django import forms
from django.utils.text import slugify

from core.forms import REASON_HELP, ActiveFilter, GroupedForm, StyledForm

from .masking import PERSON_RECORD_TYPES
from .models import (
    NON_PERSON_TYPES,
    PERSON_TYPES,
    ExternalRecordLink,
    ExternalSystem,
    LinkRole,
    PersonRemovalReason,
)


class SystemForm(GroupedForm, StyledForm, forms.ModelForm):
    key = forms.SlugField(
        max_length=40,
        required=False,
        help_text="A stable code that never changes. Leave empty to make one from the name.",
    )
    groups = (
        ("System", (("name", "col-md-8"), ("key", "col-md-4"), ("kind", "col-md-6"))),
        ("Opening records", (("base_url", "col-12"),)),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = ExternalSystem
        fields = ["name", "kind", "base_url"]
        labels = {"name": "System name"}
        widgets = {"base_url": forms.URLInput(attrs={"placeholder": "https://"})}
        help_texts = {
            "base_url": "Optional. https only. When set, every link's address must be on this "
            "host. The application never opens or reads it itself."
        }

    def clean_key(self):
        key = (self.cleaned_data.get("key") or "").strip() or slugify(
            self.data.get(self.add_prefix("name"), "")
        )[:40]
        if not key:
            raise forms.ValidationError("Give a key, or a name it can be made from.")
        return key


class SystemEditForm(SystemForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="An inactive system takes no new links. Existing links stay.",
    )
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("key")
        self.fields["is_active"].initial = self.instance.is_active


class SystemFilter(ActiveFilter):
    pass


PERSON_WARNING = (
    "Person-level: type the source system's key only. Never a name, date of birth or contact "
    "detail. The label is masked; only staff with the patient information permission see it."
)


class LinkForm(GroupedForm, StyledForm, forms.Form):
    """Add a link to a request or an organization. Person-level record types are offered only
    to holders of the capability, and only on a request."""

    system = forms.ModelChoiceField(queryset=ExternalSystem.objects.none(), empty_label=None)
    record_type = forms.ChoiceField(label="Record type")
    identifier = forms.CharField(
        max_length=200,
        help_text="As the source system writes it.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    identifier_scheme = forms.CharField(
        label="Identifier scheme",
        max_length=40,
        required=False,
        help_text="Optional, when the system has several kinds of number (accession, placer "
        "order).",
    )
    display_label = forms.CharField(
        label="Label",
        max_length=120,
        required=False,
        help_text="What staff see. Empty: the identifier. For a patient, participant or donor: "
        "the last four characters of a key of eight or more, or an alias you type.",
    )
    role = forms.ChoiceField(choices=LinkRole.choices, initial=LinkRole.RELATED)
    url = forms.CharField(
        label="Address",
        max_length=500,
        required=False,
        widget=forms.URLInput(attrs={"placeholder": "https://"}),
        help_text="Optional https link to the record, on the system's own host. Only for systems "
        "with a base address, and never for patient, participant or donor links.",
    )
    shown_to_client = forms.BooleanField(
        label="Show to client",
        required=False,
        help_text="The client sees the label and record type in the portal. Never for "
        "person-level links.",
    )
    groups = (
        ("Record", (("system", "col-md-6"), ("record_type", "col-md-6"))),
        (
            "Identifier",
            (
                ("identifier", "col-md-8"),
                ("identifier_scheme", "col-md-4"),
                ("display_label", "col-md-8"),
                ("role", "col-md-4"),
                ("url", "col-12"),
            ),
        ),
        ("Client", (("shown_to_client", "col-12"),)),
    )

    def __init__(self, *args, allow_person=False, for_request=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["system"].queryset = ExternalSystem.objects.filter(is_active=True)
        choices = list(NON_PERSON_TYPES)
        if allow_person and for_request:
            choices += [(value, f"{label} (person-level)") for value, label in PERSON_TYPES]
            self.person_warning = PERSON_WARNING
        else:
            self.person_warning = ""
        self.fields["record_type"].choices = choices
        if not for_request:
            self.fields.pop("shown_to_client")

    def clean(self):
        data = super().clean()
        if data.get("record_type") in PERSON_RECORD_TYPES:
            if data.get("shown_to_client"):
                self.add_error("shown_to_client", "Person-level links are never shown to clients.")
            if data.get("url"):
                self.add_error(
                    "url",
                    "Person-level links have no web address. Open the person in the source system.",
                )
        return data


PERSON_REASON_HELP = (
    "Chosen from a list so that no key can reach the history. Add details in the note if needed."
)


class LinkEditForm(GroupedForm, StyledForm, forms.ModelForm):
    """Correct a link's label or address. A person-level link has no address and takes a reason
    from a fixed list instead of free text."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )
    reason_code = forms.ChoiceField(
        label="Reason for change",
        choices=[("", "Choose a reason"), *PersonRemovalReason.choices],
        help_text="Chosen from a list so that no key can reach the history.",
    )
    groups = (
        ("Link", (("display_label", "col-12"), ("url", "col-12"))),
        ("Change record", (("reason", "col-12"), ("reason_code", "col-12"))),
    )

    class Meta:
        model = ExternalRecordLink
        fields = ["display_label", "url"]
        labels = {"display_label": "Label", "url": "Address"}
        widgets = {"url": forms.URLInput(attrs={"placeholder": "https://"})}
        help_texts = {"url": "Optional https link on the system's host."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.is_person:
            self.fields.pop("url")
            self.fields.pop("reason")
        else:
            self.fields.pop("reason_code")


class RemoveLinkForm(StyledForm, forms.Form):
    reason = forms.CharField(
        label="Reason for removal",
        help_text="Kept with the link's history. The link stays listed as removed.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    reason_code = forms.ChoiceField(
        label="Reason for removal",
        choices=[("", "Choose a reason"), *PersonRemovalReason.choices],
        help_text=PERSON_REASON_HELP,
    )
    note = forms.CharField(
        label="Note",
        required=False,
        max_length=500,
        help_text="Optional. Seen only by staff with the patient information permission; never "
        "in the history or the timeline.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, person=False, **kwargs):
        super().__init__(*args, **kwargs)
        if person:
            self.fields.pop("reason")
        else:
            self.fields.pop("reason_code")
            self.fields.pop("note")


class NoEchoInput(forms.TextInput):
    """A text box that never writes its value back into the page."""

    def format_value(self, value):
        return None


class PersonSearchForm(StyledForm, forms.Form):
    key = forms.CharField(
        label="Patient, participant or donor key",
        max_length=200,
        help_text="The source system's key, exactly as it writes it. The search is recorded.",
        widget=NoEchoInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
