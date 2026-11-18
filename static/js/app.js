// Small progressive enhancements. Pages must work without this file.
(function () {
  "use strict";

  // Shared markup for the other scripts (grid.js, form-dialog.js, test-picker.js), so they build
  // the same icon as includes/icon.html and the button roles of docs/ui-standards.md section 8.
  var BUTTON_ROLES = {
    primary: "btn btn-primary",
    secondary: "btn btn-outline-secondary",
    quiet: "btn btn-link",
    destructive: "btn btn-outline-danger",
  };

  window.vrUi = {
    // Decorative sprite icon as markup (for innerHTML and Tom Select render strings).
    icon: function (name) {
      var sprite = document.body.getAttribute("data-vr-icons") || "";
      return '<svg class="vr-icon" aria-hidden="true" focusable="false"><use href="' + sprite + "#i-" + name + '"></use></svg>';
    },
    // <button type="button"> in a standard role; small for table cells, card headers and dialogs.
    button: function (label, role, small) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = BUTTON_ROLES[role] + (small ? " btn-sm" : "");
      button.textContent = label;
      return button;
    },
  };

  // Colour theme: "light" | "dark" | "system" in localStorage "vr-theme" (see theme-init.js).
  var THEME_KEY = "vr-theme";
  var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function savedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "system";
    } catch (error) {
      return "system";
    }
  }

  function applyTheme(choice) {
    var dark = choice === "dark" || (choice === "system" && systemDark && systemDark.matches);
    document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
    document.documentElement.setAttribute("data-vr-theme-choice", choice);
    var label = choice.charAt(0).toUpperCase() + choice.slice(1);
    document.querySelectorAll("[data-vr-theme-value]").forEach(function (option) {
      option.setAttribute("aria-pressed", option.getAttribute("data-vr-theme-value") === choice ? "true" : "false");
    });
    document.querySelectorAll("[data-vr-theme-button]").forEach(function (button) {
      button.setAttribute("title", "Theme: " + label);
    });
    // The visible label names the current choice (the server cannot know the stored one).
    document.querySelectorAll("[data-vr-theme-label]").forEach(function (text) {
      text.textContent = label;
    });
  }

  document.addEventListener("click", function (event) {
    var option = event.target.closest("[data-vr-theme-value]");
    if (!option) {
      return;
    }
    var choice = option.getAttribute("data-vr-theme-value");
    try {
      localStorage.setItem(THEME_KEY, choice);
    } catch (error) {
      // Storage blocked: the choice applies to this page only.
    }
    applyTheme(choice);
    var toggle = option.closest(".dropdown").querySelector("[data-vr-theme-button]");
    if (toggle) {
      toggle.focus();
    }
  });

  if (systemDark) {
    systemDark.addEventListener("change", function () {
      if (savedTheme() === "system") {
        applyTheme("system");
      }
    });
  }

  // Another tab changed the theme.
  window.addEventListener("storage", function (event) {
    if (event.key === THEME_KEY) {
      applyTheme(savedTheme());
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(savedTheme());
  });

  // Page-bounded select-all: <input data-vr-select-all="name"> checks the enabled row checkboxes
  // named "name" in the same table; <span data-vr-select-count="name"> reads "3 of 18 selected".
  function selectionScope(control) {
    return control.form || control.closest("form, .vr-table-card") || document;
  }

  function rowBoxes(name, scope) {
    if (scope && scope.tagName === "FORM") {
      return Array.from(scope.elements).filter(function (control) {
        return control.type === "checkbox" && control.name === name;
      });
    }
    return Array.prototype.slice.call((scope || document).querySelectorAll('input[type="checkbox"][name="' + name + '"]'));
  }

  function syncSelection(name) {
    document.querySelectorAll('[data-vr-select-all="' + name + '"]').forEach(function (master) {
      var eligible = rowBoxes(name, master.closest("table")).filter(function (box) { return !box.disabled; });
      var checked = eligible.filter(function (box) { return box.checked; }).length;
      master.checked = eligible.length > 0 && checked === eligible.length;
      master.indeterminate = checked > 0 && checked < eligible.length;
      master.disabled = eligible.length === 0;
      selectionScope(master).querySelectorAll('[data-vr-select-count="' + name + '"]').forEach(function (counter) {
        counter.textContent = checked + " of " + eligible.length + " selected";
      });
    });
    // Optional "select only ready" shortcut (includes/select_all.html with ready_label).
    document.querySelectorAll('[data-vr-select-ready="' + name + '"]').forEach(function (button) {
      var ready = rowBoxes(name, selectionScope(button)).filter(function (box) { return !box.disabled && box.hasAttribute("data-ready"); });
      button.disabled = ready.length === 0;
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-vr-select-ready]");
    if (!button) {
      return;
    }
    var name = button.getAttribute("data-vr-select-ready");
    var changedForms = new Set();
    rowBoxes(name, selectionScope(button)).forEach(function (box) {
      if (!box.disabled) {
        var checked = box.hasAttribute("data-ready");
        if (box.checked !== checked && box.form) {
          changedForms.add(box.form);
        }
        box.checked = checked;
      }
    });
    syncSelection(name);
    changedForms.forEach(function (form) {
      form.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });

  document.addEventListener("change", function (event) {
    var master = event.target.closest("[data-vr-select-all]");
    if (master) {
      var name = master.getAttribute("data-vr-select-all");
      rowBoxes(name, master.closest("table")).forEach(function (box) {
        if (!box.disabled) {
          box.checked = master.checked;
        }
      });
      syncSelection(name);
      return;
    }
    if (event.target.matches('input[type="checkbox"][name]')) {
      if (document.querySelector('[data-vr-select-all="' + event.target.name + '"]')) {
        syncSelection(event.target.name);
      }
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-vr-select-all]").forEach(function (master) {
      syncSelection(master.getAttribute("data-vr-select-all"));
    });
  });

  // Long checkbox/radio lists (more than ten options) get a filter box. Presentation only: hidden
  // options keep their checked state and are still submitted.
  function addChoiceFilter(list) {
    var options = Array.prototype.slice.call(list.querySelectorAll(".form-check"));
    if (options.length <= 10 || list.previousElementSibling && list.previousElementSibling.matches(".vr-choice-filter")) {
      return;
    }
    var legend = list.closest("fieldset") && list.closest("fieldset").querySelector("legend");
    var name = legend ? legend.textContent.replace(/\*/g, "").trim().toLowerCase() : "options";
    var wrap = document.createElement("div");
    wrap.className = "vr-choice-filter";
    var input = document.createElement("input");
    input.type = "search";
    input.className = "form-control form-control-sm";
    input.placeholder = "Filter " + name;
    input.setAttribute("aria-label", "Filter " + name);
    input.autocomplete = "off";
    var status = document.createElement("span");
    status.className = "form-text";
    status.setAttribute("role", "status");
    wrap.append(input, status);
    list.parentNode.insertBefore(wrap, list);
    input.addEventListener("input", function () {
      var terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      var shown = 0;
      options.forEach(function (option) {
        var text = option.textContent.toLowerCase();
        var match = terms.every(function (term) { return text.indexOf(term) !== -1; });
        option.hidden = !match;
        shown += match ? 1 : 0;
      });
      status.textContent = terms.length ? shown + " of " + options.length + " shown; selections are kept." : "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".vr-choices").forEach(addChoiceFilter);
  });

  // Column priority on tables without a Columns control (record sub-tables): the cells of a column
  // whose header carries data-vr-col-priority="low" are marked, and app.css hides them below 1366px,
  // so the table fits; the full data stays on the record. Lists (data-vr-grid) use grid.js instead.
  function markLowPriorityColumns(scope) {
    (scope || document).querySelectorAll(".vr-table:not([data-vr-grid])").forEach(function (table) {
      var head = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
      if (!head || table.dataset.vrPriorityReady === "true") {
        return;
      }
      var low = [];
      var position = 0;
      Array.from(head.cells).forEach(function (cell) {
        if (cell.getAttribute("data-vr-col-priority") === "low") {
          low.push(position);
          cell.setAttribute("data-vr-low", "");
        }
        position += cell.colSpan || 1;
      });
      if (!low.length) {
        return;
      }
      table.dataset.vrPriorityReady = "true";
      Array.from(table.tBodies).concat(table.tFoot ? [table.tFoot] : []).forEach(function (section) {
        Array.from(section.rows).forEach(function (row) {
          var column = 0;
          Array.from(row.cells).forEach(function (cell) {
            if ((cell.colSpan || 1) === 1 && low.indexOf(column) !== -1) {
              cell.setAttribute("data-vr-low", "");
            }
            column += cell.colSpan || 1;
          });
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () { markLowPriorityColumns(document); });
  document.addEventListener("htmx:load", function (event) { markLowPriorityColumns(event.detail.elt); });

  // Phones show each table row as a stacked card (app.css, "Stacked rows"): every body cell
  // carries its column's header text in data-vr-label, which the card prints before the value.
  // Runs before grid.js, so the label travels with the cell when columns are hidden or reordered.
  // A header with no visible text (an actions column) gives no label. Tables marked
  // .vr-table-scroll (entry grids, genuinely wide data) keep sideways scrolling instead.
  function labelTableCells(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var tables = Array.from(scope.querySelectorAll("table.vr-table"));
    var around = scope.closest ? scope.closest("table.vr-table") : null;
    if (around) {
      tables.push(around); // rows added to a table already on the page
    }
    tables.filter(function (table) { return !table.classList.contains("vr-table-scroll"); }).forEach(function (table) {
      var head = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
      if (!head) {
        return;
      }
      var labels = [];
      Array.from(head.cells).forEach(function (cell) {
        var text = (cell.textContent || "").replace(/\s+/g, " ").trim();
        for (var span = 0; span < (cell.colSpan || 1); span += 1) {
          labels.push(span === 0 ? text : "");
        }
      });
      Array.from(table.tBodies).forEach(function (section) {
        Array.from(section.rows).forEach(function (row) {
          var column = 0;
          Array.from(row.cells).forEach(function (cell) {
            if (labels[column] && !cell.hasAttribute("data-vr-label")) {
              cell.setAttribute("data-vr-label", labels[column]);
            }
            column += cell.colSpan || 1;
          });
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () { labelTableCells(document); });
  document.addEventListener("htmx:load", function (event) { labelTableCells(event.detail.elt); });

  // Phones collapse a list's toolbar behind one "Filter and sort" button that opens a bottom sheet
  // (includes/grid_toolbar.html, includes/sheet.html, app.css "Sheets"). The server renders the
  // filters and the filter half of the summary; this fills the sheet's "Sort by" section with copies
  // of the table's own sort links (the same server-built links, the current one marked
  // aria-current) and appends "Sorted by <column>" to the summary. With the copies in place the
  // table's own "Sort by" row steps aside on phones (data-vr-sort-sheet on its card); without
  // script that row stays. The table is the one in the toolbar's card, matched by its grid key.
  function filterTable(filter) {
    var card = filter.closest(".vr-table-card") || document;
    var grid = filter.getAttribute("data-vr-filter-grid");
    var table = grid ? card.querySelector('table[data-vr-grid="' + grid + '"]') : null;
    return table || card.querySelector("table.vr-table");
  }

  function refreshFilterSort(filter) {
    var card = filter.closest(".vr-table-card");
    var slot = filter.querySelector("[data-vr-sort-slot]");
    var state = filter.querySelector("[data-vr-sort-state]");
    var table = filterTable(filter);
    var heads = table && table.tHead ? Array.from(table.tHead.querySelectorAll("th")).filter(function (th) {
      return th.querySelector(".vr-sort");
    }) : [];
    if (slot) {
      var old = slot.querySelector(".vr-sort-chips");
      if (old) {
        old.remove();
      }
    }
    if (state) {
      state.textContent = "";
    }
    if (!heads.length || !slot) {
      slot && (slot.hidden = true);
      card && card.removeAttribute("data-vr-sort-sheet");
      return;
    }
    var list = document.createElement("ul");
    list.className = "vr-sort-chips";
    heads.forEach(function (th) {
      var link = th.querySelector(".vr-sort").cloneNode(true);
      var direction = th.getAttribute("aria-sort");
      link.removeAttribute("draggable");
      if (direction) {
        link.setAttribute("aria-current", "true");
        link.setAttribute("data-vr-sort-direction", direction);
        var note = document.createElement("span");
        note.className = "visually-hidden";
        note.textContent = ", sorted " + direction;
        link.append(note);
        if (state) {
          var label = (th.getAttribute("data-vr-col-label") || link.textContent).replace(/\s+/g, " ").trim();
          state.append(document.createTextNode(" \u00b7 Sorted by " + label + " "));
          state.insertAdjacentHTML("beforeend", window.vrUi.icon(direction === "ascending" ? "arrow-up" : "arrow-down"));
          var spoken = document.createElement("span");
          spoken.className = "visually-hidden";
          spoken.textContent = direction;
          state.append(spoken);
        }
      }
      var item = document.createElement("li");
      item.append(link);
      list.append(item);
    });
    slot.append(list);
    slot.hidden = false;
    if (card) {
      card.setAttribute("data-vr-sort-sheet", "");
    }
  }

  function initFilterSheets() {
    document.querySelectorAll(".vr-grid-filter").forEach(refreshFilterSort);
  }

  // After the other scripts' DOMContentLoaded work (grid.js hides and orders the columns).
  document.addEventListener("DOMContentLoaded", function () { window.setTimeout(initFilterSheets, 0); });
  document.addEventListener("htmx:load", initFilterSheets);

  // Bootstrap's offcanvas leaves the opening button's aria-expanded to the page.
  function syncSheetButtons(event, expanded) {
    var sheet = event.target;
    if (!sheet.classList || !sheet.classList.contains("vr-sheet")) {
      return;
    }
    document.querySelectorAll('[data-bs-toggle="offcanvas"][aria-controls="' + sheet.id + '"]').forEach(function (button) {
      button.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
  }

  document.addEventListener("show.bs.offcanvas", function (event) {
    syncSheetButtons(event, true);
    var filter = event.target.closest && event.target.closest(".vr-grid-filter");
    if (filter) {
      refreshFilterSort(filter); // columns may have changed in the Columns dialog
    }
  });
  document.addEventListener("hidden.bs.offcanvas", function (event) { syncSheetButtons(event, false); });

  // A table scrolled sideways shows its frozen identifier column's edge shadow (.is-scrolled-x).
  document.addEventListener("scroll", function (event) {
    var wrap = event.target;
    if (wrap.classList && wrap.classList.contains("vr-table-wrap")) {
      wrap.classList.toggle("is-scrolled-x", wrap.scrollLeft > 0);
    }
  }, true);

  // Native focus scrolling can place entry-grid fields behind a sticky row identifier.
  // Use its actual width, including enlarged text, rather than assuming a column size.
  document.addEventListener("focusin", function (event) {
    var control = event.target;
    var wrap = control.closest(".vr-table-wrap");
    var row = wrap && control.closest("tr");
    var identifier = row && row.querySelector('th[scope="row"]:first-child');
    if (!identifier || identifier.contains(control) || getComputedStyle(identifier).position !== "sticky") {
      return;
    }
    var overlap = identifier.getBoundingClientRect().right - control.getBoundingClientRect().left;
    if (overlap > 0) {
      wrap.scrollLeft -= overlap + 4;
    }
  });

  // After a failed submission, focus the first invalid field or the form-level error.
  document.addEventListener("DOMContentLoaded", function () {
    var invalid = document.querySelector("form [aria-invalid=true]");
    if (invalid && invalid.tagName === "FIELDSET") {
      invalid = invalid.querySelector("input, select, textarea");
    }
    if (invalid && invalid.tomselect) {
      invalid = invalid.tomselect.focus_node; // Enhanced select: the native one is hidden.
    }
    if (invalid) {
      invalid.focus();
    } else {
      var error = document.querySelector(".vr-main .alert-danger[role=alert], .vr-entrance-panel .alert-danger[role=alert], .vr-messages [role=alert]");
      if (error) {
        error.setAttribute("tabindex", "-1");
        error.focus();
      }
    }
  });

  // Native POST forms: block double submission while the request is in flight.
  // Buttons are disabled after the submit event so their name/value is still sent.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (event.defaultPrevented || method !== "post" || form.hasAttribute("hx-post")) {
      return;
    }
    if (form.dataset.vrSubmitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.vrSubmitting = "true";
    form.setAttribute("aria-busy", "true");
    var submitter = event.submitter;
    window.setTimeout(function () {
      form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (button) {
        button.disabled = true;
      });
      // Release, import and export actions render or parse on the server and can take seconds:
      // show that the request is running, and announce it, instead of only greying the button.
      if (submitter && submitter.tagName === "BUTTON") {
        var spinner = document.createElement("span");
        spinner.className = "spinner-border spinner-border-sm me-2 vr-busy-mark";
        spinner.setAttribute("aria-hidden", "true");
        submitter.prepend(spinner);
        var status = document.createElement("span");
        status.className = "visually-hidden vr-busy-mark";
        status.setAttribute("role", "status");
        status.textContent = "Working. Please wait.";
        form.append(status);
      }
    }, 0);
  });

  // Back/forward cache restores the disabled state; re-enable on return.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) {
      return;
    }
    document.querySelectorAll("form[data-vr-submitting]").forEach(function (form) {
      delete form.dataset.vrSubmitting;
      form.removeAttribute("aria-busy");
      document.querySelectorAll(".vr-busy-mark").forEach(function (mark) { mark.remove(); });
      form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (button) {
        button.disabled = false;
      });
    });
  });

  // Revealed secrets (a person-level key, links/_revealed_key.html) never outlive the page view.
  // A cell marked [data-vr-forget-cell] keeps the masked content it was loaded with; once it holds
  // something marked [data-vr-forget], leaving the page puts the masked content back, before the
  // browser stores the page in its back-forward cache (pagehide), and again if a restored page
  // still shows it (pageshow with persisted). A cell loaded already revealed (the no-JavaScript
  // reveal page) falls back to its data-vr-forget-text. Back and Forward restored the
  // key from the cache with no new reveal or audit event.
  var FORGET_CELL = "[data-vr-forget-cell]";

  function rememberMasked(scope) {
    var root = scope && scope.querySelectorAll ? scope : document;
    var cells = Array.prototype.slice.call(root.querySelectorAll(FORGET_CELL));
    if (root.matches && root.matches(FORGET_CELL)) {
      cells.push(root);
    }
    cells.forEach(function (cell) {
      if (cell.vrMasked === undefined && !cell.querySelector("[data-vr-forget]")) {
        cell.vrMasked = cell.innerHTML;
      }
    });
  }

  function forgetRevealed() {
    document.querySelectorAll(FORGET_CELL).forEach(function (cell) {
      if (!cell.querySelector("[data-vr-forget]")) {
        return;
      }
      if (cell.vrMasked !== undefined) {
        cell.innerHTML = cell.vrMasked;
        if (window.htmx) {
          window.htmx.process(cell);
        }
      } else {
        cell.textContent = cell.getAttribute("data-vr-forget-text") || "";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () { rememberMasked(document); });
  document.addEventListener("htmx:load", function (event) { rememberMasked(event.detail.elt); });
  window.addEventListener("pagehide", forgetRevealed);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      forgetRevealed();
    }
  });

  // Django formsets: a button[data-formset-add="<prefix>"] appends a copy of
  // <template id="<prefix>-empty"> (the formset's empty_form) to #<prefix>-rows and keeps
  // TOTAL_FORMS accurate. "__num__" in the template becomes the 1-based row number.
  function initFormsetButton(button) {
    if (button.dataset.vrFormsetReady === "true") {
      return;
    }
    var prefix = button.dataset.formsetAdd;
    var total = document.querySelector("input[name='" + prefix + "-TOTAL_FORMS']");
    var max = document.querySelector("input[name='" + prefix + "-MAX_NUM_FORMS']");
    var template = document.getElementById(prefix + "-empty");
    var rows = document.getElementById(prefix + "-rows");
    if (!total || !template || !rows) {
      return;
    }
    button.dataset.vrFormsetReady = "true";
    function atLimit() {
      return max && parseInt(total.value, 10) >= parseInt(max.value, 10);
    }
    button.hidden = false;
    button.disabled = atLimit();
    button.addEventListener("click", function () {
      if (atLimit()) {
        return;
      }
      var index = parseInt(total.value, 10);
      var html = template.innerHTML.replace(/__prefix__/g, String(index)).replace(/__num__/g, String(index + 1));
      rows.insertAdjacentHTML("beforeend", html);
      if (window.htmx) {
        window.htmx.process(rows.lastElementChild);
        window.htmx.trigger(rows.lastElementChild, "htmx:load");
      }
      var form = button.form || button.closest("form");
      if (form) {
        form.dispatchEvent(new Event("change", { bubbles: true }));
      }
      total.value = String(index + 1);
      button.disabled = atLimit();
      var added = rows.lastElementChild;
      var first = added && added.querySelector("input:not([type=hidden]), select, textarea");
      if (first && first.tomselect) {
        first = first.tomselect.focus_node; // Enhanced select: the native one is hidden.
      }
      if (first) {
        first.focus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-formset-add]").forEach(initFormsetButton);
  });

  document.addEventListener("htmx:load", function () {
    document.querySelectorAll("[data-vr-select-all]").forEach(function (master) {
      syncSelection(master.getAttribute("data-vr-select-all"));
    });
    document.querySelectorAll(".vr-choices").forEach(addChoiceFilter);
    document.querySelectorAll("[data-formset-add]").forEach(initFormsetButton);
  });

  // Print actions remain declarative so a strict script policy can stay enabled.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-vr-print]")) {
      window.print();
    }
  });

  // HTMX transport and server errors: keep the page as it is and say what happened, in a notice
  // where Django messages appear (docs/ui-standards.md section 8: no toasts). The notice markup is
  // server-rendered once in <template id="vr-request-error"> (layouts/document.html).
  function showRequestError(message) {
    var template = document.getElementById("vr-request-error");
    var main = document.querySelector(".vr-main");
    if (!template || !main) {
      return;
    }
    main.querySelectorAll("[data-vr-request-error]").forEach(function (old) { old.remove(); });
    var notice = template.content.firstElementChild.cloneNode(true);
    notice.querySelector("[data-vr-notice-text]").textContent = message;
    notice.setAttribute("tabindex", "-1");
    var region = main.querySelector(":scope > .vr-messages");
    if (!region) {
      region = document.createElement("div");
      region.className = "vr-messages";
      var header = main.querySelector(":scope > .vr-page-header");
      main.insertBefore(region, header ? header.nextSibling : main.firstChild);
    }
    region.prepend(notice);
    notice.focus();
  }

  document.addEventListener("htmx:responseError", function (event) {
    if (event.detail.target && event.detail.target.closest("#vr-form-dialog")) {
      return; // The form dialog keeps its error beside the preserved inputs.
    }
    var status = event.detail.xhr ? event.detail.xhr.status : "";
    var message = status === 403
      ? "You do not have permission for that action. Nothing was changed."
      : "The server could not complete the request (" + status + "). Your input was kept; try again.";
    showRequestError(message);
  });

  document.addEventListener("htmx:sendError", function (event) {
    if (event.detail.target && event.detail.target.closest("#vr-form-dialog")) {
      return;
    }
    showRequestError("The server could not be reached. Check the connection and try again.");
  });
})();
