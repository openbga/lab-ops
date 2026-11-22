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
from django.core.exceptions import ObjectDoesNotExist
from django.urls import reverse_lazy

from core.forms import StyledForm
from core.ids import whole_number
from taxonomy.models import RequestCategory

from . import definition
from .forms import ValuesForm
from .models import RequestTemplate
from .selectors import staff_templates
from .services import FORM_CHANGED, NOT_OFFERED, attach_values, lock_for_intake, offered_in

PICKER = "request_form"  # also accounts.kept.CHOOSERS
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


def chosen_template(category, raw, client=None, templates=None):
    """The form to show for `category` on a page being drawn: `raw` is the picker's value, or
    None when the picker was not shown yet (then the category's only form offered to `client`,
    if it has exactly one). `templates`: the list the picker draws, read once (slot), so the
    choice and the list agree. Never used to read a submission (see Intake)."""
    if category is None or raw == "":
        return None
    if templates is None:
        templates = list(staff_templates(category, client))
    if raw is not None:
        return next((t for t in templates if str(t.pk) == str(raw)), None)
    return templates[0] if len(templates) == 1 else None


# How many times the slot reads its list and the chosen form again while they disagree (a setup
# change committed between the reads); after that it draws the last list with no form chosen.
READINGS = 3


def _shown(category, *, read=True):
    """What the page shows of `category` besides the form (its revision, and the name of the team
    it sends tickets to): as it stands (`read`), or as the object holds it."""
    if category is None:
        return None
    if not read:
        team = category.default_team
        return (category.revision, category.default_team_id, team.name if team else None)
    row = (
        RequestCategory.objects.filter(pk=category.pk)
        .values_list("revision", "default_team_id", "default_team__name")
        .first()
    )
    return tuple(row) if row else None


def _listing(templates):
    return [(t.pk, t.name, bool(getattr(t, "offered_to_some", False))) for t in templates]


def _describes(templates, defn):
    """Whether `templates` (a list read after `defn`) lists the form `defn` reads as it does: by
    the same name and offer."""
    entry = next((t for t in templates if t.pk == defn.template.pk), None)
    return (
        entry is not None
        and entry.name == defn.template.name
        and bool(getattr(entry, "offered_to_some", False)) == bool(defn.client_ids)
    )


def _draw_list(picker, templates):
    """Draw the picker with `templates` as read (not read again when it renders)."""
    field = picker.fields[PICKER]
    field.choices = [("", field.empty_label)] + [
        (template.pk, field.label_from_instance(template)) for template in templates
    ]


def _bound_slot(category, intake):
    """slot() after a refused submit: the Intake's picker, form and list as read when the
    submission was (one moment, Intake), not read again; a form not offered to the request's
    organization (the desk's form read it after) is not drawn, the picker saying why (No form).
    (picker, values_form, templates), or (None, None) when the slot draws nothing."""
    if intake.values_form is not None:
        drawn = intake.values_form.template.pk
        if not RequestTemplate.objects.filter(pk=drawn).exists():
            # Removed since (not retired: a removed form cannot be drawn or chosen again).
            intake.templates = [t for t in intake.templates if t.pk != drawn]
            intake.withdraw(NO_LONGER)
        elif intake.client is not ANY_ORGANIZATION and not offered_in(
            intake.values_form.definition, intake.client
        ):
            # Not offered to the request's organization: neither drawn nor listed.
            intake.templates = [t for t in intake.templates if t.pk != drawn]
            intake.withdraw(NOT_OFFERED)
    picker = intake.picker
    if not picker.errors and not intake.templates:
        return None, None
    return picker, intake.values_form, intake.templates


def slot(category, user, *, intake=None, query=None):
    """(picker, values_form) as the desk's new-request slot draws them for `category`, or
    (None, None) when it draws nothing: after a refused submit the Intake as posted (`intake`,
    checked against the list as it stands: _bound_slot); otherwise unbound, the form chosen by
    `query`'s picker value (a mapping: the picker's own request, or what a page gives back;
    absent, the category's only form) among those offered to the organization it names. The list
    is read before and after the chosen form's definition, again while the two readings differ,
    and drawn as read: the choice, the list and the chosen form (drawn only when its definition is
    drawable and listed as it reads) come from one moment. The ValuesForm's fields are kept across
    a sign-out as part of the ticket's (accounts.kept): the picker chose it, so a copy saying
    another form was chosen keeps none of its fields."""
    if category is None or user is None:
        return None, None
    if intake is not None:
        drawn = _bound_slot(category, intake)
        if drawn == (None, None):
            return None, None
        picker, values_form, templates = drawn
    else:
        query = query or {}
        raw = query.get(PICKER)
        # The organization chosen when the section is drawn: only the forms offered to it.
        client = whole_number(query.get("client"))
        picker = RequestFormPicker(category, client=client)
        queryset = picker.fields[PICKER].queryset
        for _reading in range(READINGS):
            before = list(queryset.all())
            chosen = chosen_template(category, raw, client, before)
            defn = None
            if chosen is not None:
                try:
                    # One read: the controls, the instructions and the token, with the category
                    # the page shows.
                    defn = definition.read(chosen, category=category)
                except ObjectDoesNotExist:
                    defn = None  # removed since the list was read
            templates = list(queryset.all())
            # The chosen form, read between two equal lists, as they list it (or not listed), and
            # drawable as listed: a definition that cannot be drawn while the list offers the
            # form (retired or withdrawn in between and back) is read again.
            listed = defn is not None and defn.template.pk in {t.pk for t in templates}
            usable = defn is not None and drawable(
                defn, category, ANY_ORGANIZATION if client is None else client
            )
            agrees = defn is None or (
                _describes(templates, defn) and usable if listed else not usable
            )
            if _listing(before) == _listing(templates) and agrees:
                break
        else:
            chosen = None  # still changing: drawn without a form
        values_form = None
        if (
            chosen is not None
            and defn is not None
            and drawable(defn, category, ANY_ORGANIZATION if client is None else client)
            and _describes(templates, defn)
        ):
            values_form = ValuesForm(defn.template, user=user, definition=defn)
        if not templates:
            return None, None
        picker.initial[PICKER] = values_form.template.pk if values_form is not None else ""
    _draw_list(picker, templates)
    if values_form is not None:
        values_form.kept_recheck = (*values_form.kept_recheck[:2], PICKER)
    return picker, values_form


