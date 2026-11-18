# OpenBGA Lab Ops UI standards

These conventions apply to staff and client portal pages. Use the shared Django template tags
and widgets so navigation, forms and tables behave consistently across the application.

## 1. Library and principles

- Bootstrap 5.3 is the only component library. Controls, dialogs, dropdowns, collapses, tables
  and form widgets are Bootstrap components, themed through the `--vr-*` tokens in
  `static/css/app.css`, which map onto Bootstrap's `--bs-*` variables. DataTables presents grid
  columns, Tom Select draws every select and suggestion input, Vanilla Calendar Pro draws the
  calendar of the date fields. Nothing else, and no self-written
  replacement for something these provide. No control shows the browser's own UI (section 8,
  Form controls); the library's own icons and arrows are replaced by Bootstrap Icons symbols.
- One job, one component. Each concept in section 8 has exactly one shared include or one markup
  pattern. A page that needs a variation asks for a parameter on the shared part; it does not
  copy the markup.
- Templates carry no colours, no inline `style`, and no Bootstrap colour utilities that bypass
  the tokens (`bg-white`, `bg-light`, `text-dark`, `text-black`, `table-light`, and any other
  `text-*`, `bg-*`, `border-*`, `link-*`, `btn-*` or `table-*` colour that `app.css` does not
  theme). Muted text is `.vr-muted`, never `text-body-secondary` or `text-muted`.
- Use the tokens in `static/css/app.css` for colour, type, spacing, sizes, focus and layering.
  Define any new semantic token for light, dark and print in the Tokens section. The stylesheet
  maps Bootstrap variants to these tokens. Keep one-off dimensions limited to the documented
  exceptions in `tests/test_css_tokens.py`.
- Scripts build markup through the same parts: `window.vrUi.icon(name)` (the markup of
  `includes/icon.html`) and `window.vrUi.button(label, role, small)` (the button roles below),
  both in `static/js/app.js`.
- Icons. Every icon is a Bootstrap Icons 1.13.2 symbol (the version pinned in
  `static/vendor/README.md`), copied verbatim into `static/img/icons.svg` as `i-<bootstrap-name>`,
  and rendered only through the `{% icon %}` tag (`includes/icon.html`) in templates or
  `window.vrUi.icon` in scripts.
  Status glyphs are Bootstrap Icons too (`includes/status.html`). No inline `<svg>`, no hand-drawn
  glyph, text character or `clip-path` shape standing in for an icon, and no custom SVG data URI;
  `app.css` keeps only Bootstrap's own form-control images. The drawings that are not icons are
  the brand mark (`static/img/mark.svg`), the sign-in scene (`static/img/entrance/scene-light.svg`
  and `scene-dark.svg`, built by `tools/entrance_scene/`, a CSS background by theme) and
  the line diagram's CSS dots, rings, ticks and stubs (section 12). To add an icon, copy its `<symbol>` from the pinned
  `bootstrap-icons` package (`icons/<name>.svg`: its `viewBox` and inner markup, unchanged) into
  `icons.svg`, then run `tests/test_icons.py`.
- One term per thing. The sidebar label, the page title, the breadcrumb and the button that
  leads to a page use the same words (section 9).
- The server decides. Hidden controls are a courtesy; every rule here sits on top of server-side
  authorization and validation, never in place of them.

## 2. Page types

Every page is one of these. The type fixes its header, layout and navigation behaviour.

| Type | Purpose | Breadcrumbs | Primary action | Examples |
| --- | --- | --- | --- | --- |
| List | Find and open records | No | `New <thing>` in the header | Client organizations |
| Record | Read one record, start actions on it | Yes | The next step for the record's state, in the header | Client organization |
| Form | Create or edit one record in a dialog | Fallback page only | The submit button, in the form's action row | New client organization, Edit contact |
| Action form | One audited change in a dialog | Fallback page only | The submit button, in the form's action row | A single state change that needs a reason |
| Entry grid | Enter many values in a wide, maximizable dialog | Fallback page only | The save button, in a sticky action row | — |
| Overview | Work queues and counts that link to filtered lists | No | None, or the one most frequent start | Dashboard |
| Document | A preview of something issued or exported | Yes | The decision about the document, in the header | — |

## 3. Navigation

1. The sidebar owns work destinations. Show destinations for the account’s effective roles and
   explicit capabilities, combine them across roles, and omit empty sections. Existing record
   links and direct pages retain their authorization even when their destination is absent from
   the sidebar. A destination appears in exactly one navigation component: never in the sidebar
   and again as tabs, pills or header buttons.
