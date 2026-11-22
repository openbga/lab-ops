"""Forms of the client portal's request pages (requirements P1 to P3). Choices are built from the
signed-in account's own scope (desk.portal_scope); desk.services checks every value again."""

from django import forms

from attachments.forms import AttachmentsField
from core.forms import SearchFilter, StyledForm

from .models import CLIENT_STATUS_LABELS, Status
from .portal_scope import portal_sites, site_required

# Client wording (desk.models.CLIENT_STATUS_LABELS) and the status tone for each status.
STATUS_TONES = {
    Status.OPEN: "progress",
    Status.WAITING_CLIENT: "warning",
    Status.WAITING_THIRD_PARTY: "hold",
    Status.RESOLVED: "success",
    Status.CLOSED: "neutral",
}
CLIENT_STATUS = {code: (CLIENT_STATUS_LABELS[code], tone) for code, tone in STATUS_TONES.items()}
OPEN_STATUSES = (Status.OPEN, Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY)
REPLYABLE = frozenset({*OPEN_STATUSES, Status.RESOLVED})


def client_status(code):
    """(label, tone) of a status in the client's words."""
    return CLIENT_STATUS.get(code, (str(code), "neutral"))


PATIENT_DATA_HELP = (
    "An order, accession or sample number helps the laboratory find it. Do not enter patient "
    "names or dates of birth."
)


def raisable_categories():
    from taxonomy.models import RequestCategory

    return (
        RequestCategory.objects.filter(
            is_active=True,
            client_may_raise=True,
            parent__isnull=False,
            parent__is_active=True,
        )
        .select_related("parent")
        .order_by("parent__position", "parent__name", "position", "name", "pk")
    )


class CategoryChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, category):
        return category.portal_label


class SiteChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, site):
        return f"{site.name} ({site.code})" if site.code else site.name


class RaiseRequestForm(StyledForm, forms.Form):
    category = CategoryChoiceField(
        queryset=None,
        label="What is it about?",
        empty_label="Choose a topic",
    )
    site = SiteChoiceField(queryset=None, label="Site", empty_label="Choose a site")
    subject = forms.CharField(
        max_length=120, help_text="A few words the laboratory and you will recognize."
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="What happened and what you need.",
    )
    client_reference = forms.CharField(
        label="Your reference",
        max_length=80,
        required=False,
        help_text=PATIENT_DATA_HELP,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    files = AttachmentsField(audience="portal")
    follow_up = forms.CharField(required=False, widget=forms.HiddenInput)
    urgent = forms.BooleanField(
        required=False,
        label="Urgent",
        help_text="Ask the laboratory to treat this as urgent. The laboratory confirms the "
        "priority.",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = raisable_categories()
        sites = portal_sites(user)
        # The sites read once: whether one is required and the sites offered come from the same
        # moment, also when the page is drawn later (a site added meanwhile is offered next time).
        site_ids = list(sites.values_list("pk", flat=True))
        if not site_ids:
            del self.fields["site"]
        else:
            self.fields["site"].queryset = sites.filter(pk__in=site_ids)
            self.fields["site"].required = site_required(user, site_count=len(site_ids))
            if self.fields["site"].required and len(site_ids) == 1:
                self.initial.setdefault("site", site_ids[0])
            if not self.fields["site"].required:
                self.fields["site"].empty_label = "Not about one site"
                self.fields["site"].help_text = "Leave it if the ticket is not about one site."


class ReplyForm(StyledForm, forms.Form):
    body = forms.CharField(
        label="Your reply",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Seen by the laboratory. " + PATIENT_DATA_HELP.split(". ", 1)[1],
    )
    files = AttachmentsField(audience="portal")
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)


class UnsentReplyForm(StyledForm, forms.Form):
    """A reply the portal could not send because the ticket was closed (before the page sent
    it, or while it was being saved), shown back read only so the client can copy it into a
    follow-up ticket. Never submitted."""

    body = forms.CharField(
        label="Your reply, not sent",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "readonly": True}),
        help_text="Copy it into a follow-up ticket. Files you chose were not sent either.",
    )


class PortalRequestFilter(SearchFilter):
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[
            ("", "Any status"),
            ("active", "Not yet resolved"),
            *((code, label) for code, (label, _tone) in CLIENT_STATUS.items()),
        ],
    )
