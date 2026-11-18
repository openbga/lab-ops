"""Staff intake with a request form (F4): the form picker and the form's fields in the desk's
new-request page (the slot desk/_intake_fields.html), and saving the values with the request.

desk.views.request_create calls `bound_intake` with the posted data and, when it is valid,
passes `saver(...)` as create_request's `on_created`, so the values are stored in the creation's
transaction and a refusal undoes it. The picker re-renders the category section through the
desk's own partial (desk:category-guidance), carrying the chosen form.

A submission is read only under the form it names, under the definition it was drawn from
(request_forms.definition; reviews 3 and 5): a form retired, moved to another category or
changed since (its fields, catalogs or items), or a submission that names no form while the
category has some, is refused. The request's own fields are kept; the form is drawn again from
a new reading with none of its answers, or, retired or moved, not at all (reviews 6 and 7). The
category's only form is preselected when the page is drawn, never assumed when it is saved.
Everything the slot says about the form, its instructions included, comes from the reading it
signs (`drawable` checks that reading before anything of it is drawn).

A form offered to selected organizations is used only for a request of one of them.
The picker lists the forms offered to the organization chosen when the category section is drawn
(the category and picker requests include it); any organization chosen or changed afterwards is
checked when the request is saved, against the organization the request will have (a site's or
contact's when none is chosen): a form not offered to it is refused on the picker, with the input
kept, and request_forms.services checks it again. A request with no organization takes only forms
offered to every organization. A request's organization does not change after creation, so the
form stays with the organization it was offered to.
"""

from django import forms
from django.urls import reverse_lazy

from core.forms import StyledForm
from taxonomy.models import RequestCategory

from . import definition
from .forms import ValuesForm
from .selectors import staff_templates
from .services import NOT_OFFERED, attach_values, lock_for_intake, offered_in

PICKER = "request_form"
CHOOSE = "Choose a ticket form, or No form."
NO_LONGER = (
    "The form chosen is no longer used for this category, so nothing was saved. Choose a form "
    "again."
)


class TemplateChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, template):
        if getattr(template, "offered_to_some", False):
            return f"{template.name} (selected organizations only)"
        return template.name


class RequestFormPicker(StyledForm, forms.Form):
    request_form = TemplateChoiceField(
        queryset=None,
        required=False,
        label="Ticket form",
        empty_label="No form",
        help_text="Choose a form to add its fields to the ticket. With No form, fill in only "
        "the fields above and below.",
        error_messages={"invalid_choice": NO_LONGER},
    )

    def __init__(self, category, *args, client=None, **kwargs):
        """`client`: list only the forms offered to it (a page being drawn); a submission is
        read against every form of the category and its offer checked on save (Intake)."""
        super().__init__(*args, **kwargs)
        self.fields["request_form"].queryset = staff_templates(category, client)
        self.fields["request_form"].widget.attrs.update(
            {
                "hx-get": reverse_lazy("desk:category-guidance"),
                "hx-trigger": "change",
                "hx-target": "#category-section",
                "hx-swap": "innerHTML",
                "hx-include": "[name='category'],[name='client']",
            }
        )


# The organization is not chosen yet (a page being drawn without one): any form of the category.
# None is an answer: a request with no organization, which takes only forms offered to all.
ANY_ORGANIZATION = object()


def drawable(defn, category, client=ANY_ORGANIZATION):
    """Whether staff intake may draw the form `defn` reads for `category`: active, filing under
    it, its category and group active, and offered to `client` (a Client, an id, None for a
    request with no organization; ANY_ORGANIZATION when none is chosen yet)
    (selectors.staff_templates for one form, from the reading the page signs). The picker
    lists forms from an earlier query; one retired, moved or withdrawn since is not drawn."""
    template, own = defn.template, defn.category
    return bool(
        template.is_active
        and template.category_id == category.pk
        and own.is_active
        and own.parent_id
        and own.parent.is_active
        and (client is ANY_ORGANIZATION or offered_in(defn, client))
    )


