# Backend rules

The principles are in `AGENTS.md` (rules 2–4, 7, 9): provenance, authorization on every read
and write, attributed audits, no homemade layers. This file names the code that implements them.
Reuse it; don't write a second version.

- **Who may act.** Use `accounts/roles.py`: `require_role(user, *roles)` / `has_role` for staff
  roles (`ROLES`; admin implies the four `OPERATIONAL_ROLES`), `require_staff` for any staff role,
  and `require_capability(user, codename)` / `has_capability` for an explicit capability
  (`CAPABILITIES`, today `view_patient_information`). A capability is a permission on
  `accounts.User`, held by the account or one of its groups; no role, admin or superuser implies
  it, and a client portal account never holds one. App role sets live in
  `<app>/permissions.py` (`clients/permissions.py`: `MANAGE_ROLES`, `require_client_manager`).
  Check inside the service, not only in the view. A hidden button or admin access is never the
  gate.
- **Client ownership.** Portal views use `client_required` and build every queryset with
  `client_scope(queryset, user)` (`clients/portal.py`), which starts from `user.client_id`.
  Return 404 for another client's record, never a filtered-out 200. Staff accounts have
  `client_id is None` and are refused by `client_required`; client accounts never hold roles.
- **Change and audit commit together.** Decorate the service with `@transaction.atomic`, lock
  with `select_for_update()`, recheck the state, change it, then call
  `core.audit.record_event(actor=, action=, obj=, before=, after=, reason=)`, which raises outside
  a transaction. Use `core.audit.snapshot(obj)` for before/after, never on account or credential
  models. `clients/services.py` is the worked example (`create_client`, `update_client`,
  `add_contact`, `update_contact`): a reason is required on update, and an update that changes
  nothing is refused.
- **Concurrent edits.** Configuration edit forms use `core/concurrency.py`: `protect_form(form,
  instance)` adds the original-state token, and `check_original_state(instance, expected)` rejects
  a stale save while keeping the input. Records with a counter use `expected_revision`
  (`clients.services.update_client`). Recheck after locking and before writing.
- **Forms and lists.** Forms derive from `core/forms.py` (`StyledForm`, `GroupedForm` for
  sectioned layouts, `StaffChoiceField`, `SearchFilter`, `ActiveFilter`, `REASON_HELP`). List
  pages call `core.lists.list_response(...)` with a column map, search fields, `filter_fields`
  and a `refine` callable for filters that are not a plain match; an invalid filter shows no rows.
- **Remote select lookups.** Register in `core/lookups.py`: the lookups are `clients`,
  `active-clients`, `portal-contacts` and `staff`; add one with
  `register(Lookup("<name>", staff(lambda request: …), SEARCH_FIELDS, ordering))`, or
  `portal_client(...)` for portal lists. The form's `ModelChoiceField` queryset still validates.
- **Per-user presentation** is `core/preferences.py` (`grid:<grid>:v<n>`, `page:<view name>`,
  `nav.collapsed`, `whats_new.seen`). It is presentation only and never widens a queryset.
- **Dates and clocks.** `core.due.due_state` decides overdue and soon for dated obligations. Don't
  reimplement it in a view or template. New form input and displayed times use the deployment
  `TIME_ZONE`.
- **Vocabularies.** `core/vocabulary.py` (`US_STATES`) holds shared choice lists; a field that
  takes the laboratory's own wording uses `core.widgets.ListOrOtherField`.
- **Migrations.** Additive and backward compatible while old and new code overlap. Add nullable
  or defaulted fields and backfill in a separate step. A destructive change is staged across
  releases (`docs/architecture.md`). Never edit a merged migration, and keep
  `makemigrations --check` clean.
- **Structure.** Domain logic lives in the app's `services.py` or named domain modules and reads
  in `selectors.py` when they grow. No generic service or repository base classes, and no
  workflow DSL.
- **Tests** go in `<app>/tests/` (cross-cutting ones in `tests/`), using the app's `conftest.py`
  fixtures. Cover the allowed and the refused role, another client's user, the audit event and
  the stale-edit path. List pages keep a query budget with more rows than one page
  (`CaptureQueriesContext` or `django_assert_max_num_queries`).
