"""Laboratory profile forms. core.lab_profile applies the same rules again when saving."""

from django import forms

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