def chosen_template(category, raw, client=None):
    """The form to show for `category` on a page being drawn: `raw` is the picker's value, or
    None when the picker was not shown yet (then the category's only form offered to `client`,
    if it has exactly one). Never used to read a submission (see Intake)."""
    if category is None or raw == "":
        return None
    templates = list(staff_templates(category, client))
    if raw is not None:
        return next((t for t in templates if str(t.pk) == str(raw)), None)
    return templates[0] if len(templates) == 1 else None


class Intake:
    """A submitted staff intake: the picker as posted and, when it names a current form of the
    category, that form's values (a ValuesForm, which checks the rendered revision)."""

    def __init__(self, category, data, user):
        self.picker = RequestFormPicker(category, data)
        self.template = None
        self.values_form = None
        self.locked = None  # the reading taken for writing (lock)
        # The organization the request will have, once the desk's form has read it (check_offer).
        self.client = ANY_ORGANIZATION
        if self.picker.is_valid():
            if PICKER not in data:
                self.picker.add_error(PICKER, CHOOSE)
            else:
                self.template = self.picker.cleaned_data[PICKER]
        self.category, self.user = category, user
        if self.template is not None:
            self.values_form = ValuesForm(
                self.template,
                data,
                user=user,
                definition=definition.read(self.template, category=category),
            )

    def check_offer(self, client):
        """Refuse, on the picker, a form not offered to `client`, the organization the request
        will have (None: no organization), from the form's definition as read; a form drawn
        again after a refusal is checked against it too (refresh)."""
        self.client = client
        if self.values_form is not None and not offered_in(self.values_form.definition, client):
            self.picker.add_error(PICKER, NOT_OFFERED)

    @property
    def definition_changed(self):
        return self.values_form is not None and self.values_form.definition_changed

    def refresh(self):
        """The form or its category changed since the page was drawn (noticed here or by a
        service): the same form, drawn fresh from its current definition with none of the
        answers sent, and the category read again, so the guidance, the desk's category terms
        and the form's token (and its instructions, which the slot draws from the form) come
        from one current object and a second submit is accepted. A form retired or moved to
        another category since, or not offered to the request's organization once the desk's
        form has read it (none: forms offered to every organization only), is not drawn again:
        the picker is set to No form and says why, and nothing of the form or of what was typed
        in it is on the page (reviews 7 and 8)."""
        self.category = RequestCategory.objects.select_related("default_team", "parent").get(
            pk=self.category.pk
        )
        if self.template is None:
            return
        fresh = definition.read(self.template, category=self.category)
        if drawable(fresh, self.category, self.client):
            self.values_form = ValuesForm(self.template, user=self.user, definition=fresh)
            return
        reason = NOT_OFFERED if drawable(fresh, self.category) else NO_LONGER
        self.template, self.values_form = None, None
        data = self.picker.data.copy()
        data[PICKER] = ""
        self.picker = RequestFormPicker(self.category, data)
        self.picker.is_valid()
        self.picker.add_error(PICKER, reason)

    def lock(self):
        """Read the chosen form for writing, taking its locks before the request is created, in
        the transaction that creates it (request_forms.definition: one lock order everywhere);
        the values are stored from this one reading (saver)."""
        if self.template is not None:
            self.locked = lock_for_intake(self.template)

    def is_valid(self):
        values_ok = self.values_form is None or self.values_form.is_valid()
        return not self.picker.errors and values_ok

    @property
    def template_fields(self):
        return self.values_form.template_fields if self.values_form is not None else []

    def add_value_errors(self, exc):
        if self.values_form is not None:
            self.values_form.add_value_errors(exc)
        else:
            self.picker.add_error(None, exc)


def bound_intake(category, data, user):
    """The Intake of posted `data`, or None when the category has no form and none was named."""
    if category is None:
        return None
    if PICKER not in data and not staff_templates(category).exists():
        return None
    return Intake(category, data, user)


def saver(intake, actor):
    """create_request's `on_created` for a valid Intake (None when it chose no form)."""
    values_form = intake.values_form
    if values_form is None:
        return None

    def save(request):
        attach_values(
            actor=actor,
            request=request,
            template=values_form.template,
            expected_definition=values_form.posted_definition,
            values=values_form.values(),
            details_not_known=values_form.details_not_known,
            locked=intake.locked,
        )

    return save
