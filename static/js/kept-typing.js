// What was typed, kept across a sign-out by a session limit (accounts/kept.py,
// includes/kept_typing.html). A form drawn with keep=... carries a hidden input[data-vr-kept]: its
// value is the form's token, data-vr-kept-fields the names that may be kept, data-vr-kept-several
// those that hold a list of values (ticked boxes).
//   - collect(): what is typed in those forms, for static/js/session.js to send once the session
//     has ended. A form is sent when a field one types in holds text and either something in it
//     differs from how the page drew it or what the page drew is itself unsent (data-vr-kept-bound:
//     drawn again after a refused save, or with a copy given back). Nothing is written to the
//     browser's storage.
//   - A copy given back is drawn in the fields by the server; a field given back with a value that
//     the page leaves out (data-vr-kept-given) shows the sentence on what was not put back. Discard
//     opens the page again (the
//     copy is taken by then); this script lets it go without the composer's question about unsent
//     text (static/js/ticket-draft.js) and then puts the focus in the form's first text box.
(function () {
  "use strict";

  var DISCARDED = "#vr-kept-discarded";
  var drawn = new Map(); // each kept control's state as the page drew it

  function list(marker, name) {
    try {
      var value = JSON.parse(marker.getAttribute(name) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (error) {
      return [];
    }
  }

  function controls(form, name) {
    return Array.from(form.elements).filter(function (element) {
      return element.name === name && !element.disabled;
    });
  }

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

  function collect() {
    var out = [];
    markers().forEach(function (marker) {
      var form = marker.form;
      var several = list(marker, "data-vr-kept-several");
      var values = {};
      var dirty = marker.hasAttribute("data-vr-kept-bound");
      var text = false;
      list(marker, "data-vr-kept-fields").forEach(function (name) {
        var elements = controls(form, name);
        if (!elements.length) {
          return;
        }
        var value = valueOf(elements, several.indexOf(name) !== -1);
        dirty = dirty || elements.some(changed);
        values[name] = value;
        text = text || (typedIn(elements[0]) && typeof value === "string" && value.trim() !== "");
      });
      if (dirty && text) {
        out.push({ token: marker.value, values: values });
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

  window.vrKeptTyping = { collect: collect };

  // After the other scripts of the page have set up their controls (selects, dates, the composer).
  function start() {
    setTimeout(function () {
      markers().forEach(function (marker) {
        list(marker, "data-vr-kept-fields").forEach(function (name) {
          controls(marker.form, name).forEach(function (element) {
            drawn.set(element, state(element));
          });
        });
      });
      // A field given back with a value that this page does not show: say it was not put back.
      markers().forEach(function (marker) {
        var missed = marker.form.querySelector("[data-vr-kept-missed]");
        var absent = list(marker, "data-vr-kept-given").some(function (name) {
          return !controls(marker.form, name).length;
        });
        if (missed && absent) {
          missed.hidden = false;
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
