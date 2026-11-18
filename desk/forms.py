"""Forms of the staff request pages. Validation of the rules themselves is in desk.services; these
forms collect and shape input. The team and the person are one choice ("Assign to", grouped by
team), so a person is only ever offered with a team they belong to."""

from django import forms
from django.urls import reverse_lazy

from accounts.models import User
from attachments.forms import AttachmentsField
from clients.models import Client, ClientContact, ClientSite
from core.forms import REASON_HELP, GroupedForm, StyledForm
from core.lookups import RemoteSelect
from taxonomy.choices import Priority
from taxonomy.models import RequestCategory
from taxonomy.services import offered_categories
from teams.models import Team, TeamMembership

from .models import (
    Channel,
    ComplaintDecision,
    ContactChannel,
    ContactDirection,
    ContactOutcome,
    EntryKind,
    Status,
    ViewSharing,
)

STAFF_CHANNELS = [choice for choice in Channel.choices if choice[0] != Channel.PORTAL]


def person_name(user):
    return user.get_full_name() or user.get_username()


def assign_choices(empty_label=None):
    """Grouped choices "<team id>:" (the team, nobody yet) and "<team id>:<user id>"."""
    from accounts.roles import ROLES
    from jobs.actor import system_account_id

    members = {}
    for membership in (
        TeamMembership.objects.filter(
            team__is_active=True,
            user__is_active=True,
            user__client__isnull=True,
            # Only staff who can open the team's tickets: a member without a role cannot.
            user__groups__name__in=ROLES,
        )
        .exclude(user_id=system_account_id())
        .select_related("user")
        .distinct()
    ):
        members.setdefault(membership.team_id, []).append(membership.user)
    choices = [("", empty_label)] if empty_label else []
    for team in Team.objects.filter(is_active=True):
        people = sorted(members.get(team.pk, []), key=lambda u: person_name(u).lower())
        group = [(f"{team.pk}:", f"{team.name} (nobody yet)")]
        # The team in the label too: a person in two teams must be told apart once chosen.
        group += [(f"{team.pk}:{user.pk}", f"{person_name(user)} ({team.name})") for user in people]
        choices.append((team.name, group))
    return choices


def parse_assignment(value):
    """(team, assignee or None) from an "Assign to" value; ValidationError if unknown."""
    try:
        team_id, _, user_id = value.partition(":")
        team = Team.objects.get(pk=int(team_id))
        assignee = User.objects.get(pk=int(user_id)) if user_id else None
    except ValueError, Team.DoesNotExist, User.DoesNotExist:
        raise forms.ValidationError("Choose a team or a person.") from None
    return team, assignee


def category_choices():
    """Categories a staff request may take (taxonomy.services.offered_categories), under their
    groups."""
    groups = {}
    for category in offered_categories():
        groups.setdefault(category.parent.name, []).append((category.pk, category.name))
    return [("", "Choose a category"), *groups.items()]


class CategoryField(forms.TypedChoiceField):
    """An active category (not a group), offered under its group."""

    def __init__(self, **kwargs):
        kwargs.setdefault("label", "Category")
        super().__init__(coerce=self._category, **kwargs)
        self.choices = category_choices

    @staticmethod
    def _category(value):
        try:
            return RequestCategory.objects.get(pk=int(value), parent__isnull=False)
        except ValueError, RequestCategory.DoesNotExist:
            raise forms.ValidationError("Choose a category.") from None

    def valid_value(self, value):
        return True  # the coercion checks it against the database


