"""Form building blocks shared by every app: Bootstrap control classes, sectioned layouts, the
staff picker and the list filter base. Templates: includes/grouped_fields.html renders a
GroupedForm; core/record_form.html is the create and edit page around one."""

from django import forms
from django.contrib.auth import get_user_model

from core.lookups import RemoteSelect

REASON_HELP = "Kept with the change in the record's history."


class StyledForm:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                css = "form-check-input"
            else:
                css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", css)


class GroupedForm:
    """Sections for includes/grouped_fields.html:
    ((legend, ((field name, column class), ...)), ...).

    A field the form does not have is skipped, so create and edit forms share one layout."""

    groups = ()

    def grouped(self):
        out = []
        for legend, layout in self.groups:
            fields = [(self[name], css) for name, css in layout if name in self.fields]
            if fields:
                out.append((legend, fields))
        return out


class StaffChoiceField(forms.ModelChoiceField):
    """Active laboratory accounts; a client portal account is never offered."""

    def __init__(self, **kwargs):
        kwargs.setdefault(
            "queryset",
            get_user_model()
            .objects.filter(is_active=True, client__isnull=True)
            .order_by("first_name", "last_name", "username"),
        )
        kwargs.setdefault("empty_label", "Not assigned")
        kwargs.setdefault("widget", RemoteSelect("staff"))
        super().__init__(**kwargs)

    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


class SearchFilter(StyledForm, forms.Form):
    """Base of a list page's filter form (core.lists.list_response): free-text search plus the
    page's own fields. A pair of `<prefix>_from` / `<prefix>_to` date fields is checked in order."""

    q = forms.CharField(required=False, max_length=200, label="Search")
    date_ranges = ()  # prefixes of the from/to date pairs this filter has

    def clean(self):
        data = super().clean()
        for prefix in self.date_ranges:
            start, end = data.get(f"{prefix}_from"), data.get(f"{prefix}_to")
            if start and end and start > end:
                self.add_error(f"{prefix}_to", "End date must be on or after the start date.")
        return data


class ActiveFilter(SearchFilter):
    """Search plus the Active / Inactive status choice list_response understands."""

    active = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), ("all", "All"), ("yes", "Active"), ("no", "Inactive")],
        label="Status",
    )
