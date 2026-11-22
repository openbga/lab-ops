// What was typed, kept across a sign-out by a session limit (accounts/kept.py,
// includes/kept_typing.html). A form drawn with keep=... carries a hidden input[data-vr-kept]: its
// value is the form's token, data-vr-kept-fields the names that may be kept.
//   - collect(): what is typed in those forms, for static/js/session.js to send once the session
//     has ended. A form is sent only when something in it differs from how the page drew it.
//     Nothing is written to the browser's storage.
//   - On a page drawn with a kept copy (data-vr-kept-values names its JSON), the values are put
//     back as if typed (input and change events, so the form's own scripts follow), then the
//     notice says so; a choice no longer offered is left as drawn and the notice says that too.
//     Discard returns the form to how the page drew it.
(function () {
  "use strict";

  function names(marker) {
    try {
      var list = JSON.parse(marker.getAttribute("data-vr-kept-fields") || "[]");
      return Array.isArray(list) ? list : [];
    } catch (error) {
      return [];
    }
  }

  function controls(form, name) {
    return Array.from(form.elements).filter(function (element) {
      return element.name === name && !element.disabled;
    });
  }

  function changed(element) {
    if (element.type === "checkbox" || element.type === "radio") {
      return element.checked !== element.defaultChecked;
    }
    if (element.tagName === "SELECT") {
      return Array.from(element.options).some(function (option) {
        return option.selected !== option.defaultSelected;
      });
    }
    return element.value !== element.defaultValue;
  }

  // One string per name, as a form posts it: the checked radio's value, a ticked box's value,
  // the chosen option, the text; "" for nothing.
  function valueOf(list) {
    var first = list[0];
    if (first.type === "radio" || first.type === "checkbox") {
      var checked = list.find(function (element) { return element.checked; });
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
      var values = {};
      var dirty = false;
      names(marker).forEach(function (name) {
        var list = controls(form, name);
        if (!list.length) {
          return;
        }
        dirty = dirty || list.some(changed);
        values[name] = valueOf(list);
      });
      if (dirty) {
        out.push({ token: marker.value, values: values });
      }
    });
    return out;
  }

  function fire(element) {
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Put one value back; false when it cannot be (a choice no longer offered).
  function put(list, value) {
    var first = list[0];
    if (first.type === "radio") {
      var radio = list.find(function (element) { return element.value === value; });
      if (radio) {
        radio.checked = true;
        fire(radio);
        return true;
      }
      return value === "";
    }
    if (first.type === "checkbox") {
      first.checked = value !== "" && value === first.value;
      fire(first);
      return value === "" || value === first.value;
    }
    if (first.tagName === "SELECT") {
      var offered = Array.from(first.options).some(function (option) { return option.value === value; });
      if (offered) {
        first.value = value;
        fire(first);
      }
      return offered;
    }
    first.value = value;
    fire(first);
    return true;
  }

  function restore(marker) {
    var form = marker.form;
    var source = document.getElementById(marker.getAttribute("data-vr-kept-values"));
    var notice = form.querySelector("[data-vr-kept-notice]");
    var values;
    try {
      values = JSON.parse(source ? source.textContent : "null");
    } catch (error) {
      values = null;
    }
    if (!values || typeof values !== "object" || !notice) {
      return;
    }
    var missed = false;
    var kept = names(marker);
    kept.forEach(function (name) {
      var list = controls(form, name);
      if (list.length && typeof values[name] === "string") {
        missed = !put(list, values[name]) || missed;
      }
    });
    var missedText = notice.querySelector("[data-vr-kept-missed]");
    if (missedText) {
      missedText.hidden = !missed;
    }
    notice.hidden = false;
    var discard = notice.querySelector("[data-vr-kept-discard]");
    if (discard) {
      discard.addEventListener("click", function () {
        form.reset();
        kept.forEach(function (name) {
          controls(form, name).forEach(fire);
        });
        notice.hidden = true;
        var first = kept.map(function (name) { return controls(form, name)[0]; }).find(Boolean);
        if (first && typeof first.focus === "function") {
          first.focus();
        }
      });
    }
  }

  window.vrKeptTyping = { collect: collect };

  // After the other scripts of the page have set up their controls (selects, the composer).
  function start() {
    setTimeout(function () {
      markers().forEach(function (marker) {
        if (marker.hasAttribute("data-vr-kept-values")) {
          restore(marker);
        }
      });
    }, 0);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
