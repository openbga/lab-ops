// Public site behaviour. Theme menu: Light, Dark or System, stored in localStorage "vr-theme", the
// key the application uses. assets/theme-init.js applies the saved choice before CSS loads; this
// file handles the menu, the hero picture and changes made in another tab or in the system setting.
// Without JavaScript the menu stays hidden and the page follows the system setting.
(function () {
  "use strict";
  var KEY = "vr-theme";
  var root = document.documentElement;
  var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      return localStorage.getItem(KEY) || "system";
    } catch (error) {
      return "system";
    }
  }

  function apply(choice) {
    if (choice !== "light" && choice !== "dark") {
      choice = "system";
    }
    var dark = choice === "dark" || (choice === "system" && !!systemDark && systemDark.matches);
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.setAttribute("data-theme-choice", choice);
    // A <source data-theme-dark> carries the dark art; pin it to the chosen theme.
    document.querySelectorAll("source[data-theme-dark]").forEach(function (source) {
      source.media = dark ? "all" : "not all";
    });
    var label = choice.charAt(0).toUpperCase() + choice.slice(1);
    document.querySelectorAll("[data-theme-label]").forEach(function (text) {
      text.textContent = label;
    });
    document.querySelectorAll("[data-theme-value]").forEach(function (option) {
      option.setAttribute("aria-pressed", option.getAttribute("data-theme-value") === choice ? "true" : "false");
    });
  }

  function setOpen(menu, open, focusButton) {
    var button = menu.querySelector("[data-theme-button]");
    var options = menu.querySelector("[data-theme-options]");
    button.setAttribute("aria-expanded", open ? "true" : "false");
    options.hidden = !open;
    if (open) {
      (options.querySelector('[aria-pressed="true"]') || options.querySelector("button")).focus();
    } else if (focusButton) {
      button.focus();
    }
  }

  document.querySelectorAll("[data-theme-menu]").forEach(function (menu) {
    var button = menu.querySelector("[data-theme-button]");
    var options = menu.querySelector("[data-theme-options]");
    var items = Array.prototype.slice.call(options.querySelectorAll("[data-theme-value]"));

    button.addEventListener("click", function () {
      setOpen(menu, options.hidden, false);
    });
    button.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setOpen(menu, true, false);
      }
    });
    items.forEach(function (item, index) {
      item.addEventListener("click", function () {
        var choice = item.getAttribute("data-theme-value");
        try {
          localStorage.setItem(KEY, choice);
        } catch (error) {
          // Storage blocked: the choice applies to this page only.
        }
        apply(choice);
        setOpen(menu, false, true);
      });
      item.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowDown") next = items[(index + 1) % items.length];
        if (event.key === "ArrowUp") next = items[(index - 1 + items.length) % items.length];
        if (event.key === "Home") next = items[0];
        if (event.key === "End") next = items[items.length - 1];
        if (next) {
          event.preventDefault();
          next.focus();
        }
      });
    });
    menu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !options.hidden) {
        event.preventDefault();
        setOpen(menu, false, true);
      }
    });
    // Close when focus or a click moves outside the menu.
    menu.addEventListener("focusout", function (event) {
      if (!options.hidden && event.relatedTarget && !menu.contains(event.relatedTarget)) {
        setOpen(menu, false, false);
      }
    });
    document.addEventListener("click", function (event) {
      if (!options.hidden && !menu.contains(event.target)) {
        setOpen(menu, false, false);
      }
    });
  });

  if (systemDark) {
    systemDark.addEventListener("change", function () {
      if (saved() === "system") {
        apply("system");
      }
    });
  }
  window.addEventListener("storage", function (event) {
    if (event.key === KEY) {
      apply(saved());
    }
  });

  apply(saved());
})();
