"""Forms of the nonconformance pages. They collect and shape input; every rule on a change
(who may make it, required values, dates, the record's state) is checked again in
nonconformance.services, whose refusals the views show on these forms."""

from django import forms

from core.forms import REASON_HELP, SearchFilter, StaffChoiceField, StyledForm

from .models import (
    ACTIVE_STATUSES,
    ActionStatus,
    ClientNotice,
    Effectiveness,
    Impact,
    Severity,
    Source,
    Status,
)

NO_PATIENT_DATA = (
    "Refer to an order, sample or ticket number. Do not enter patient names or identifiers."
)
STATUS_LABELS = dict(Status.choices)


def _text(label, rows=3, required=True, help_text="", max_length=4000):
    return forms.CharField(
        label=label,
        required=required,
        max_length=max_length,
        help_text=help_text,
        widget=forms.Textarea(attrs={"rows": rows}),
    )


def _optional_reason():
    return _text("Reason (optional)", rows=2, required=False, help_text=REASON_HELP)


def _reason(label="Reason", help_text=REASON_HELP):
    return _text(label, rows=2, help_text=help_text)


class RevisionForm(StyledForm, forms.Form):
    """Every change carries the record's revision the page showed (a stale save is refused)."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput)


# ------------------------------------------------------------------------------ list filters

SHOW_OPEN, SHOW_MINE, SHOW_CLOSED, SHOW_ALL, SHOW_OVERDUE = "", "mine", "closed", "any", "overdue"


class NonconformanceFilter(SearchFilter):
    show = forms.ChoiceField(
        required=False,
        label="Show",
        choices=[
            (SHOW_OPEN, "Open"),
            (SHOW_MINE, "Mine"),
            (SHOW_CLOSED, "Closed"),
            (SHOW_ALL, "All"),
        ],
    )
    status = forms.ChoiceField(required=False, choices=[("", "Any status"), *Status.choices])
    severity = forms.ChoiceField(required=False, choices=[("", "Any severity"), *Severity.choices])
    source = forms.ChoiceField(required=False, choices=[("", "Any source"), *Source.choices])


class ActionFilter(SearchFilter):
    show = forms.ChoiceField(
        required=False,
        label="Show",
        choices=[
            (SHOW_OPEN, "Open"),
            (SHOW_MINE, "Mine"),
            (SHOW_OVERDUE, "Overdue"),
            (SHOW_ALL, "All"),
        ],
    )
    status = forms.ChoiceField(required=False, choices=[("", "Any status"), *ActionStatus.choices])


# ------------------------------------------------------------------------------ the record


class DetailsFields(StyledForm, forms.Form):
    title = forms.CharField(max_length=160)
    description = _text("What happened", rows=5, help_text="What was found, where and when.")
    source = forms.ChoiceField(choices=[("", "Choose where it came from"), *Source.choices])
    severity = forms.ChoiceField(
        choices=[("", "Choose a severity"), *Severity.choices],
        help_text="The laboratory's own grading; its procedures say what each level means.",
    )
    area = forms.CharField(
        required=False,
        max_length=80,
        label="Area or section",
        help_text="Where in the laboratory it happened, in the laboratory's own terms.",
    )


class NonconformanceCreateForm(DetailsFields):
    containment = _text(
        "Containment",
        required=False,
        help_text="What was done at once to limit the problem.",
    )
    owner = StaffChoiceField(
        required=False,
        label="Owner",
        empty_label="No owner yet",
        help_text="The person who works the record. Leave it empty to choose later.",
    )

    def __init__(self, *args, can_choose_owner=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_choose_owner:
            del self.fields["owner"]


class DetailsForm(DetailsFields, RevisionForm):
    reason = _optional_reason()

    def __init__(self, *args, from_complaint=False, **kwargs):
        super().__init__(*args, **kwargs)
        if from_complaint:
            # A record opened from a complaint keeps that source (services.update_details).
            self.fields["source"].choices = [(Source.COMPLAINT, Source.COMPLAINT.label)]
            self.fields["source"].help_text = "Opened from a complaint: the source stays Complaint."


class OwnerForm(RevisionForm):
    owner = StaffChoiceField(label="Owner", empty_label="Choose a person")
    reason = _optional_reason()


class StatusForm(RevisionForm):
    status = forms.ChoiceField(label="New status", widget=forms.RadioSelect)
    reason = _optional_reason()

    def __init__(self, *args, current, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [
            (value, STATUS_LABELS[value]) for value in ACTIVE_STATUSES if value != current
        ]
        if current == Status.VERIFICATION:
            self.fields[
                "status"
            ].help_text = (
                "Leaving Verification clears the effectiveness check; the history keeps it."
            )


class InvestigationForm(RevisionForm):
    containment = _text(
        "Containment",
        required=False,
        help_text="What was done at once to limit the problem.",
    )
    root_cause = _text("Root cause", required=False, help_text="Why it happened.")
    similar_cases = _text(
        "Similar cases",
        required=False,
        help_text="Whether similar nonconformities exist or could occur, and what was checked.",
    )


class ImpactForm(RevisionForm):
    impact = forms.ChoiceField(
        label="Decision on reported results", choices=Impact.choices, widget=forms.RadioSelect
    )
    impact_assessment = _text(
        "What was reviewed",
        rows=4,
        help_text="The orders and reports reviewed and the conclusion.",
    )
    client_notice = forms.ChoiceField(
        required=False,
        label="Client told",
        choices=[("", "Not recorded"), *ClientNotice.choices],
        widget=forms.RadioSelect,
        help_text="Required when reported results are affected.",
    )
    client_told_on = forms.DateField(
        required=False,
        label="Told on",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    client_told_how = forms.CharField(
        required=False,
        max_length=160,
        label="How",
        help_text="For example a phone call to the practice manager.",
    )
    client_told_by = StaffChoiceField(
        required=False, label="Told by", empty_label="Choose a person"
    )


class ActionForm(RevisionForm):
    description = _text("Action", max_length=2000, help_text="What is to be done.")
    assignee = StaffChoiceField(label="Person", empty_label="Choose a person")
    due_on = forms.DateField(
        label="Due date",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Today or later.",
    )
    reason = _reason()

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not editing:
            del self.fields["reason"]


class FinishActionForm(RevisionForm):
    note = _text("What was done", max_length=2000)

    def __init__(self, *args, done=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not done:
            self.fields["note"].label = "Reason"
            self.fields["note"].help_text = REASON_HELP


class LinkTicketForm(RevisionForm):
    ticket = forms.CharField(
        label="Ticket number",
        max_length=40,
        help_text="For example REQ-000123.",
    )


class ReasonForm(RevisionForm):
    reason = _reason()


class EffectivenessForm(RevisionForm):
    result = forms.ChoiceField(
        label="Result", choices=Effectiveness.choices, widget=forms.RadioSelect
    )
    note = _text("What was checked", help_text="What was checked and what it showed.")


class CloseForm(RevisionForm):
    note = _text("Closing note", help_text="Why the record can close.")


class OpenFromComplaintForm(RevisionForm):
    title = forms.CharField(max_length=160)
    description = _text("What happened", rows=5, help_text="What was found, where and when.")
    severity = forms.ChoiceField(
        choices=[("", "Choose a severity"), *Severity.choices],
        help_text="The laboratory's own grading; its procedures say what each level means.",
    )
    area = forms.CharField(
        required=False,
        max_length=80,
        label="Area or section",
        help_text="Where in the laboratory it happened, in the laboratory's own terms.",
    )
    containment = _text(
        "Containment",
        required=False,
        help_text="What was done at once to limit the problem.",
    )
