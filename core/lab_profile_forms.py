"""Laboratory profile forms. core.lab_profile applies the same rules again when saving."""

from django import forms

from accounts.confirmation import ConfirmIdentity
from accounts.roles import ROLE_LABELS, ROLES

from .lab_profile import IDENTITY_FIELDS, MAX_LOGO_SIDE, MIN_LOGO_SIDE
from .models import MAX_LOGO_BYTES, Lab, LabCertificate
from .widgets import TelInput

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class GroupedForm:
    groups = ()

    def grouped(self):
        return [(legend, [self[name] for name in names]) for legend, names in self.groups]


class LabIdentityForm(GroupedForm, forms.ModelForm):
    """Address and contact details, shown to staff and on the client portal."""

    groups = (
        ("Address", ["address_line1", "address_line2", "city", "state", "postal_code"]),
        ("Contact details", ["phone", "email", "website"]),
    )

    class Meta:
        model = Lab
        fields = list(IDENTITY_FIELDS)
        labels = {"state": "State"}
        help_texts = {
            "address_line1": "Optional. The laboratory's street address as clients should see it.",
            "state": "Optional. US state or territory.",
            "phone": "Optional. As clients should see it, for example 555-0100.",
            "email": "Optional. An address clients can write to.",
            "website": "Optional. The full address, starting with https:// or http://.",
        }
        widgets = {"phone": TelInput(), "website": forms.URLInput()}


class LabLogoForm(GroupedForm, forms.Form):
    """Upload the laboratory's logo, shown on the profile and the client portal."""

    logo = forms.FileField(
        label="Logo image",
        widget=forms.FileInput(attrs={"accept": "image/png,image/jpeg"}),
        help_text=(
            f"A PNG or JPEG file of at most {MAX_LOGO_BYTES // 1024} KiB, each side between "
            f"{MIN_LOGO_SIDE} and {MAX_LOGO_SIDE} pixels, in RGB or greyscale colour. The file is "
            "kept exactly as uploaded."
        ),
    )
    groups = (("Logo", ["logo"]),)


class LabCertificateForm(GroupedForm, forms.ModelForm):
    """Add or change one certificate as the laboratory records it."""

    groups = (
        ("Certificate", ["authority", "program", "number"]),
        ("Validity", ["valid_from", "valid_until"]),
        ("Listing", ["position"]),
    )

    class Meta:
        model = LabCertificate
        fields = [
            "authority",
            "program",
            "number",
            "valid_from",
            "valid_until",
            "position",
        ]
        help_texts = {
            "authority": "The body that issued the certificate, as written on it.",
            "program": "Optional. The program or scope named on the certificate.",
            "number": "The certificate number or laboratory ID the authority assigned.",
            "valid_from": "Optional. Leave blank when the certificate states no start date.",
            "valid_until": "Optional. Leave blank when the certificate states no end date.",
            "position": "Certificates are listed in this order, lowest first.",
        }
        widgets = {"valid_from": DATE, "valid_until": DATE}


