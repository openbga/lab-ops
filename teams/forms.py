from django import forms

from core.forms import REASON_HELP, GroupedForm, StaffChoiceField, StyledForm

from .models import Team


class TeamForm(GroupedForm, StyledForm, forms.ModelForm):
    groups = (
        ("Team", (("name", "col-md-6"), ("description", "col-12"))),
        ("Routing", (("is_default", "col-12"), ("is_active", "col-12"))),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = Team
        fields = ["name", "description", "is_default"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}
        labels = {"is_default": "Default team"}
        help_texts = {
            "is_default": "Receives tickets whose category names no team. Choosing it moves "
            "the default here."
        }

    def _get_validation_exclusions(self):
        """Every model rule except "one default team": choosing the default moves it here
        (teams.services), so another default team is not an error."""
        return super()._get_validation_exclusions() | {"is_default"}


class TeamEditForm(TeamForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    class Meta(TeamForm.Meta):
        fields = [*TeamForm.Meta.fields, "is_active"]
        labels = {**TeamForm.Meta.labels, "is_active": "Active"}
        help_texts = {
            "is_active": "Clear to stop routing new tickets here. Its tickets and history stay."
        }


class MemberForm(StyledForm, forms.Form):
    user = StaffChoiceField(label="Person", empty_label="Choose a person")
    is_lead = forms.BooleanField(required=False, label="Team lead")
