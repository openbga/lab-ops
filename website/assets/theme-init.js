// Applies the saved theme before CSS loads, so the page never flashes the other theme. Loaded
// synchronously in <head>. Key "vr-theme" matches the application's: "light" | "dark" | "system"
// (default). The menu itself lives in site.js.
(function () {
  "use strict";
  var choice = "system";
  try {
    choice = localStorage.getItem("vr-theme") || "system";
  } catch (error) {
    // Storage blocked: follow the system setting.
  }
  var dark = choice === "dark" ||
    (choice !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();