class LabContactForm(GroupedForm, forms.Form):
    """Record, change or clear the security contact or the privacy contact
    (core.lab_profile.save_contact), with a reason. `expected_revision` is the contact's revision
    the form showed: a change saved in between refuses this one."""

    name = forms.CharField(
        label="Name or office",
        max_length=160,
        required=False,
        help_text="The person or office, for example Information Security Office. Required "
        "when any other detail is given.",
    )
    title = forms.CharField(
        label="Title or role",
        max_length=160,
        required=False,
        help_text="Optional. As your laboratory names the role.",
    )
    email = forms.EmailField(label="Email", max_length=254, required=False, help_text="Optional.")
    phone = forms.CharField(
        label="Phone",
        max_length=40,
        required=False,
        widget=TelInput(),
        help_text="Optional. For example 555-0100.",
    )
    reason = forms.CharField(
        label="Reason",
        max_length=2000,
        help_text="Kept with the change in the laboratory profile's history.",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    groups = ((None, ["name", "title", "email", "phone", "reason"]),)


class ModuleSwitchForm(forms.Form):
    """Turn an optional module on or off (core.modules.set_enabled). `enabled` is the state the
    page asked for, the opposite of the one it showed, and `expected_revision` the switch's
    revision it showed: a switch in between is refused rather than silently inverted."""

    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    enabled = forms.TypedChoiceField(
        choices=[("1", "On"), ("0", "Off")],
        coerce=lambda value: value == "1",
        widget=forms.HiddenInput,
    )
    reason = forms.CharField(
        label="Reason",
        max_length=2000,
        help_text="Kept with the change in the laboratory profile's history.",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )


class SessionLimitsForm(ConfirmIdentity, GroupedForm, forms.Form):
    """Set one audience's session limits (core.session_limits.save_limits), with a reason.
    `expected_revision` is the limits' revision the form showed: a change saved in between
    refuses this one. The bounds are checked again when saving. Saving asks the person to
    confirm it is them (accounts.confirmation)."""

    inactivity_minutes = forms.IntegerField(
        label="Sign out after no activity for",
        min_value=5,
        max_value=60,
        help_text="Minutes, from 5 to 60. Opening a page, saving or Stay signed in counts as "
        "activity; reading or typing on a page does not.",
        error_messages={
            "min_value": "Choose from 5 to 60 minutes.",
            "max_value": "Choose from 5 to 60 minutes.",
        },
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )
    maximum_hours = forms.IntegerField(
        label="Longest session",
        min_value=1,
        max_value=24,
        help_text="Hours, from 1 to 24, counted from sign-in whatever the activity.",
        error_messages={
            "min_value": "Choose from 1 to 24 hours.",
            "max_value": "Choose from 1 to 24 hours.",
        },
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )
    reason = forms.CharField(
        label="Reason",
        max_length=2000,
        help_text="Kept with the change in the history on Security settings.",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    groups = ((None, ["inactivity_minutes", "maximum_hours", "reason"]),)


ROLE_CHOICES = [(role, ROLE_LABELS[role]) for role in ROLES]


class SecondFactorRuleForm(ConfirmIdentity, GroupedForm, forms.Form):
    """Set who must have a second sign-in factor (core.second_factor.save_rule), with a reason.
    `expected_revision` is the rule's revision the form showed: a change saved in between refuses
    this one. The roles are checked again when saving. Saving asks the person to confirm it is
    them (accounts.confirmation)."""

    staff = forms.BooleanField(
        label="Every staff account",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Every account that is not a client portal account, whatever its role.",
    )
    roles = forms.MultipleChoiceField(
        label="Staff with these roles",
        choices=ROLE_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="Administrators do the work of every role, so choosing any role includes them.",
    )
    patient_information = forms.BooleanField(
        label="Staff who can view patient information",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Staff accounts granted Can view patient information, themselves or through any "
        "of their groups, with or without a role.",
    )
    admin_console = forms.BooleanField(
        label="Accounts that can open the admin console",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Accounts with staff status in the admin console, superusers among them.",
    )
    portal = forms.BooleanField(
        label="Client portal accounts",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    reason = forms.CharField(
        label="Reason",
        max_length=2000,
        help_text="Kept with the change in the history on Security settings.",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    groups = (
        (
            "Accounts that must have one",
            ["staff", "roles", "patient_information", "admin_console", "portal"],
        ),
        ("Reason for the change", ["reason"]),
    )


class RevealConfirmationRuleForm(ConfirmIdentity, GroupedForm, forms.Form):
    """Turn on or off the confirmation before the first reveal of patient information in a session
    (core.reveal_confirmation.save_rule), with a reason. `expected_revision` is the rule's revision
    the form showed: a change saved in between refuses this one. Saving asks the person to confirm
    it is them (accounts.confirmation)."""

    first_reveal = forms.BooleanField(
        label="Confirm it is you before the first reveal in a session",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Staff who can view patient information give their password, and a code from "
        "their authenticator app or a recovery code where they have one, before they first "
        "reveal a patient-information value or a person-level key in each session. Later reveals "
        "in that session are not asked.",
    )
    reason = forms.CharField(
        label="Reason",
        max_length=2000,
        help_text="Kept with the change in the history on Security settings.",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    groups = (
        ("Revealing patient information", ["first_reveal"]),
        ("Reason for the change", ["reason"]),
    )


class ConfirmBeforeRevealForm(ConfirmIdentity, GroupedForm, forms.Form):
    """Confirm it is you before revealing patient information (core.reveal_confirmation): the
    confirmation's fields only, and the page to return to."""

    next = forms.CharField(required=False, widget=forms.HiddenInput)
    groups = ()

    def _ask(self, state):
        super()._ask(state)
        # Nothing is saved here: the messages for a field left empty say what it is for.
        from accounts.confirmation import CODE_FIELD, PASSWORD_FIELD

        if PASSWORD_FIELD in self.fields:
            self.fields[PASSWORD_FIELD].error_messages["required"] = (
                "Enter your password to confirm it is you."
            )
        if CODE_FIELD in self.fields:
            self.fields[CODE_FIELD].error_messages["required"] = (
                "Enter a code from your authenticator app, or a recovery code, to confirm it is "
                "you."
            )
        if self.confirm_refused:
            self.confirm_refused = (
                "Your account has no password to confirm it is you, so you cannot reveal "
                "patient information while your laboratory asks for this. Set a password on "
                "Change password first."
            )

    def confirmation_help(self):
        password, code = self.confirm_with or (False, False)
        asked = (
            "your password and a code from your authenticator app (or a recovery code)"
            if password and code
            else "a code from your authenticator app (or a recovery code)"
            if code
            else "your password"
        )
        return (
            f"Enter {asked}. Once you have confirmed, you are not asked again before a reveal "
            "until you sign out or your session ends."
        )
