// What was typed, kept across a sign-out by a session limit (accounts/kept.py,
// includes/kept_typing.html). A form drawn with keep=... carries a hidden input[data-vr-kept]: its
// value is the form's token, data-vr-kept-fields the names that may be kept, data-vr-kept-several
// those that hold a list of values (ticked boxes).
//   - collect({commit}): what is typed in those forms, for static/js/session.js to send once the
//     session has ended (with commit: a date editor writes what it shows to its field first). A form is sent when a field one types in holds text and either something in it
//     differs from how the page drew it or what the page drew is itself unsent (data-vr-kept-bound:
//     drawn again after a refused save, or with a copy given back). Nothing is written to the
//     browser's storage.
//   - A copy given back is drawn in the fields by the server; a field given back with a value that
//     the page leaves out (data-vr-kept-given) shows the sentence on what was not put back. Discard
//     opens the page again (the
//     copy is taken by then); this script lets it go without the composer's question about unsent
//     text (static/js/ticket-draft.js) and then puts the focus in the form's first text box.
//   - A marker drawn again with part of its form (the desk's new ticket draws it in the category
//     section, which htmx draws again with the ticket form's fields): the controls drawn with it
//     are compared with how they were drawn then, and a form that held unsent text when the page
//     was drawn (data-vr-kept-bound) still counts as unsent.
//   - A form in the shared form dialog (static/js/form-dialog.js) is noted as drawn once the dialog
//     has set it up (vr:form-ready); returnTo(held) gives the address of its page when a copy is
//     held for it (`held`: the keep answer's list, one per form collected), so that signing in
//     again opens that page, where the copy is given back (static/js/session.js); heldForms(held)
//     the forms a copy is held for. Discard inside the dialog loads the form again in place; the
//     focus then goes to its first text box.
(function () {
  "use strict";

  var DISCARDED = "#vr-kept-discarded";
  var drawn = new WeakMap(); // each kept control's state as the page (or htmx) drew it
  var unsent = new WeakSet(); // forms whose marker was drawn with unsent text

  function list(marker, name) {
    try {
      var value = JSON.parse(marker.getAttribute(name) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (error) {
      return [];
    }
  }

  // The form's controls named `name` that it would send; with `all` "busy", also those the form
  // dialog has disabled for a request (data-vr-busy-disabled); with `all` true, every one (how the
  // page drew them, while the dialog opens its form).
  function controls(form, name, all) {
    return Array.from(form.elements).filter(function (element) {
      if (element.name !== name) {
        return false;
      }
      if (!element.disabled || all === true) {
        return true;
      }
      return all === "busy" && element.hasAttribute("data-vr-busy-disabled");
    });
  }

  // Forms whose own save is under way (htmx posts them; the form dialog disables them meanwhile).
  var posting = new Set();
  function postedForm(event) {
    var detail = event.detail || {};
    var element = detail.elt;
    var verb = detail.requestConfig && detail.requestConfig.verb;
    if (!element || String(verb).toLowerCase() !== "post") {
      return null;
    }
    return element.tagName === "FORM" ? element : element.form || element.closest("form");
  }
  document.addEventListener("htmx:beforeRequest", function (event) {
    var form = postedForm(event);
    if (form) {
      posting.add(form);
    }
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    var form = postedForm(event);
    if (form) {
      posting.delete(form);
    }
  });

  function state(element) {
    if (element.type === "checkbox" || element.type === "radio") {
      return String(element.checked);
    }
    if (element.tagName === "SELECT") {
      return Array.from(element.options).map(function (option) { return option.selected ? "1" : "0"; }).join("");
    }
    return element.value;
  }

  function changed(element) {
    return drawn.has(element) && drawn.get(element) !== state(element);
  }

  // A date field's visible editor (static/js/controls.js moves the field's name to a hidden input).
  function dateEditor(element) {
    var wrapper = element.type === "hidden" && element.closest(".vr-date-input");
    var input = wrapper && wrapper.querySelector(".vr-date-text");
    return input && input.vrDateField ? input : null;
  }

  // A field one types in (a text box or a date), as opposed to a choice.
  function typedIn(element) {
    if (element.tagName === "TEXTAREA") {
      return true;
    }
    if (element.tagName !== "INPUT") {
      return false;
    }
    if (element.type === "hidden") {
      return Boolean(dateEditor(element));
    }
    return !/^(checkbox|radio|file|submit|button|reset|image)$/.test(element.type);
  }

  // As a form posts it: the checked radio's value, a ticked box's value, the chosen option, the
  // text; "" for nothing. A field of several values: the ticked values.
  function valueOf(elements, several) {
    var first = elements[0];
    if (several) {
      return elements.filter(function (element) { return element.checked; }).map(function (element) { return element.value; });
    }
    if (first.type === "radio" || first.type === "checkbox") {
      var checked = elements.find(function (element) { return element.checked; });
      return checked ? checked.value : "";
    }
    return first.value;
  }

  function markers() {
    return Array.from(document.querySelectorAll("input[data-vr-kept]")).filter(function (marker) {
      return marker.form && marker.value;
    });
  }

  var sent = []; // the forms collect() gave last, in order: {form, page}

  // The address of the form dialog's page (data-vr-form-url, this site's) of the first form
  // collect() gave last that a copy is held for (`held`, in the same order), or null.
  function returnTo(held) {
    var found = sent.find(function (item, index) { return item.page && held && held[index]; });
    return found ? found.page : null;
  }

  function heldForms(held) {
    return sent.filter(function (item, index) { return held && held[index]; }).map(function (item) {
      return item.form;
    });
  }

  function dialogPage(form) {
    var page = form.closest("#vr-form-content [data-vr-form-page]");
    var address = page && page.getAttribute("data-vr-form-url");
    if (!address) {
      return null;
    }
    var url = new URL(address, window.location.href);
    return url.origin === window.location.origin ? url.pathname + url.search : null;
  }

  // `commit`: before sending, a date editor still being typed in writes what it shows to its
  // field first (static/js/controls.js), as it would on leaving the field.
  function collect(options) {
    var commit = Boolean(options && options.commit);
    var out = [];
    sent = [];
    markers().forEach(function (marker) {
      var form = marker.form;
      var several = list(marker, "data-vr-kept-several");
      var values = {};
      var dirty = marker.hasAttribute("data-vr-kept-bound") || unsent.has(form);
      var text = false;
      // While the form's own save is under way the form dialog disables it: what it holds is
      // still what the person typed (not while the dialog loads another form over it, Discard).
      var busy = posting.has(form) ? "busy" : false;
      list(marker, "data-vr-kept-fields").forEach(function (name) {
        var elements = controls(form, name, busy);
        if (!elements.length) {
          return;
        }
        var editor = dateEditor(elements[0]);
        if (editor && commit) {
          editor.vrDateField.commit();
        }
        var value = valueOf(elements, several.indexOf(name) !== -1);
        if (editor && !commit && !value && editor.value.trim()) {
          value = editor.value; // shown but not yet written to the field: still typing
          dirty = true;
        }
        dirty = dirty || elements.some(changed);
        values[name] = value;
        text = text || (typedIn(elements[0]) && typeof value === "string" && value.trim() !== "");
      });
      if (dirty && text) {
        out.push({ token: marker.value, values: values });
        sent.push({ form: form, page: dialogPage(form) });
      }
    });
    return out;
  }

  function visible(element) {
    var box = element.getBoundingClientRect();
    return element.offsetParent !== null && box.width > 2 && box.height > 2;
  }

  // The first text box of the form's kept fields that can take the focus, else its first control.
  function focusForm(marker) {
    var elements = [];
    list(marker, "data-vr-kept-fields").forEach(function (name) {
      controls(marker.form, name).forEach(function (element) {
        elements.push(dateEditor(element) || element);
      });
    });
    var first = elements.find(function (element) { return typedIn(element) && visible(element); }) ||
      elements.find(visible);
    if (first) {
      first.focus();
    }
  }

  document.addEventListener("click", function (event) {
    var discard = event.target.closest && event.target.closest("a[data-vr-kept-discard]");
    if (!discard || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) {
      return;
    }
    event.preventDefault();
    document.dispatchEvent(new CustomEvent("vr:typing-discarded"));
    window.history.replaceState(null, "", discard.getAttribute("href") + DISCARDED);
    window.location.reload();
  });

  window.vrKeptTyping = { collect: collect, returnTo: returnTo, heldForms: heldForms };

  // Discard inside the form dialog: the dialog loads the form again in place (its own link
  // handling, which runs first); once it is set up, the focus goes to its first text box. Noted
  // on the window, whose capture listeners run before the document's.
  var discardedInDialog = false;
  window.addEventListener("click", function (event) {
    var target = event.target.closest ? event.target : null;
    var discard = target && target.closest("a[data-vr-kept-discard]");
    if (discard) {
      discardedInDialog = Boolean(discard.closest("#vr-form-content"));
    } else if (!(target && target.closest("#vr-form-discard"))) {
      discardedInDialog = false; // anything but the dialog's question about unsaved changes
    }
  }, true);
  document.addEventListener("vr:form-ready", function () {
    if (!discardedInDialog) {
      return;
    }
    discardedInDialog = false;
    setTimeout(function () {
      var marker = markers().find(function (item) { return item.closest("#vr-form-content"); });
      if (marker) {
        focusForm(marker);
      }
    }, 0);
  });

  // How each kept control was drawn, for those not seen yet (the page's, then those htmx draws).
  function baseline() {
    // A form drawn with unsent text, also one whose marker has no token (a copy given back into
    // a form that can no longer be marked).
    document.querySelectorAll("input[data-vr-kept][data-vr-kept-bound]").forEach(function (marker) {
      if (marker.form) {
        unsent.add(marker.form);
      }
    });
    markers().forEach(function (marker) {
      list(marker, "data-vr-kept-fields").forEach(function (name) {
        controls(marker.form, name, true).forEach(function (element) {
          if (!drawn.has(element)) {
            drawn.set(element, state(element));
          }
        });
      });
    });
  }

  // After htmx has drawn part of a form again, or the form dialog has set up its form, and the
  // page's scripts have set up its controls.
  ["htmx:afterSettle", "vr:form-ready"].forEach(function (name) {
    document.addEventListener(name, function () {
      setTimeout(baseline, 0);
    });
  });

  // After the other scripts of the page have set up their controls (selects, dates, the composer).
  function start() {
    setTimeout(function () {
      baseline();
      // A field given back with a value that this page does not show: say it was not put back.
      // A sentence drawn only for dates handed to the date editor as text goes once the editor
      // shows them (static/js/controls.js reads data-vr-date-text).
      markers().forEach(function (marker) {
        var missed = marker.form.querySelector("[data-vr-kept-missed]");
        if (!missed) {
          return;
        }
        var absent = list(marker, "data-vr-kept-given").some(function (name) {
          return !controls(marker.form, name, true).length;
        });
        if (absent) {
          missed.hidden = false;
        } else if (missed.hasAttribute("data-vr-kept-dates-only")) {
          var shown = Array.from(marker.form.querySelectorAll("[data-vr-date-text]")).every(function (input) {
            return input.vrDateField && input.value.trim() !== "";
          });
          if (shown) {
            missed.hidden = true;
          }
        }
      });
      if (window.location.hash === DISCARDED) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
        var marker = markers()[0];
        if (marker) {
          focusForm(marker);
        }
      }
    }, 0);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