2. Section tabs exist for one case only: one sidebar item whose content is two to six sibling
   lists that are not themselves sidebar items, or sibling sub-views of one action. Declare tab sets in
   `core/templatetags/navigation.py`.
   They are links with their own URLs, rendered by `includes/section_tabs.html` directly under
   the page header, with `aria-current="page"` and no tab roles. One visual style. Tabs stay
   visible on every list in the set and never appear on record or form pages.
3. A long page with four or more sections gets an "On this page" list of anchors
   (`includes/page_toc.html`). It is not a tab set and does not hide content.
4. Each list, overview, record, form and action page maps to one sidebar item. Mark that item
   current only when it is visible for the account; a permitted page whose destination is absent
   marks none. Pages reached from the user menu (password, two-factor) also mark none. The
   mapping from route to sidebar item is declared in one place and checked by a test (section
   11). A route shared by two kinds of record is declared as shared (`SHARED_ROUTES`), and the
   view names the item for the record at hand.
5. Breadcrumbs appear on every record, form, action, entry and document page, and never on lists
   or overviews. The trail is `Sidebar item › Record identifier › Action`; the last crumb is the
   current page and is not a link. Under a tabbed section the trail is `Section › Tab › …`, where
   the section is text and the tab links to its list. Rendered by `includes/breadcrumbs.html`.
   There is no separate Back link.
6. Cross-links to another section belong in the content where they are relevant (an organization
   name in a table cell), not in the page header as a button.
7. Signed-in users keep the application shell on error pages (400, 403, 404).
8. The client portal has a top navigation bar and no sidebar. The same rules apply: one current
   item, including Home on the landing page.

## 4. Page header

Top to bottom, all through the blocks of `templates/base.html`:

| Part | Block | Rule |
| --- | --- | --- |
| Breadcrumbs | `breadcrumbs` | Section 3, rule 5 |
| Title | `page_heading` | List and overview: the sidebar label. Record: the record's identifier. Form: `New <thing>` or `Edit <identifier>`. Action: the same words as the button that opened it |
| Key facts | `page_meta` | Record pages: status first (section 8), then at most four facts that identify the record. Not used for prose |
| Description | `page_intro` | At most one sentence, under the title. Explanatory text never floats between the header and the first panel |
| Actions | `page_actions` | Right of the title on one row at every width from 992px. Order left to right: destructive (outline danger), secondary actions, primary last. At most one primary and three secondary; anything further goes in a `More` dropdown |

- A button-only action (no input needed) that changes the record's state lives in the header.
  An action that needs input opens a form dialog with its own action row.
- A header button carries an icon only when it creates something (`plus`) or downloads or prints
  a file. No other header icons.
- A list has one primary button on the page: the header's `New <thing>`. The toolbar's `Apply`
  and an empty state's action are secondary.

## 5. Lists

The server sorts, filters and paginates the full authorized queryset; DataTables only shows,
hides and reorders columns. One list is one `section.card.vr-table-card`.

1. Toolbar. One toolbar, `includes/grid_toolbar.html`: search (labelled "Search"), up to three
   everyday filters, `More filters` (a Bootstrap collapse, open when one of its filters is
   active), `Apply`, `Clear all` when anything is active, then `Columns` at the right. Active
   filters show as removable chips under the toolbar. A default value is not an active filter.
   On a phone the whole toolbar sits in a bottom sheet behind one `Filter and sort` button
   (rule 9).
