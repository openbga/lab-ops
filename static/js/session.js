// The warning before automatic sign-out (accounts/sessions.py, includes/session_warning.html).
// Times are kept on this page's clock, from the moment each answer arrived (the page's own numbers
// from when the script starts), never compared with the server's clock. Before it warns, the page
// asks how long remains (the status address, which is not activity): another tab may have been
// active. Stay signed in is activity; nothing extends the longest session. The server decides;
// the page only warns, and goes to the sign-in page once the server says the session has ended.
// Before it goes, it sends what is typed in forms that keep it (static/js/kept-typing.js) to the
// keep address, which keeps a copy if the session ended by a limit (accounts/kept.py).
(function () {
  "use strict";

  var RECHECK_MS = 15000; // while a warning is open, so Stay signed in in another tab closes it here
  // While no warning is open and the page is in view: a limit an administrator shortened, or
  // activity this page's own scripts sent, is learned within a minute.
  var IDLE_RECHECK_MS = 60000;
  var LAST_SECOND_MS = 1000; // at zero while the server still counts the last (whole) second
  var FAILED_RETRY_MS = 2000; // a status request at zero that got no answer
  var GIVE_UP_MS = 5000; // past zero with no answer: the server has ended the session by now
  var REQUEST_TIMEOUT_MS = 15000;
  var KEEP_TIMEOUT_MS = 8000; // sending what was typed, before going to the sign-in page
  var LONGEST_DELAY_MS = 2147483647; // setTimeout's limit
  var REQUEST_FAILED = "The request could not be completed. Check the connection and try again.";

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || "";
    } catch (error) {
      return "";
    }
  }

  function clockText(seconds) {
    var rest = seconds % 60;
    return Math.floor(seconds / 60) + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function milliseconds(value) {
    var number = Number(value);
    return value !== "" && value !== null && isFinite(number) && number >= 0 ? number * 1000 : NaN;
  }

  function init() {
    var config = document.querySelector("[data-vr-session]");
    var element = document.querySelector("[data-vr-session-warning]");
    if (!config || !element || !window.bootstrap || !window.fetch || !window.AbortController) {
      return; // No warning; the server still signs the session out.
    }
    var settings = config.dataset;
    var modal = window.bootstrap.Modal.getOrCreateInstance(element, { backdrop: "static", keyboard: false });
    var parts = Array.from(element.querySelectorAll("[data-vr-session-for]"));
    var countdowns = Array.from(element.querySelectorAll("[data-vr-session-countdown]"));
    var stayButton = element.querySelector("[data-vr-session-stay]");
    var closeButton = element.querySelector("[data-vr-session-close]");
    var errorRegion = element.querySelector("[data-vr-session-error]");
    var keptLines = Array.from(element.querySelectorAll("[data-vr-session-kept]"));
    var keeping = null; // what was typed, on its way to be kept (sent once)
    var returnTo = null; // the form dialog's page whose typing was kept, to open after signing in
    var times = null; // the two endings and the two leads, in ms on this page's Date.now() clock
    var variant = null; // the warning wanted on screen: "inactivity", "maximum" or null
    var state = "hidden"; // Bootstrap's: hidden, showing, shown or hiding
    var dismissed = false; // Close on the longest-session warning: it is not shown again
    var leaving = false;
    var expiring = false;
    var sending = false;
    var opener = null;
    var timer = 0;
    var ticker = 0;
    var poller = 0;
    var checking = null;
    var sent = 0; // requests are numbered; an answer to one sent before the answer in use is stale
    var used = 0;

    function measure(answer) {
      var now = Date.now();
      var next = {
        inactivityEnd: now + milliseconds(answer.inactivity_seconds),
        maximumEnd: now + milliseconds(answer.maximum_seconds),
        warning: milliseconds(answer.warning_seconds),
        longestWarning: milliseconds(answer.longest_warning_seconds),
      };
      if (Object.keys(next).some(function (key) { return isNaN(next[key]); })) {
        return false;
      }
      if (times && Math.abs(next.maximumEnd - times.maximumEnd) > 60000) {
        dismissed = false; // Another session (signed in again in this browser): its own warning.
      }
      times = next;
      return true;
    }

    function ending() {
      return Math.min(times.inactivityEnd, times.maximumEnd);
    }

    // The warning due now, or null. When both are due, the one that ends first; when both end at
    // the same moment, the longest session's, as the server decides (activity cannot extend it).
    // The inactivity warning does not matter once the longest session ends no later than it.
    function wanted(now) {
      var maximumDue = !dismissed && now >= times.maximumEnd - times.longestWarning;
      if (times.inactivityEnd < times.maximumEnd && now >= times.inactivityEnd - times.warning) {
        return "inactivity";
      }
      return maximumDue ? "maximum" : null;
    }

    function nextChange(now) {
      var next = ending();
      [times.inactivityEnd - times.warning, dismissed ? Infinity : times.maximumEnd - times.longestWarning].forEach(function (at) {
        if (at > now && at < next) {
          next = at;
        }
      });
      return next;
    }

    function typed(options) {
      return window.vrKeptTyping ? window.vrKeptTyping.collect(options) : [];
    }

    // Send what is typed in forms that keep it, once; resolves when answered or given up. The
    // session has ended by now, so the request needs none: each form's token names it. Kept, the
    // page may go without asking about its unsent text (static/js/ticket-draft.js).
    function keep() {
      if (keeping) {
        return keeping;
      }
      var forms = typed({ commit: true });
      if (!forms.length || !settings.keepUrl) {
        keeping = Promise.resolve();
        return keeping;
      }
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, KEEP_TIMEOUT_MS);
      keeping = fetch(settings.keepUrl, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ forms: forms }),
        signal: controller.signal,
      }).then(function (response) {
        return response.ok ? response.json() : null;
      }).then(function (answer) {
        clearTimeout(timeout);
        if (answer && answer.kept > 0) {
          // One answer per form sent. An earlier release answers with the count only: every form
          // is known held only when the count covers them all.
          var all = answer.kept >= forms.length;
          var held = Array.isArray(answer.held) ? answer.held : forms.map(function () { return all; });
          var typing = window.vrKeptTyping;
          // Held for a form in the form dialog: signing in again opens that form's page.
          returnTo = typing ? typing.returnTo(held) : null;
          document.dispatchEvent(new CustomEvent("vr:typing-kept", {
            detail: { kept: answer.kept, forms: typing ? typing.heldForms(held) : [] },
          }));
        }
      }).catch(function () {
        clearTimeout(timeout);
      });
      return keeping;
    }

    // The server's sign-in address from a refusal (accounts.sessions: it returns to the form a post
    // was kept from), rebuilt on this site's sign-in page with its `next` when that is a path on
    // this site; else null (an address that cannot be read included).
    function serverSignIn(address) {
      if (typeof address !== "string" || !address) {
        return null;
      }
      var url;
      var own;
      try {
        url = new URL(address, window.location.href);
        own = new URL(settings.signInUrl, window.location.href);
      } catch (error) {
        return null;
      }
      var next = url.searchParams.get("next") || "";
      if (url.origin !== own.origin || url.pathname !== own.pathname || !/^\/(?![\/\\])/.test(next)) {
        return null;
      }
      return settings.signInUrl + "?next=" + encodeURIComponent(next);
    }

    // `signIn`: the sign-in address the server gave with a refusal, used unless a form dialog's
    // copy decides where to return.
    function leave(signIn) {
      clearTimeout(timer);
      clearTimeout(ticker);
      stopPoller();
      // Again on a later call: a page that asked before unloading (unsaved form) may still be here.
      leaving = true;
      keep().then(function () {
        var fallback = serverSignIn(signIn);
        if (!returnTo && fallback) {
          window.location.replace(fallback);
          return;
        }
        var next = returnTo || window.location.pathname + window.location.search;
        window.location.replace(settings.signInUrl + "?next=" + encodeURIComponent(next));
      });
    }

    function accept(answer, number) {
      if (!answer || typeof answer.signed_in !== "boolean") {
        return null;
      }
      if (!answer.signed_in) {
        leave(); // Ended (by time, elsewhere, or by the status request itself): the server's notice waits.
        return answer;
      }
      if (number < used) {
        return answer; // Asked before the answer in use (a Stay signed in answered meanwhile).
      }
      if (!measure(answer)) {
        return null;
      }
      used = number;
      return answer;
    }

    // A same-origin JSON request; resolves to the accepted answer, or null when none came.
    function request(method, url) {
      var number = ++sent;
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
      var headers = { Accept: "application/json" };
      if (method === "POST") {
        headers["X-CSRFToken"] = csrfToken();
      }
      return fetch(url, {
        method: method, credentials: "same-origin", cache: "no-store", headers: headers, signal: controller.signal,
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      }).then(function (answer) {
        clearTimeout(timeout);
        return accept(answer, number);
      }).catch(function () {
        clearTimeout(timeout);
        return null;
      });
    }

    // `fresh`: an answer to a question already on its way may have been read before something
    // this page did (a Stay signed in whose reply was lost), so wait for it and ask again.
    function check(fresh) {
      if (checking && fresh) {
        return checking.then(function () { return check(false); });
      }
      if (!checking) {
        checking = request("GET", settings.statusUrl).then(function (answer) {
          checking = null;
          return answer;
        });
      }
      return checking;
    }

    function plan(fresh) {
      clearTimeout(timer);
      if (leaving || !times) {
        return;
      }
      var now = Date.now();
      if (now >= ending()) {
        expire();
        return;
      }
      var want = wanted(now);
      if (want && !variant && !fresh) {
        check().then(function () { plan(true); }); // Without an answer it warns all the same.
        return;
      }
      setVariant(want);
      timer = setTimeout(function () { plan(false); }, Math.min(nextChange(now) - now, LONGEST_DELAY_MS));
    }

    // The countdown reached zero: ask, so the server ends the session and keeps its notice for the
    // sign-in page, and so a session kept open elsewhere goes on here.
    function expire() {
      if (expiring) {
        return;
      }
      expiring = true;
      check().then(function (answer) {
        expiring = false;
        if (leaving) {
          return;
        }
        var now = Date.now();
        if (answer && now < ending()) {
          plan(true);
        } else if (!answer && now - ending() > GIVE_UP_MS) {
          leave();
        } else {
          clearTimeout(timer);
          timer = setTimeout(expire, answer ? LAST_SECOND_MS : FAILED_RETRY_MS);
        }
      });
    }

    function remaining() {
      return (variant === "maximum" ? times.maximumEnd : times.inactivityEnd) - Date.now();
    }

    // m:ss, changed as each second passes. Not a live region: the dialog's description is read
    // once when it opens.
    function tick() {
      clearTimeout(ticker);
      if (!variant) {
        return;
      }
      var holds = typed().length > 0;
      keptLines.forEach(function (line) {
        if (line.hidden === holds) {
          line.hidden = !holds;
        }
      });
      var left = Math.max(0, remaining());
      var text = clockText(Math.ceil(left / 1000));
      countdowns.forEach(function (countdown) {
        if (countdown.textContent !== text) {
          countdown.textContent = text;
        }
      });
      if (left > 0) {
        ticker = setTimeout(tick, (left % 1000 || 1000) + 20);
      }
    }

    function startPoller() {
      if (!poller) {
        poller = setInterval(recheck, RECHECK_MS); // with its short retry when unanswered
      }
    }

    function stopPoller() {
      clearInterval(poller);
      poller = 0;
    }

    function setVariant(want) {
      if (want === variant) {
        tick();
        sync();
        return;
      }
      var switching = Boolean(variant && want);
      variant = want;
      hideError(); // an error belongs to the warning it was shown in
      if (want) {
        parts.forEach(function (part) {
          part.hidden = part.getAttribute("data-vr-session-for") !== want;
        });
        tick();
        startPoller();
        if (switching && state === "shown") {
          focusAction();
        }
      } else {
        clearTimeout(ticker);
        stopPoller();
      }
      sync();
    }

    // Bring Bootstrap's dialog to the wanted state; the end of a transition calls this again.
    function sync() {
      if (state === "showing" || state === "hiding") {
        return;
      }
      if (variant && state === "hidden") {
        opener = document.activeElement;
        takeBack();
        modal.show();
      } else if (!variant && state === "shown") {
        modal.hide();
      }
    }

    // An open form dialog (form-dialog.js) makes the rest of the page inert and renames its ids;
    // this warning opens over it, so it takes itself back (the form dialog restores both later).
    function takeBack() {
      element.inert = false;
      [element].concat(Array.from(element.querySelectorAll("[id]"))).forEach(function (node) {
        var id = node.id.replace(/^vr-background-\d+-/, "");
        if (id !== node.id) {
          node.id = id;
        }
      });
    }

    // Bootstrap does not stack dialogs: showing this one took the focus trap of a dialog or sheet
    // still open beneath it, and closing it ended that one's scroll lock marker. Give them back.
    function restoreBeneath() {
      var others = Array.from(document.querySelectorAll(".modal.show, .offcanvas.show, .offcanvas-sm.show, .offcanvas-lg.show")).filter(function (other) {
        return other !== element;
      });
      var beneath = others.find(function (other) { return other.classList.contains("vr-confirm-dialog"); }) || others[0];
      var instance = beneath && (window.bootstrap.Modal.getInstance(beneath) || window.bootstrap.Offcanvas.getInstance(beneath));
      if (beneath && beneath.classList.contains("modal")) {
        document.body.classList.add("modal-open");
      }
      if (instance && instance._focustrap) {
        instance._focustrap.deactivate();
        instance._focustrap.activate();
      }
    }

    function focusAction() {
      var action = variant === "maximum" ? closeButton : stayButton;
      if (action) {
        action.focus();
      }
    }

    function showError(message) {
      errorRegion.querySelector("[data-vr-notice-text]").textContent = message;
      errorRegion.hidden = false;
    }

    function hideError() {
      errorRegion.hidden = true;
    }

    // Bootstrap sets role="dialog" whenever it shows a modal; this one is an alert dialog. The
    // observer runs before the focus moves in, so the dialog is announced once, as an alert dialog.
    new MutationObserver(function () {
      if (element.getAttribute("role") === "dialog") {
        element.setAttribute("role", "alertdialog");
      }
    }).observe(element, { attributes: true, attributeFilter: ["role"] });

    element.addEventListener("show.bs.modal", function () {
      state = "showing";
    });
    element.addEventListener("shown.bs.modal", function () {
      state = "shown";
      if (variant) {
        focusAction();
      }
      sync();
    });
    element.addEventListener("hide.bs.modal", function (event) {
      if (variant) {
        event.preventDefault(); // Only this script closes the warning.
        return;
      }
      state = "hiding";
    });
    element.addEventListener("hidden.bs.modal", function () {
      state = "hidden";
      hideError();
      restoreBeneath();
      if (opener && opener.isConnected && typeof opener.focus === "function") {
        opener.focus();
      }
      opener = null;
      sync();
    });

    // Escape closes the longest-session warning, as Close does. The inactivity warning waits for
    // a choice (closing it would let the session end unseen) and keeps the focus where it is:
    // Bootstrap's own answer for a static dialog would move it to the dialog.
    element.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && variant) {
        event.preventDefault();
        event.stopPropagation();
        if (variant === "maximum") {
          closeButton.click();
        }
      }
    }, true);

    stayButton.addEventListener("click", function () {
      if (leaving) {
        leave();
        return;
      }
      if (sending) {
        return;
      }
      sending = true;
      hideError();
      stayButton.setAttribute("aria-disabled", "true");
      request("POST", settings.continueUrl).then(function (answer) {
        sending = false;
        stayButton.removeAttribute("aria-disabled");
        if (leaving) {
          return;
        }
        if (!answer) {
          showError(REQUEST_FAILED);
          // The press may have been recorded and only its answer lost: ask after the short
          // retry delay (and again if unanswered), and close the warning if it was extended.
          setTimeout(function () { recheck(true); }, FAILED_RETRY_MS);
          return;
        }
        plan(true);
      });
    });

    closeButton.addEventListener("click", function () {
      dismissed = true;
      plan(true);
    });

    // A page shown again (back-forward cache, or a tab brought back after sleep) has stale numbers.
    // A question that got no answer (no connection, or a request cut off when the page's clock
    // jumped, as after sleep) is asked again shortly, not at the next scheduled moment.
    var retrying = 0;
    function recheck(fresh) {
      check(fresh === true).then(function (answer) {
        if (!answer && !leaving && !retrying) {
          retrying = setTimeout(function () {
            retrying = 0;
            recheck();
          }, FAILED_RETRY_MS);
        }
        plan(true);
      });
    }

    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        leaving = false;
        expiring = false;
        sending = false;
        checking = null;
        recheck();
      }
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && !leaving) {
        recheck();
      }
    });
    // A dialog, a partial update or a save that finds the session ended (accounts.sessions'
    // refusal: 401 {"signed_in": false} with HX-Redirect) leaves as the page does at zero, sending
    // what is typed first, instead of letting htmx go to sign in at once.
    function refused(event) {
      var xhr = event.detail && event.detail.xhr;
      if (!xhr || xhr.status !== 401 || !xhr.getResponseHeader("HX-Redirect")) {
        return;
      }
      var answer = null;
      try {
        answer = JSON.parse(xhr.responseText);
      } catch (error) {
        return;
      }
      if (answer && answer.signed_in === false) {
        event.preventDefault();
        if (answer.kept === true) {
          // The refused post was kept from what it sent (accounts.sessions._refusal): its form's
          // guard may let the page go, whatever the keep request below answers.
          var element = event.detail.elt;
          var form = element && (element.tagName === "FORM" ? element : element.form || element.closest("form"));
          document.dispatchEvent(new CustomEvent("vr:typing-kept", {
            detail: { kept: 1, forms: form ? [form] : [] },
          }));
        }
        if (!leaving) {
          leave(answer.sign_in);
        }
      }
    }
    document.addEventListener("htmx:beforeOnLoad", refused);
    // htmx signals the answer on the element that asked; one that has left the page by then (a
    // dialog closed or drawn again meanwhile) no longer passes it on to the document.
    var asking = new WeakSet();
    document.addEventListener("htmx:beforeRequest", function (event) {
      var element = event.detail && event.detail.elt;
      if (!element || asking.has(element)) {
        return;
      }
      asking.add(element);
      element.addEventListener("htmx:beforeOnLoad", function (answer) {
        if (!document.contains(element)) {
          refused(answer);
        }
      });
    });
    // A dialog, a partial update or a save sent from this page is activity under the limits as
    // they stand: ask again, so the page plans from the server's numbers, not its own.
    document.addEventListener("htmx:afterRequest", function () {
      if (!leaving) {
        recheck();
      }
    });
    setInterval(function () {
      if (!leaving && !variant && document.visibilityState === "visible") {
        recheck();
      }
    }, IDLE_RECHECK_MS);

    var page = {
      inactivity_seconds: settings.inactivitySeconds,
      maximum_seconds: settings.maximumSeconds,
      warning_seconds: settings.warningSeconds,
      longest_warning_seconds: settings.longestWarningSeconds,
    };
    if (measure(page)) {
      plan(false);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