class RequestCreateForm(GroupedForm, StyledForm, forms.Form):
    groups = (
        ("What it is about", (("category", "col-md-8"), ("priority", "col-md-4"))),
        (
            "Who it is from",
            (
                ("client", "col-md-6"),
                ("site", "col-md-6"),
                ("contact", "col-md-6"),
                ("reported_by", "col-md-6"),
                ("channel", "col-md-6"),
                ("client_reference", "col-md-6"),
            ),
        ),
        (
            "The ticket",
            (
                ("subject", "col-12"),
                ("first_entry_kind", "col-12"),
                ("body", "col-12"),
                ("files", "col-12"),
            ),
        ),
        ("Routing", (("assign_to", "col-12"),)),
    )

    category = CategoryField()
    priority = forms.ChoiceField(
        required=False,
        choices=[("", "The category's priority"), *Priority.choices],
    )
    client = forms.ModelChoiceField(
        required=False,
        queryset=Client.objects.filter(is_active=True),
        widget=RemoteSelect("active-clients"),
        label="Organization",
        empty_label="No organization",
        help_text="Leave empty for an anonymous caller or a problem found inside the laboratory.",
    )
    site = forms.ModelChoiceField(
        required=False,
        queryset=ClientSite.objects.filter(is_active=True).select_related("client"),
        widget=RemoteSelect("desk-sites"),
        help_text="Search by site or organization name. Choosing it fills in the organization.",
    )
    contact = forms.ModelChoiceField(
        required=False,
        queryset=ClientContact.objects.filter(is_active=True).select_related("client"),
        widget=RemoteSelect("desk-contacts"),
        help_text="The person who raised it. Choosing them fills in the organization.",
    )
    reported_by = forms.CharField(
        required=False,
        max_length=160,
        label="Reported by",
        help_text="An unknown caller's name and role, when there is no contact.",
    )
    channel = forms.ChoiceField(choices=STAFF_CHANNELS, initial=Channel.PHONE)
    client_reference = forms.CharField(
        required=False,
        max_length=80,
        label="Reference given",
        help_text="An order, accession, sample, report or invoice number the caller gave.",
    )
    subject = forms.CharField(max_length=120)
    first_entry_kind = forms.ChoiceField(
        label="The first entry is",
        choices=[
            (EntryKind.NOTE, "An internal note (staff only)"),
            (EntryKind.REPLY, "A reply the client sees in the portal"),
        ],
        initial=EntryKind.NOTE,
        widget=forms.RadioSelect,
    )
    body = forms.CharField(
        label="What the client asked",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Use an order, accession or sample number; never a patient's name or date of "
        "birth.",
    )
    files = AttachmentsField(audience="staff")
    assign_to = forms.ChoiceField(
        required=False,
        label="Assign to",
        help_text="Empty: the category's team, or the default team.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assign_to"].choices = assign_choices("The category's team")
        # Choosing a category re-renders the section that follows it (desk/_category_section.html):
        # its guidance and the request type's own fields. On the select itself, so a search box
        # inside the enhanced control cannot trigger it.
        self.fields["category"].widget.attrs.update(
            {
                "hx-get": reverse_lazy("desk:category-guidance"),
                "hx-trigger": "change",
                "hx-target": "#category-section",
                "hx-swap": "innerHTML",
                # The organization, when chosen first: the request forms offered to it.
                "hx-include": "[name='client']",
            }
        )

    def clean(self):
        data = super().clean()
        client = data.get("client")
        for name in ("site", "contact"):
            record = data.get(name)
            if record is None:
                continue
            if client is None:
                client = data["client"] = record.client
            elif record.client_id != client.pk:
                self.add_error(name, "Choose one of the organization's own.")
        if data.get("assign_to"):
            try:
                data["team"], data["assignee"] = parse_assignment(data["assign_to"])
            except forms.ValidationError as exc:
                self.add_error("assign_to", exc)
        return data


class RevisionForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)


class EntryForm(RevisionForm):
    """The composer: a reply, an internal note or a logged contact, and an optional status."""

    kind = forms.ChoiceField(
        choices=[
            (EntryKind.REPLY, "Reply"),
            (EntryKind.NOTE, "Note"),
            (EntryKind.CONTACT, "Log contact"),
        ],
        widget=forms.RadioSelect,
    )
    body = forms.CharField(label="Message", widget=forms.Textarea(attrs={"rows": 4}))
    contact_direction = forms.ChoiceField(
        required=False, choices=[("", "Choose")] + ContactDirection.choices, label="Direction"
    )
    contact_channel = forms.ChoiceField(
        required=False, choices=[("", "Choose")] + ContactChannel.choices, label="By"
    )
    contact_with = forms.CharField(required=False, max_length=160, label="With whom")
    contact_outcome = forms.ChoiceField(
        required=False, choices=[("", "Choose")] + ContactOutcome.choices, label="Outcome"
    )
    files = AttachmentsField(audience="staff")
    new_status = forms.ChoiceField(
        required=False,
        label="Then set status",
        choices=[
            ("", "Keep the status"),
            (Status.OPEN, Status.OPEN.label),
            (Status.WAITING_CLIENT, Status.WAITING_CLIENT.label),
            (Status.WAITING_THIRD_PARTY, Status.WAITING_THIRD_PARTY.label),
        ],
    )

    def __init__(self, *args, closed=False, **kwargs):
        super().__init__(*args, **kwargs)
        if closed:
            self.fields["kind"].choices = [(EntryKind.NOTE, "Note")]

    def clean(self):
        data = super().clean()
        if data.get("files") and data.get("kind") == EntryKind.CONTACT:
            self.add_error(
                "files",
                "Files go with a reply or a note, not a logged contact. Choose the files again.",
            )
        return data

    def contact(self):
        data = self.cleaned_data
        if data["kind"] != EntryKind.CONTACT:
            return None
        return {
            "direction": data["contact_direction"],
            "channel": data["contact_channel"],
            "with_whom": data["contact_with"],
            "outcome": data["contact_outcome"],
        }


