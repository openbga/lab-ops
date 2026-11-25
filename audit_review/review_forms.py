"""The forms of the review records (audit_review.reviews): record and sign a review, add a note to
one, and set the review interval."""

from django import forms

from accounts import confirmation
from core.forms import StyledForm

from . import reviews
from .models import EXAMINED, Outcome

DATE = {"type": "date"}
NO_PASSWORD = (
    "Your account has no password to sign with, so this review cannot be signed. Set a password "
    "on Change password first."
)
TOO_MANY = "Too many attempts to enter your password. Wait a minute, then try again."


def signing_refusal(request, password):
    """Why the password entered at signing is not accepted for the signed-in account of `request`,
    or "" when it is. Checked as a password asked for again is checked (accounts.adapter): a
    password not accepted is written to the sign-in history as a failed re-authentication and
    counts toward the failed sign-in limit, and the attempts are rate-limited. The account's
    password is read from the database, never the request's copy. Nothing is recorded in the
    session: a signing does not stand for the confirmation other changes ask for."""
    from allauth.account.adapter import get_adapter
    from allauth.core import ratelimit
    from django.core.exceptions import ValidationError

    user = getattr(request, "user", None)
    if confirmation.read(user).password_hash is None:
        return NO_PASSWORD
    if not ratelimit.consume(request, action="reauthenticate", user=user):
        return TOO_MANY
    try:
        accepted = get_adapter(request).reauthenticate(user, password)
    except ValidationError as error:  # refused by the failed sign-in limit
        return " ".join(error.messages)
    return "" if accepted else "Incorrect password."


class ReviewForm(StyledForm, forms.Form):
    """Record a review and sign it (reviews.sign_review). The password is asked for at each
    signing, and checked only once everything else on the form is accepted (check_password)."""

    period_from = forms.DateField(
        label="From",
        widget=forms.DateInput(attrs=DATE, format="%Y-%m-%d"),
        help_text="The first day of activity the review covers.",
    )
    period_to = forms.DateField(
        label="To",
        widget=forms.DateInput(attrs=DATE, format="%Y-%m-%d"),
        help_text=f"The last day it covers: today or earlier, at most {reviews.MOST_DAYS} days "
        "after From.",
    )
    examined = forms.MultipleChoiceField(
        label="Lists examined",
        required=False,
        choices=[(source.value, source.label) for source in EXAMINED],
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="The lists of this workspace you went through for the period.",
    )
    examined_other = forms.CharField(
        label="Other records examined",
        required=False,
        max_length=reviews.OTHER_LIMIT,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Records kept outside this application that you looked at too, such as the "
        "hosting provider's logs.",
    )
    outcome = forms.ChoiceField(
        label="Outcome",
        choices=Outcome.choices,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )
    findings = forms.CharField(
        label="Findings",
        required=False,
        max_length=reviews.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="What you found and how you checked it. Required when there are findings "
        "that need follow-up.",
    )
    follow_up = forms.CharField(
        label="Follow-up",
        required=False,
        max_length=reviews.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What was done or is to be done, and by whom: a ticket or nonconformance "
        "number, for example.",
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        field = confirmation.password_field()
        field.help_text = (
            "Signing asks for your password each time. A signed review cannot be changed: add a "
            "note to correct it later."
        )
        field.error_messages["required"] = "Enter your password to sign this review."
        self.fields[confirmation.PASSWORD_FIELD] = field
        if self.request is not None:
            confirmation.mark_sensitive(self.request)

    @property
    def password(self):
        return self[confirmation.PASSWORD_FIELD]

    def clean(self):
        data = super().clean()
        for name, message in reviews.check_period(
            data.get("period_from"), data.get("period_to")
        ).items():
            if name not in self.errors:
                self.add_error(name, message)
        if not data.get("examined") and not (data.get("examined_other") or "").strip():
            self.add_error("examined", "Choose at least one list, or say what else was examined.")
        if data.get("outcome") == Outcome.FOLLOW_UP and not (data.get("findings") or "").strip():
            self.add_error("findings", "Say what was found that needs follow-up.")
        return data

    def check_password(self):
        """Once every other field is accepted: whether the password entered is accepted (the
        refusal is added to its field). False without checking it when anything else is refused,
        so that a form sent again for another reason costs no attempt."""
        name = confirmation.PASSWORD_FIELD
        if not self.is_valid():
            return False
        refusal = signing_refusal(self.request, self.cleaned_data.get(name, ""))
        if refusal:
            self.add_error(name, refusal)
            return False
        # The stored hash the password was checked against, for the service to compare with the
        # account as it holds it (reviews.sign_review).
        self.checked_hash = getattr(self.request, confirmation.CHECKED, None)
        return True


class NoteForm(StyledForm, forms.Form):
    """Add a note to a signed review (reviews.add_note). `expected_revision` is the revision the
    review's page showed."""

    text = forms.CharField(
        label="Note",
        max_length=reviews.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="A correction, or what the follow-up came to. It is added below the signed "
        "review and is not part of what was signed.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=1)


class IntervalForm(StyledForm, forms.Form):
    """Set how often reviews are due (reviews.set_interval), with a reason. `expected_revision` is
    the interval's revision the form showed."""

    interval_days = forms.IntegerField(
        label="Review every (days)",
        required=False,
        min_value=1,
        max_value=reviews.MOST_INTERVAL,
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
        help_text="The next review is due this many days after the last one was signed: 31 for "
        "about monthly, 92 for quarterly, 366 for yearly. Leave it empty for no interval.",
    )
    reason = forms.CharField(
        label="Reason",
        max_length=reviews.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Kept with the change in Changes to the interval and under Changes.",
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