2. Columns. Every grid has a `Columns` button, on staff and portal pages alike. A column may carry
   a low priority (`data-vr-col-priority="low"`): it is hidden by default on screens narrower than
   1366px so names keep their room, the user can show it in `Columns`, and a saved layout wins.
   Every standard table, list or record sub-table, fits its card at 1280px and 1440px: column
   heads wrap between words, lists in a cell wrap their items, and a record sub-table without a
   `Columns` control marks its secondary columns the same way (`data-vr-col-priority="low"` on the
   head; `app.js` marks the column's cells), hidden below 1366px while the record keeps the full
   data. Only entry grids and genuinely wide data tables (the laboratory's certificate list)
   scroll sideways, with a visible scrollbar and the frozen-column edge shadow.
   Column layout, rows per page and sort are saved per user on the server (`core/preferences.py`:
   lab workstations are shared, so nothing is kept in the browser); a list opened without
   `per_page` or `sort` uses the user's saved values, and `Clear all` removes filters only.
   Rows are at most two lines; identifiers, dates, figures and statuses never wrap inside
   themselves, and a truncated value carries its full text in a tooltip and for screen readers. Identifier first
   and locked; status next to the identifier in queues; numbers right-aligned with the unit in
   the header; dates as `YYYY-MM-DD HH:MM` in a `<time>` element; actions last and locked.
3. Opening a record. The identifier in the first column is the link to the record page
   (`th[scope=row] > a.vr-row-link`). It is the only way a row opens: no `Open`, `View` or
   `Details` link, no whole-row click, no `Edit` button standing in for it.
   - The link text is the row's own identifier (the organization's name on a client list), and
     an identifier always leads to the record it names, on every list where it appears.
   - Where a record has no page of its own (a client contact), the identifier links to the edit
     form for users who may edit, and is plain text for everyone else.
   - A dashboard queue row is one link to its filtered list, including its count. Each count belongs to that queue alone.
4. Row actions. Only for something done to the row that is not opening it. At most one inline
   button (`btn btn-sm btn-outline-secondary`), the next step for that row's state. Two or more
   go in an `Actions` dropdown. A destructive row action is a button that asks for confirmation,
   never a link that posts.
5. Selection. One mechanism, `includes/select_all.html`: a header checkbox bounded to the page,
   a count, and a bulk action bar. Ineligible rows are disabled with a visible reason.
6. Footer. `includes/pagination.html`: count, rows per page (25, 50, 100), pager. Shown even
   when everything fits on one page; replaced by the empty state when there are no rows.
7. States. `includes/empty_state.html` in place of the table: `empty` with the create action for
   users who may create, `no-results` with `Clear filters`, `error` when a filter is invalid. No
   placeholder rows, no bare "None" paragraphs.
8. Several tables on one page. Each is its own table card with an `h2` in the card header, its
   count, and its `Add <thing>` button in that header. The page has no primary button.
9. On a phone (below 576 px) every list and table uses one pattern, the stacked row, built into
   `static/css/app.css` ("Phones") and `static/js/app.js`; a page adds nothing of its own:
   - Each row is a card. The row header (`th[scope=row]`, the identifier link) is the card's
     title; every other cell shows its column's header as a label beside the value (`app.js`
     copies the header text into the cell's `data-vr-label`). A cell with no header text (an
     actions column) shows its value alone.
   - Filter and sort come after the title and before the rows, and take one control's room: the
     toolbar collapses behind a `Filter and sort` button (funnel icon, a count of the active
     filters and search) with a one-line summary beside it, "Status: Active · State: NY · Sorted
     by Organization", cut with an ellipsis when long. The button opens a bottom sheet
     ({% sheet %}, section 8) holding the whole toolbar, one filter per row (search, filters,
     `More filters`, `Apply`, `Clear all`, `Columns`) and a "Sort by" section. The removable
     chips row is not shown on a phone: the summary replaces it and `Clear all` is in the sheet.
   - Sorting stays: the "Sort by" section of the sheet shows the same server-built
     `{% sort_header %}` links as on desktop as chips, with the current sort and direction
     marked. `app.js` copies them from the table's header into the sheet and then hides the
     table's own header row on phones; without script that header row becomes the "Sort by" row
     of chips above the cards, as before. Columns that cannot be sorted drop out of the chips; a
     table without sortable columns hides its header row visually and keeps it for screen
     readers.
   - Pagination is the same footer as on desktop; `Columns` still chooses which fields a card
     shows, because hidden columns are hidden cells.
   - Requirements on the markup: one `th scope="row"` per row, a visible header text for every
     column a value needs a name for, at most one link per cell (a cell's whole-value link is a
     44 px target). Hide nothing on phones with `d-none`; let the pattern do it.
   - Exceptions: an entry grid or a genuinely wide data table is marked `vr-table-scroll` and keeps
     sideways scrolling inside its card (never the page). Keep its scrolling within the card and verify keyboard and touch access.

## 6. Record pages

1. Order of content: header; alerts that need action; facts; related tables; history last.
2. Facts are `dl.vr-facts` inside a panel with an `h2`. Never a Bootstrap grid of labels, never
   a two-column table.
3. Each related list is a table card following section 5, with its `Add <thing>` button in the
   card header. A related record opens through its identifier link.
4. History is one component, `includes/history_item.html`, newest last, in the final panel:
   what happened, when, who, and the reason where one was required.
