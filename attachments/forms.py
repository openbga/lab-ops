"""The upload field host forms add (see the attachments contract in the scope folder)."""

from django import forms
from django.core.exceptions import ValidationError

from .inspection import ACCEPT, ALLOWED_TEXT, inspect_upload
from .limits import human_size, max_entry_bytes, max_file_bytes, max_files
from .services import check_batch


class FilesInput(forms.FileInput):
    """Several files; on a phone the picker offers the camera and the photo library (accept
    includes image/*, and there is no capture attribute, which would force the camera)."""

    allow_multiple_selected = True
    template_name = "django/forms/widgets/attachment_files.html"

    def __init__(self, attrs=None):
        super().__init__(
            {
                "accept": ACCEPT,
                "data-vr-files": "",
                "data-max-files": str(max_files()),
                "data-max-file-bytes": str(max_file_bytes()),
                "data-max-total-bytes": str(max_entry_bytes()),
                **(attrs or {}),
            }
        )

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"].update(
            {
                "allowed": ALLOWED_TEXT,
                "max_files": max_files(),
                "max_file": human_size(max_file_bytes()),
                "max_total": human_size(max_entry_bytes()),
            }
        )
        return context


class AttachmentsField(forms.FileField):
    """Cleans to a list of attachments.inspection.Inspected (possibly empty). Every file is
    checked by its content; any refused file fails the field with a message per file, and the
    message asks to choose the files again (a browser never refills a file input)."""

    widget = FilesInput

    def __init__(self, *, audience="staff", **kwargs):
        self.audience = audience
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Files")
        kwargs.setdefault(
            "help_text",
            "Photos, scans and documents. Clients see files on replies; files on notes stay "
            "internal."
            if audience == "staff"
            else "Photos, scans and documents for the laboratory.",
        )
        super().__init__(**kwargs)

    def to_python(self, data):
        return data

    def clean(self, data, initial=None):
        uploads = [item for item in (data if isinstance(data, list | tuple) else [data]) if item]
        if not uploads:
            if self.required:
                raise ValidationError(self.error_messages["required"], code="required")
            return []
        if len(uploads) > max_files():
            raise ValidationError(
                f"Attach at most {max_files()} files at a time. Choose the files again.",
                code="count",
            )
        # Declared sizes first, so a batch known to be too large is refused before any file is
        # read into memory. The service checks the real bytes again.
        declared = sum(int(getattr(upload, "size", 0) or 0) for upload in uploads)
        if declared > max_entry_bytes():
            raise ValidationError(
                f"The files add up to {human_size(declared)}; the limit is "
                f"{human_size(max_entry_bytes())} at a time. Choose the files again.",
                code="total",
            )
        errors, inspected = [], []
        for upload in uploads:
            try:
                inspected.append(inspect_upload(upload))
            except ValidationError as error:
                errors.extend(error.messages)
        if not errors:
            try:
                check_batch(inspected)
            except ValidationError as error:
                errors.extend(error.messages)
        if errors:
            errors.append("Choose the files again: a browser cannot keep them after an error.")
            raise ValidationError(errors)
        return inspected


class WithdrawForm(forms.Form):
    reason = forms.CharField(
        label="Reason for withdrawal",
        help_text="Kept with the file's history. Clients no longer reach the file; staff see it "
        "labelled and only supervisors can still download it.",
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
