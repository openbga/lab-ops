// A ticket's unsent message and selected files stay in this document only.
(function () {
  "use strict";

  var composer = document.querySelector("form.vr-composer");
  if (!composer) {
    return;
  }

  function hasDraft() {
    return Array.from(composer.elements).some(function (field) {
      return /^(body|files|files-previous|contact_direction|contact_channel|contact_with|contact_outcome|new_status)$/.test(field.name) && Boolean(field.value);
    });
  }

  function allowDiscard() {
    return window.confirm("Discard the unsent ticket entry and its selected files to continue?");
  }

  var leaving = false;
  window.addEventListener("beforeunload", function (event) {
    if (!leaving && hasDraft()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pageshow", function () { leaving = false; });
  // The session ended and what was typed is kept (static/js/session.js, accounts/kept.py): the
  // page goes to the sign-in page without asking (chosen files are never kept).
  // Only when the composer's own typing is among what was kept.
  document.addEventListener("vr:typing-kept", function (event) {
    var forms = event.detail && event.detail.forms;
    if (Array.isArray(forms) && forms.indexOf(composer) !== -1) {
      leaving = true;
    }
  });
  // Discard on a form given back what was typed opens the page again (static/js/kept-typing.js).
  document.addEventListener("vr:typing-discarded", function () { leaving = true; });

  function nativePost(form) {
    return form.method === "post" && !form.hasAttribute("hx-post");
  }

  // Ask before native actions such as Take it, before the shared double-submit guard
  // marks the form busy. Declining keeps both the draft and the action button usable.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form !== composer && nativePost(form) && hasDraft() && !allowDiscard()) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  document.addEventListener("submit", function (event) {
    if (nativePost(event.target) && !event.defaultPrevented) {
      leaving = true;
    }
  });

  // A dialog save may redirect the whole page. A refused save or an inline operation
  // such as revealing a key must keep the draft, its files, and navigation protection.
  var confirmed = new WeakSet();
  document.addEventListener("htmx:beforeRequest", function (event) {
    var request = event.detail;
    if (request.target.id !== "vr-form-content" || request.requestConfig.verb !== "post" || !hasDraft()) {
      return;
    }
    if (allowDiscard()) {
      confirmed.add(request.xhr);
    } else {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  document.addEventListener("htmx:beforeOnLoad", function (event) {
    var xhr = event.detail.xhr;
    if (confirmed.has(xhr) && xhr.getResponseHeader("HX-Redirect")) {
      leaving = true;
    }
  });
})();