class AssignForm(RevisionForm):
    assign_to = forms.ChoiceField(label="Assign to")
    reason = forms.CharField(
        required=False,
        label="Reason (optional)",
        help_text=REASON_HELP,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assign_to"].choices = assign_choices()

    def clean_assign_to(self):
        return parse_assignment(self.cleaned_data["assign_to"])


def status_choices(current):
    """The statuses a request in `current` may move to."""
    if current == Status.CLOSED:
        return []
    if current == Status.RESOLVED:
        return [
            (Status.CLOSED, "Closed"),
            (Status.OPEN, "Open (reopen)"),
            (Status.WAITING_CLIENT, "Waiting on client (reopen)"),
            (Status.WAITING_THIRD_PARTY, "Waiting on third party (reopen)"),
        ]
    return [(value, label) for value, label in Status.choices if value not in (current, "closed")]


class StatusForm(RevisionForm):
    status = forms.ChoiceField(label="New status", widget=forms.RadioSelect)
    summary = forms.CharField(
        required=False,
        label="Resolution summary",
        help_text="Required to resolve: what was done. Internal; the client does not see it.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    reason = forms.CharField(
        required=False,
        label="Reason",
        help_text="Required to reopen a resolved ticket. " + REASON_HELP,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, current, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = status_choices(current)


class PropertiesForm(RevisionForm):
    category = CategoryField()
    priority = forms.ChoiceField(choices=Priority.choices)
    reason = forms.CharField(
        required=False,
        label="Reason (optional)",
        help_text=REASON_HELP,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ReasonForm(RevisionForm):
    reason = forms.CharField(
        label="Reason", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )


class WithdrawForm(RevisionForm):
    reason = forms.CharField(
        label="Why withdraw this reply",
        help_text="The client no longer sees the reply; staff still do, with this reason.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ComplaintDecisionForm(RevisionForm):
    decision = forms.ChoiceField(
        choices=ComplaintDecision.choices, widget=forms.RadioSelect, label="Decision"
    )
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class ComplaintRecordForm(RevisionForm):
    findings = forms.CharField(
        required=False, label="Findings and actions", widget=forms.Textarea(attrs={"rows": 4})
    )
    corrective_action_reference = forms.CharField(
        required=False,
        max_length=120,
        label="Corrective action reference",
        help_text="Its number in the laboratory's quality system.",
    )
    outcome_communicated_on = forms.DateField(
        required=False,
        label="Outcome communicated on",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    outcome_communicated_how = forms.CharField(
        required=False, max_length=120, label="How it was communicated"
    )


class ComplaintReviewForm(RevisionForm):
    note = forms.CharField(
        label="Review note",
        help_text="What you checked and your conclusion.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )


BULK_ACTIONS = [("assign", "Assign"), ("status", "Set status"), ("close", "Close")]


class BulkForm(StyledForm, forms.Form):
    action = forms.ChoiceField(choices=BULK_ACTIONS, label="With the selected")
    assign_to = forms.ChoiceField(required=False, label="Assign to")
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[
            ("", "Choose"),
            (Status.OPEN, Status.OPEN.label),
            (Status.WAITING_CLIENT, Status.WAITING_CLIENT.label),
            (Status.WAITING_THIRD_PARTY, Status.WAITING_THIRD_PARTY.label),
        ],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assign_to"].choices = assign_choices("Choose")

    def clean(self):
        data = super().clean()
        if data.get("action") == "assign":
            if not data.get("assign_to"):
                self.add_error("assign_to", "Choose a team or a person.")
            else:
                try:
                    data["team"], data["assignee"] = parse_assignment(data["assign_to"])
                except forms.ValidationError as exc:
                    self.add_error("assign_to", exc)
        if data.get("action") == "status" and not data.get("status"):
            self.add_error("status", "Choose a status.")
        return data


class SavedViewForm(StyledForm, forms.Form):
    name = forms.CharField(max_length=80, label="View name")
    shared_with = forms.ChoiceField(
        choices=ViewSharing.choices, initial=ViewSharing.PRIVATE, label="Who sees it"
    )
    team = forms.ModelChoiceField(
        required=False,
        queryset=Team.objects.filter(is_active=True),
        empty_label="Choose a team",
        help_text="For a view shared with a team.",
    )
