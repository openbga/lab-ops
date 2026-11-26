"""The forms of access reviews (audit_review.access_reviews): which accounts to review, and the
review itself, with a Keep or Remove decision for each grant and the reviewer's signature."""

from django import forms
from django.views.decorators.debug import sensitive_variables

from accounts import confirmation
from clients.models import Client
from core.forms import StyledForm

from . import access_reviews
from .models import AccessScope, Decision
from .review_forms import signing_refusal


class ScopeForm(StyledForm, forms.Form):
    """Which accounts a review covers: staff accounts, or client portal accounts of every
    organization or of one. Read from the address (GET) or the review form (POST)."""

    scope = forms.ChoiceField(
        label="Accounts to review",
        choices=AccessScope.choices,
        initial=AccessScope.STAFF,
        required=False,
    )
    organization = forms.ModelChoiceField(
        label="Organization",
        queryset=Client.objects.none(),
        required=False,
        empty_label="Every organization",
        help_text="For client portal accounts: one organization's, or every organization's.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = Client.objects.order_by("name", "pk")

    def clean(self):
        data = super().clean()
        data["scope"] = data.get("scope") or AccessScope.STAFF
        if data.get("organization") is not None and data["scope"] != AccessScope.PORTAL:
            self.add_error(
                "organization", "Choose an organization only for client portal accounts."
            )
        return data

    def chosen(self):
        """(scope, organization) when the choice is accepted, else (staff, None)."""
        if self.is_valid():
            return self.cleaned_data["scope"], self.cleaned_data.get("organization")
        return AccessScope.STAFF, None


def shown(grants):
    """The `shown` value of a form over `grants`: their decision fields' names."""
    return ",".join(grant.field_name for grant in grants)


class AccessReviewForm(StyledForm, forms.Form):
    """Record an access review and sign it (access_reviews.sign_access_review): a Keep or Remove
    decision for each grant of `grants` (the reading the page shows), findings, follow-up and the
    password, asked for at each signing and checked only once everything else on the form is
    accepted (check_password). `expected` posts the fingerprint of the grants the form showed."""

    scope = forms.ChoiceField(choices=AccessScope.choices, widget=forms.HiddenInput)
    organization = forms.IntegerField(required=False, widget=forms.HiddenInput)
    expected = forms.CharField(max_length=64, widget=forms.HiddenInput)
    # The decision fields the form showed, so that one drawn again can mark what is new.
    shown = forms.CharField(required=False, widget=forms.HiddenInput)
    findings = forms.CharField(
        label="Findings",
        required=False,
        max_length=access_reviews.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What you found, such as why a grant is to be removed. Refer to people by "
        "username.",
    )
    follow_up = forms.CharField(
        label="Follow-up",
        required=False,
        max_length=access_reviews.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Who removes what and by when: a ticket number, for example.",
    )

    def __init__(self, *args, grants=(), request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.grants = list(grants)
        # Grants the form shows that it did not show when it was sent (drawn again after a change).
        self.changed = set()
        for grant in self.grants:
            self.fields[grant.field_name] = forms.ChoiceField(
                label=grant.label,
                choices=Decision.choices,
                widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
                error_messages={"required": access_reviews.UNDECIDED},
            )
        field = confirmation.password_field()
        field.help_text = (
            "Signing asks for your password each time. A signed access review cannot be changed."
        )
        field.error_messages["required"] = "Enter your password to sign this review."
        self.fields[confirmation.PASSWORD_FIELD] = field
        if self.request is not None:
            confirmation.mark_sensitive(self.request)

    @property
    def password(self):
        return self[confirmation.PASSWORD_FIELD]

    def accounts(self):
        """The grants grouped by account, each with its bound decision field."""
        groups = access_reviews.by_account(self.grants)
        for group in groups:
            group.rows = [
                (grant, self[grant.field_name], grant.field_name in self.changed)
                for grant in group.grants
            ]
        return groups

    def undecided(self):
        return sum(1 for grant in self.grants if grant.field_name in self.errors)

    @sensitive_variables()
    def full_clean(self):
        return super().full_clean()

    def decisions(self):
        """{grant key: decision} from the accepted form."""
        return {grant.key: self.cleaned_data[grant.field_name] for grant in self.grants}

    @sensitive_variables()
    def check_password(self):
        """Once every other field is accepted: whether the password entered is accepted (the
        refusal is added to its field). False without checking it when anything else is refused,
        so that a form sent again for another reason costs no attempt."""
        name = confirmation.PASSWORD_FIELD
        if not self.is_valid():
            return False
        refusal = signing_refusal(self.request, self.cleaned_data.pop(name, ""))
        if refusal:
            self.add_error(name, refusal)
            return False
        self.checked_hash = getattr(self.request, confirmation.CHECKED, None)
        return True

    def redraw(self, grants, fingerprint, message):
        """Draw the form again over `grants` (read as they stand now): the decisions sent are kept
        for the grants shown before and unchanged (the same field), the others are marked changed
        and undecided, the fingerprint is the new reading's, and only `message` is shown as an
        error. The password is asked for again."""
        sent = set(str(self.data.get("shown", "")).split(","))
        data = self.data.copy()
        data["expected"] = fingerprint
        data["shown"] = shown(grants)
        data.pop(confirmation.PASSWORD_FIELD, None)
        redrawn = type(self)(data, grants=grants, request=self.request)
        redrawn.changed = {grant.field_name for grant in grants if grant.field_name not in sent}
        redrawn.full_clean()
        for name in list(redrawn._errors):
            del redrawn._errors[name]
        redrawn.add_error(None, message)
        return redrawn