class Intake:
    """A submitted staff intake: the picker as posted and, when it names a current form of the
    category, that form's values (a ValuesForm, which checks the rendered revision)."""

    def __init__(self, category, data, user):
        self.locked = None  # the reading taken for writing (lock)
        # The organization the request will have, once the desk's form has read it (check_offer).
        self.client = ANY_ORGANIZATION
        self.category, self.user = category, user
        self.picker, self.template = None, None

        def choose(category):
            self.picker = RequestFormPicker(category, data)
            self.template = None
            if self.picker.is_valid():
                if PICKER not in data:
                    self.picker.add_error(PICKER, CHOOSE)
                else:
                    self.template = self.picker.cleaned_data[PICKER]
            return self.template

        category, defn, stable = self._bracketed(category, choose)
        self.category = category
        self.values_form = None
        if not stable:
            if self.template is not None or data.get(PICKER):
                self.withdraw(FORM_CHANGED)  # still changing: not drawn, the picker says why
        elif self.template is not None and defn is None:
            self.withdraw(NO_LONGER)
        elif self.template is not None:
            self.values_form = ValuesForm(self.template, data, user=user, definition=defn)
        elif data.get(PICKER) and self.picker.errors.get(PICKER):
            # A posted choice refused (no longer offered): No form, with the reason, never the
            # choice shown without its fields.
            self.withdraw(self.picker.errors[PICKER][0])

    def _bracketed(self, category, choose):
        """Read the category, the list of forms (`templates`, kept: a page drawn again after a
        refusal shows it as read) and the chosen form's definition as one moment: the category
        and the list before and after the choice (`choose(category)`, the template or None) and
        its definition, again while they differ (a setup change committed between them).
        (category, definition or None, whether a reading was stable). A category removed since
        stays as it was read (the desk's form refuses it)."""
        defn = None
        for _reading in range(READINGS):
            if category is not None:
                fresh = (
                    RequestCategory.objects.select_related("default_team", "parent")
                    .filter(pk=category.pk)
                    .first()
                )
                if fresh is None:
                    choose(category)  # the picker as posted, over no forms
                    self.templates = []
                    return category, None, True
                category = fresh
            before = (_shown(category, read=False), _listing(self._list(category)))
            template = choose(category)
            defn = None
            if template is not None:
                try:
                    defn = definition.read(template, category=category)
                except ObjectDoesNotExist:
                    defn = None  # removed since it was chosen
            self.templates = self._list(category)
            after = (_shown(category), _listing(self.templates))
            listed = defn is not None and defn.template.pk in {t.pk for t in self.templates}
            if before == after and (not listed or _describes(self.templates, defn)):
                return category, defn, True
        return category, None, False

    def _list(self, category):
        """The forms of `category` (a submission is read against every form of the category)."""
        return list(staff_templates(category))

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
        in it is on the page (reviews 7 and 8). Read as one moment, as a submission is
        (_bracketed)."""
        chosen = self.template
        category, fresh, stable = self._bracketed(self.category, lambda _category: chosen)
        self.category = category
        if chosen is None:
            return
        listed = {t.pk for t in self.templates}
        if stable and fresh is not None and drawable(fresh, self.category, self.client):
            self.values_form = ValuesForm(chosen, user=self.user, definition=fresh)
            return
        if not stable:
            reason = FORM_CHANGED
        elif fresh is not None and drawable(fresh, self.category):
            reason = NOT_OFFERED
        else:
            reason = NO_LONGER
        if reason == NOT_OFFERED or chosen.pk not in listed:
            self.templates = [t for t in self.templates if t.pk != chosen.pk]
        self.withdraw(reason)

    def withdraw(self, reason):
        """Draw the picker at No form with `reason`, without the form or what was typed in it (a
        redraw that cannot draw the form as validated)."""
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