5. Layout is `{% record_layout %}` (`includes/record_layout.html`). A side column (from 1200px)
   may hold the context and launch panel of the most frequent record action. It sits beside the
   first panel only (the record's key facts) and grows with its content up to the viewport
   height. Longer content scrolls inside its own keyboard-focusable box with a visible scrollbar
   and a fade while more remains below; it never hides content silently. Long-form guidance does
   not belong in the side column. Every later panel spans the
   full width, so wide tables keep their columns. Below 1200px the page stacks: first panel, side
   column, then the rest, which is also the reading and tab order. Its editing fields open in the
   shared form dialog. No panel inside a panel.
6. `Edit` is a secondary header button for records that can be edited freely. A locked record
   is changed through `Correct <thing>`, which requires a reason and is recorded in history.

## 7. Forms

Use the shared Bootstrap modal for editing records and for actions that need input. The record
or list stays behind the dialog. Long forms and entry grids can be maximized. On a phone the
dialog fills the screen. Keep the heading and close controls visible while the body scrolls.
Fields have a rounded outline, a visible focus ring and an inline message when invalid.

Use one dialog at a time. Move focus inside on open, retain keyboard focus there, and return it
to the opener on close. Escape and Cancel close clean forms; unsaved input offers Keep editing
or Discard changes. Loading, failed requests and validation have visible states. Validation
keeps entered values and focuses the first invalid field. Save follows the existing server
destination; multi-step forms and Add another remain in the dialog. Direct form URLs open the
same dialog, with a server-rendered page as the no-JavaScript fallback. Authentication,
authorization, CSRF, concurrency and business validation stay on the server.

Read-only records, search, filter controls and sign-in use their own pages.

1. Rendering. Django forms through the project renderer (`{{ form }}`, `as_field_group`,
   `includes/field.html`). No hand-written inputs where a form field exists.
2. Structure. `form.vr-form` containing one or more `fieldset.vr-form-section`, each with a
   legend when there is more than one. One column; two columns from 768px only for short related
   pairs; notes full width. Field width follows the expected value.
3. Required fields carry `*` after the label, and the form says "Fields marked * are required."
   once at the top.
4. Errors. `includes/form_errors.html` at the top with `role="alert"`, the message under each
   field, focus on the first invalid field, and entered values kept where the current authorization and form definition permit it.
5. Action row. `div.vr-form-actions`, right-aligned:
   destructive action first (`btn-outline-danger`), then `Cancel` (`btn btn-link`), then the one
   primary button last. Keep the action row visible at the bottom of an open dialog.
   Page, dropdown and sign-in action rows remain in normal flow.
6. Destinations. After saving, the record page; where the record has no page, the list. `Cancel`
   returns to the page the user came from: the record when editing, the list when creating.
7. Entry grids follow rules 4 to 6 and are never DataTables-enhanced: fixed columns, units in
   the header, tab order left to right then down, row-level messages under the row.
8. Child rows (several contacts or line items) use one pattern: a row per item, an `Add <thing>`
   button under the rows, and a `Remove` control on each row that works without JavaScript.

## 8. Components

| Job | The one way to do it |
| --- | --- |
| Status of a record | `{% status %}` (`includes/status.html`: text, Bootstrap Icons glyph, tone); the mapping from a domain state to one of seven tones lives in one small partial per app |
| Panel or table card | `{% panel %}` (`includes/panel.html`): `h2`, count, header actions, body, footer; `table=True` for a table card |
| List rows or their state | `{% list_results %}` (`includes/list_results.html`): the table and pagination, or the empty, no-results or filter-error state |
| Notice inside a page | `{% notice %}` (`includes/notice.html`): the tone's alert variant opening with `includes/status.html` in its `plain` form; `role="alert"` when it blocks the task, otherwise `role="status"` |
| Result of the last action | Django message at the top of the next page (`includes/messages.html`, rendered as a notice); it stays until dismissed or the next navigation. No toasts: a failed background request shows a dismissible danger notice in the same place (`<template id="vr-request-error">`) |
| Breadcrumbs | `{% breadcrumbs %}` (`includes/breadcrumbs.html`) |
| Section tabs | `includes/section_tabs.html` |
| On-this-page anchors | `{% page_toc %}` (`includes/page_toc.html`) |
| Key facts under the title | `{% page_meta %}` (`includes/page_meta.html`) |
| List toolbar, chips, columns | `{% grid_toolbar %}`, `{% filter_chips %}`, `{% grid_columns_button %}` |
| Bottom sheet for choices on a phone | `{% sheet id=… title=… %}…{% endsheet %}` (`includes/sheet.html`; Bootstrap offcanvas-bottom): a header with the title and a 44px Close, a scrolling body, an optional `{% fill footer %}` sticky action row; `below="sm"` makes it a sheet on phones only and plain inline content from 576px (the list toolbar), without it it is a bottom sheet at every width, centred at the form width on a desktop (offer it there only when the phone gesture suits; a desktop task that needs input uses the form dialog, section 7) |
| Sortable header | `{% sort_header %}` (`includes/sort_header.html`) |
| Pagination and count | `{% pagination %}` (`includes/pagination.html`) |
| Empty, no results, filter error | `{% empty_state %}` (`includes/empty_state.html`), through `{% list_results %}` on lists |
| Row selection | `{% select_all %}` for the header, `{% row_select %}` for each row |
| Facts | `dl.vr-facts` of `{% fact %}` rows (`includes/fact.html`; a blank value shows "—") |
| History | `{% history_panel %}` (`includes/history_panel.html`) of `{% history_item %}` entries |
| Record page layout | `{% record_layout %}` (`includes/record_layout.html`): the first panel with the side column beside it, then full-width panels |
| Form page | `{% form_page %}` (`includes/form_page.html`): errors, required note, sections, action row |
| Form action row | `{% form_actions %}…{% endform_actions %}` (`includes/form_actions.html`); the body holds any further buttons |
| Required-fields note | `{% required_note %}` (`includes/required_note.html`) |
| Form error summary | `{% form_errors %}` (`includes/form_errors.html`) |
| One-button POST action | `{% post_button %}` (`includes/post_button.html`) |
| Disclosure of filters | Bootstrap collapse, through the toolbar include |
| Disclosure of a note, long value or collapsible panel | `<details>` with a `<summary>`, styled once in `app.css` (chevron marker, focus ring); `{% panel collapsible=True %}` for a panel |
| Menu of actions | Bootstrap dropdown |
| Editing or an action requiring input | Shared maximizable Bootstrap form dialog, with the object named |
| Confirmation of a destructive action | Compact Bootstrap confirmation dialog for a single yes or no; shared form dialog when input is required |
| Brand mark and name | `{% brand %}` (`includes/brand.html`) |
| Icon | A Bootstrap Icons symbol in `static/img/icons.svg`, through `{% icon %}` (`includes/icon.html`); `window.vrUi.icon` in scripts |
| Hint on hover and focus | A `title` attribute; `static/js/controls.js` turns it into a Bootstrap tooltip and makes a non-interactive hint focusable. A `<time>` keeps its title tooltip out of the tab order; `iframe` and `abbr` keep the native title |

Every shared component is a tag of the `ui` library (`{% load ui %}`), declared once in
`COMPONENTS` in `core/templatetags/ui.py`: its template under `templates/includes/`, whose header
comment documents it, its keyword parameters and the page variables it is documented to read. A
tag renders in a context of its own: the parameters it is given (an omitted one is empty, never a
page variable of the same name), its documented reads, and `request`, `user` and `csrf_token`.
Pages never `{% include %}` a registered component; `tests/test_include_isolation.py` checks
that no component reads a page variable it was not given.

Form controls. No control shows the browser's own UI. Each is one Django widget template in
`templates/django/forms/widgets/` plus one initialiser in `static/js/controls.js` and token-only
CSS in the Form controls section of `app.css`; pages get it by rendering a form field, never by
writing the element (`tests/test_controls.py` fails on a raw `<select>`, `<datalist>` or
date/time/number/file `<input>` in a page template). The native element stays in the form as
the no-JavaScript fallback and the submitted value, and the server stays the validator.

| Field | Control |
| --- | --- |
| Choice (`select`) | Tom Select. Up to eight options: no search box; type-ahead, and Space or Enter opens the menu, as a native select. More than eight: a search box in the menu. The empty choice stays selectable. A select that may be empty (a filter's "Any …", an optional field, a multi-select) has a clear button, `Clear <label>`, beside the chevron, shown while the control is hovered, focused or open (always on touch screens) and only when there is a value; Backspace or Delete on the closed control also clears. Clearing fires the usual change event (filters still wait for Apply); a required select has none. The menu is fixed beside its field, so a dialog or table never clips it |
| Large or growing model choice | `core.lookups.RemoteSelect("<lookup>")`: Tom Select loading pages from `/lookups/<lookup>/` as the user types. Each lookup in `core/lookups.py` builds its queryset from the request (staff roles, or the portal user's own client); the form field's queryset still validates, and an edit form renders its current value |
| Multiple choice (`SelectMultiple`) | Tom Select with removable chips. Short lists stay Bootstrap check groups (`CheckboxSelectMultiple`) |
| Free text with suggestions | `core.widgets.SuggestInput` (values or `(value, label)` pairs): Tom Select free text; what is typed is the value |
| Date, date and time | A typed text field in the format the server reads (`YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, with `:SS` where the field records seconds; US `MM/DD/YYYY` is also read), with a calendar popover (Alt+Down, Esc closes and returns focus; weeks start on Sunday). Shortcuts Today, Yesterday, Clear; Now, Today, Clear for date and time. Times are the laboratory's local time, stated under the field; shortcuts use the laboratory's clock, never the browser's zone. An unreadable or out-of-range date stops submission with the message under the field |
| Number | A text field (`inputmode` numeric or decimal where the value cannot be negative), so the exact typed string reaches the Decimal field; no spinners. A unit is the widget attr `unit` (`forms.NumberInput(attrs={"unit": "%"})`): one input group with the unit as its addon, read with the field, wherever the field renders; the label carries no unit |
| File | A drop zone that names the chosen file and its size and lists the accepted types; the native input keeps keyboard focus |
| Search box | One clear button; the browser's own is hidden |
| Checkbox, radio, switch | Bootstrap `form-check`, themed by tokens |
| Validation | Every form is `novalidate` (set centrally by `controls.js`): no browser bubbles; errors are the server's inline messages |

Buttons:

| Role | Classes | Where |
| --- | --- | --- |
| Primary | `btn btn-primary` | One per page or form |
| Secondary | `btn btn-outline-secondary` | Header, toolbar, card header |
| Quiet | `btn btn-link` | `Cancel`, `Clear all` |
| Destructive | `btn btn-outline-danger` | Header or action row, first in the row |
| Row | add `btn-sm` | Table cells and card headers only |

**Interaction states.** Text links underline on hover. Buttons, menu items and other controls
respond with a change to their own surface or outline.

| Element | At rest | On hover |
| --- | --- | --- |
| Text link: a link that is only text (the identifier in a table, a value in a fact, a breadcrumb, an "On this page" anchor, the footer) | Plum text, no underline | Underlined, in the deeper plum |
| Text link inside running text (a paragraph, a notice, a field hint) | Plum and underlined, so colour is never the only cue | The deeper plum |
| Button: primary, secondary, destructive, top bar | Its fill | The fill one step stronger |
| Quiet button (`btn-link`) | Plum text, no fill | The secondary button's fill |
| Tile, navigation item, tab, menu item, pager number, chip, station, file drop zone | Its surface | The surface one step stronger; the current item does not change |
| Field, select, checkbox, radio | Its outline | The outline one step stronger; a checked box the deeper plum |
| Sort header | The label | The strong text colour and the sort arrow |
| Disclosure (`<summary>`), a collapsible panel's header included | The label and its chevron | Both in plum |
| Table row | The zebra | The row fill: it helps the eye follow the row and does not mean the row opens (section 5, rule 3) |

- A control never underlines. A link styled as a button (`<a class="btn …">`) behaves exactly like
  a `<button>` with the same classes.
- `app.css` tells the two apart by the class: a link without a class is a text link, as are
  `vr-row-link`, `vr-id` and `vr-entry-file-name`; every other class on a link is a component's,
  and the component defines its hover. So a text link takes no utility class (`small`,
  `fw-bold`): style its container, or it loses its underline. A component that draws a bare
  `<a>` as a control (a filter chip's remove link, the help panel's jump pills) turns the
  underline off in its own rule.
- Hover is a courtesy for a pointer. The resting state already shows that a thing can be used
  (plum text, a fill, an outline, a chevron), because a touch screen has no hover (section 13).
- Everything that can be clicked shows the pointer; a text field shows the text cursor. A checkbox
  or radio and its label are one target: both show the pointer and hovering either marks the box.
- Focus and disabled states: section 12 (Rules) and section 10.

## 9. Wording

| Meaning | Words | Notes |
| --- | --- | --- |
| Create a top-level record | `New <thing>` | List header. The form's title is the same; its submit is `Create <thing>` |
| Attach a child to the open record | `Add <thing>` | Card header or under the child rows |
| Log something that happened | `Record <event>` | Entering an event after the fact |
| Change a freely editable record | `Edit` in the header; `Edit <identifier>` as the title; `Save changes` to submit | |
| Change a locked record with a reason | `Correct <thing>` | Title and submit use the same words |
| Move a record to another state | `Change <state>`, or the state's own verb (`Close <thing>`, `Withdraw <thing>`) | Not `Update` |
| Leave a form without saving | `Cancel` | Never used alone for anything else |
| End or void a record | Verb plus object (`Cancel <thing>`, `Delete draft`) | A destructive label always names its object |
| Detach a child | `Remove` | |
| Apply filters / reset them | `Apply` / `Clear all` | |

Sentence case everywhere. A title, its breadcrumb, its sidebar item and the button that opens
it use the same words. Staff and portal pages use the same term for the same thing unless the
client's point of view needs another ("Your account code").

## 10. Roles and states

- A control the user's role can never use is not rendered. A control that is unavailable only
  in the record's current state is rendered disabled with the reason in text beside it.
- A page that refuses a person because of their own part in the work says so in one sentence,
  the same wherever it occurs.
- Every page has its states: empty, no results, invalid filter, validation error, denied and
  read-only. Loading states apply only where a request updates part of a page.

## 11. Enforcement

The template tests check the component rules, navigation, icon use, form structure, and
isolation of tag arguments. The CSS checks cover token definitions and theme use. The browser
journeys exercise dialogs, keyboard focus, hover states, table controls, panel spacing and
readability at desktop and phone widths. [Test commands](../CONTRIBUTING.md)

## 12. Visual language

OpenBGA Lab Ops uses a pale mauve shell and damson actions. The stylesheet defines the
light, dark and print themes.

**Separation by surface, never by lines.** The shell, the content ground, panels, raised layers
and hover each have their own tint. Panels, table rows, the shell and dialogs have no outlines.
The only lines are field outlines, checkbox and radio edges, focus rings, the invalid outline and
the coloured lines of the line diagram. No pure black or white on screen.

**Type and shape.** Use the self-hosted Overpass font, with tabular figures for identifiers,
dates and quantities. Reserve monospace for code. Use the shared radii and shadows to distinguish
fields, panels, menus and dialogs.

**Components.**

- Shell: the brand lock-up (the mark with "OpenBGA" over "Lab Ops"), the laboratory name as a
  tonal pill, the client lookup as a field on the shell, quiet Help, Theme and Account buttons. The sidebar keeps a gutter so its scrollbar
  stays inside the shell. Group labels open with a short stub in the area's line colour; the
  current item is the damson fill with bold text and `aria-current="page"`.
- Page header: breadcrumbs on records only; the title; key facts as a row of muted text with the
  status pill first; actions right, aligned to the foot of the title block (secondary buttons,
  then the one primary). An overview's dateline sits under the title, never above it.
- Panels: the surface fill, 16px radius, the panel shadow; the head carries the title, its count
  as a pill and tools on the right, with no rule under it. Spacing is defined once in the panel
  component: every panel body (`.vr-panel-body`, also each `{% panel_section %}`) insets its
  content by `--vr-panel-inset` and separates stacked blocks by `--vr-stack`; a table is flush only
  as a table panel's own body (`.vr-table-body`), where notes and selection bars still take the
  inset; forms inside a panel lie on it without a surface of their own. `e2e/test_panel_spacing.py`
  (measurement in `e2e/panel_spacing.js`) checks the main record and list pages.
- Key facts: one grid of equal tracks (`--vr-fact-width` at least), so labels and values line up
  from row to row; a long value wraps inside its cell, and a fact whose value is a phrase (a
  mailing address) is `{% fact wide=True %}` and spans two tracks, or the
  row when only one fits. `e2e/panel_spacing.js` checks every fact starts on a track line.
- Action rows: on a page the row is plain, in flow directly after its fields. Only a form
  dialog's footer is sticky and carries the raised-edge shadow, and it is always the dialog's last
  element; nothing else in a dialog is raised. Raised edges (dialog header and footer, a frozen
  table column) use one shadow family (`--vr-shadow-scroll-*`).
- Tables: no vertical or per-row rules; a rounded header row in `--vr-th-bg` with 12px bold
  uppercase labels; rows with rounded ends, a faint zebra and a hover fill. The sorted column's
  label is in the strong text colour with its arrow. The identifier link is plum, bold, and
  underlined only on hover.
- Status: a tinted pill (tone fill, tone text, a Bootstrap Icons glyph, 12px bold), no edge;
  inside a table cell it may wrap with a 10px radius; inside a notice it drops its fill.
- Notices use the tone's fill and a 16px radius. The status sits beside the first line of
  the message when both fit. In a narrow panel the message wraps below the status and uses
  the full width. Labelled lines use a two-column `dl` of `{% fact %}` rows.
- Wide tables: the identifier column stays frozen while the table scrolls sideways, with its row's
  opaque fill and an edge shadow only while scrolled; full-width sub-rows (`td.vr-subrow` holding
  `.vr-subrow-content`) stay pinned to the visible width. A free-text row header (`th.vr-wrap`, a
  long text value) is not an identifier and is not frozen.
- Buttons: primary is the damson fill; secondary (`btn-outline-secondary`, kept as the class
  contract) a tonal fill with no outline; quiet (`btn-link`) plum text that takes the secondary
  fill on hover; destructive danger text that takes the danger tint on hover. 36px tall, 30px
  small. Hover follows section 8, Interaction states.
- Fields follow section 7. Checkboxes and radios are
  Bootstrap's own, with a 1.5px edge and damson when checked.
- Dialogs: the raised surface, 18px radius, the dialog shadow and scrim, no rules; the header
  takes a soft shadow while the body is scrolled, and the sticky action row always carries one.
- Scrollbars: thin, rounded and inset on a transparent track; stronger when the container is
  hovered, strongest on the thumb. Never hidden, with one exception: the sidebar's scrollbar is
  quiet at rest and fades in on hover or focus (no fade under reduced motion). Tables and panels
  keep a visible scrollbar, so overflow is never silent.

**Dashboard and area colours.** Dashboard queues are grouped by area in short, linked rows.
Each row shows its own count and, for tickets, the age of the oldest item. Queue definitions
give the role-specific descriptions. The recent organization list shares the row with the
queue panel when the available content width permits. In a narrow panel each organization
keeps its name, code, status and location together in a compact row.
The dashboard's group stubs use the same area colours as the sidebar. A record that moves
through stages can show a route with filled completed stops, a ringed current stop and muted
pending stops. These stubs and stops are the diagram shapes permitted by section 1.

**Footer and printed records.** A record footer gives its identifier, who produced the view
and when. The application footer displays the product name, license and Source code link
configured by `SOURCE_CODE_URL`. Keep it on staff, portal, entrance, error and admin pages.
Hide it in dialogs and print views. History displays both the previous and new value.

Dates and times are rendered by the server in the laboratory's time zone (`TIME_ZONE`), never the
browser's.

**Dark mode** is damson night, composed rather than inverted: a deep plum ground, the shell
darker than the panels, pale orchid for action and the current item.

**Print** always uses one light, high-contrast palette whatever the screen mode: white ground and
panels, panels edged with a hairline instead of a shadow, pills printed as icon and text on white,
no navigation, toolbars or buttons.

**Rules.** Text and its background meet WCAG 2.2 AA (4.5:1, labels and pills included) and field
outlines, checkbox edges, focus rings, the current nav fill and the line colours 3:1 against every
surface they sit on, in both modes and in print. Focus is always visible: the plum ring and halo
on fields, a 2px outline elsewhere. Status never relies on colour alone. Motion is limited to
short colour transitions, and stops under `prefers-reduced-motion`; the sign-in scene is still.
Templates never carry colours: a new need is a token or a component rule in `app.css`.

## 13. Phone use

Design every staff and client page for one-handed use at 360 to 430 px.

- No horizontal page scrolling. A table turns into readable stacked rows or cards at narrow
  widths. Entry grids and genuinely wide data tables are the exception that section 5 names, and
  need a phone layout of their own.
- Navigation and the page's primary action are within thumb reach.
- Touch targets are at least 44 px high and wide.
- Forms use the right input types (email, telephone, numeric) and keep the on-screen keyboard
  from hiding the field being filled in. No control is available only on hover.
- Desktop stays a dense operational workspace.

Use the phone layout below 576 px and the touch-target sizes for coarse pointers. The shared
components provide the following behaviors.

- Tables and lists use the stacked-row pattern of section 5, rule 9, with filtering and sorting
  behind one `Filter and sort` button that opens a bottom sheet, so the rows come first (on a
  360 px screen the first card starts in the first screen above the thumb bar). This is the one
  pattern for dense data on a phone.
- Choices that need a few controls and an action (assign, change a status, filter) open in a
  bottom sheet, `{% sheet %}` (section 8): Bootstrap's offcanvas, the same component as the
  navigation drawer. The scrim and the focus trap keep the page behind it inert; Escape or Close
  dismisses it and returns focus to the button that opened it, whose `aria-expanded` follows the
  sheet; the title is its accessible name. The sheet is at most 85% of the screen high, scrolls
  its body, sits above the thumb bar and keeps its action row (`{% fill footer %}`) in view.
  Use the sheet for a phone choice, and the shared form dialog (section 7) for editing a record.
- Navigation and the primary action are within thumb reach. Staff pages have a bottom thumb bar
  with Menu (the navigation drawer) and Find (tickets and client organizations); the page's one `.btn-primary` in
  `page_actions` sits at its right end. Keep exactly one primary action per page. The client
  portal's navigation moves to the bottom edge. Form dialogs fill the screen with their action
  row fixed at the bottom.
- Touch targets measure at least 44 px (`--vr-touch-target`). On phones and touch screens the
  control-size tokens (`--vr-control-height`, `--vr-control-height-sm`, `--vr-nav-item-height`)
  take that value, so fields, selects, buttons, pager numbers, menu items and sidebar links grow
  together; standalone links, chips, checkbox and radio labels, the help panel's links and
  toggles and small icon buttons are sized in the same section. Links inside running text keep
  their line height (WCAG 2.5.8 exempts them).
- Nothing is reachable only on hover. Where a pointer would reveal something on hover (sort
  arrows, the select's clear button), screens without hover show it from the start.
- Input types: email fields are `type="email"`, phone numbers use `core.widgets.TelInput`
  (`type="tel"`), web addresses `type="url"`. Allauth's hand-written fields render Bootstrap
  controls (`templates/allauth/elements/field.html`).

Check every page you build or change at a phone width (360 and 430 px) and a desktop width, in
both themes, and run the browser journeys at a phone viewport as well
(`e2e/test_client_journey.py` runs at both; the `view` fixture of `e2e/conftest.py` runs a test
at {desktop 1366, phone 390} x {light, dark}). There must be no horizontal page scrolling and no
control under 44 px outside running text, also inside an open sheet. `e2e/test_phone_layout.py`
holds the phone sweep: add a new list to its `LISTS`, and its first row must start in the first
screen.
