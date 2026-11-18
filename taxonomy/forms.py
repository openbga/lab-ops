"""Forms for request category setup (T1) and preset loading (T2)."""

from django import forms
from django.utils.text import slugify

from core.forms import REASON_HELP, ActiveFilter, GroupedForm, StyledForm
from teams.models import Team

from . import presets as catalogue
from .models import RequestCategory

KEY_HELP = (
    "A stable code that never changes after creation, used by presets and reports. "
    "Leave empty to make one from the name."
)


class _KeyMixin:
    def clean_key(self):
        key = (self.cleaned_data.get("key") or "").strip()
        if not key:
            key = slugify(self.data.get(self.add_prefix("name"), ""))[:60]
        if not key:
            raise forms.ValidationError("Give a key, or a name it can be made from.")
        return key


class GroupForm(_KeyMixin, GroupedForm, StyledForm, forms.ModelForm):
    key = forms.SlugField(max_length=60, required=False, help_text=KEY_HELP)
    groups = (
        ("Group", (("name", "col-md-8"), ("key", "col-md-4"), ("client_label", "col-12"))),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = RequestCategory
        fields = ["name", "client_label"]
        labels = {"name": "Group name", "client_label": "Client label"}


class CategoryForm(_KeyMixin, GroupedForm, StyledForm, forms.ModelForm):
    key = forms.SlugField(max_length=60, required=False, help_text=KEY_HELP)
    groups = (
        (
            "Category",
            (
                ("parent", "col-md-6"),
                ("name", "col-md-6"),
                ("key", "col-md-6"),
                ("client_label", "col-md-6"),
                ("guidance", "col-12"),
            ),
        ),
        ("Routing", (("default_team", "col-md-6"), ("default_priority", "col-md-6"))),
        (
            "Rules",
            (
                ("client_may_raise", "col-12"),
                ("is_complaint", "col-12"),
                ("is_restricted", "col-12"),
            ),
        ),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = RequestCategory
        fields = [
            "parent",
            "name",
            "client_label",
            "guidance",
            "default_team",
            "default_priority",
            "client_may_raise",
            "is_complaint",
            "is_restricted",
        ]
        labels = {
            "parent": "Group",
            "name": "Category name",
            "client_label": "Client label",
            "default_team": "Default team",
            "default_priority": "Default priority",
            "client_may_raise": "Clients may raise it in the portal",
            "is_complaint": "Complaint: tickets follow the complaint rules",
            "is_restricted": "Restricted: visible only to the team's members and supervisors",
        }
        help_texts = {
            "default_team": "Proposed for new tickets. Empty: the default team.",
            "guidance": "Shown to staff when they log a ticket in this category.",
        }
        widgets = {"guidance": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        groups = RequestCategory.objects.filter(parent__isnull=True)
        teams = Team.objects.all()
        if instance.pk:
            groups = groups.filter(is_active=True) | groups.filter(pk=instance.parent_id)
            teams = teams.filter(is_active=True) | teams.filter(pk=instance.default_team_id)
        else:
            groups, teams = groups.filter(is_active=True), teams.filter(is_active=True)
        self.fields["parent"].queryset = groups.order_by("position", "name")
        self.fields["parent"].required = True
        self.fields["parent"].empty_label = None
        self.fields["default_team"].queryset = teams.order_by("name")
        self.fields["default_team"].empty_label = "The default team"


class _EditMixin(forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Clear to stop offering it for new tickets. Existing tickets keep it.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("key", None)  # the key never changes
        self.fields["is_active"].initial = self.instance.is_active


class GroupEditForm(_EditMixin, GroupForm):
    pass


class CategoryEditForm(_EditMixin, CategoryForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].help_text = (
            "Kept with the change in the record's history. Required for every change, and "
            "especially for the complaint and restricted rules."
        )


class CategoryFilter(ActiveFilter):
    pass


PRESET_CHOICES = [(key, preset.name) for key, preset in catalogue.PRESETS.items() if key != "all"]


class PresetForm(StyledForm, forms.Form):
    presets = forms.MultipleChoiceField(
        label="Laboratory types",
        required=False,
        choices=PRESET_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="“All laboratories” is always loaded with them. Loading again adds only what "
        "is missing and never changes a category you have edited.",
    )
