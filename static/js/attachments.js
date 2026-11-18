// The attachments picker (django/forms/widgets/attachment_files.html): lists the chosen files with name and
// size, keeps earlier picks when more are added (a photo, then another), lets each be removed, and
// checks type, size, count and total before the form is sent, so an oversized upload is caught
// here instead of losing the typed text to a refused request. The server checks everything again
// by content. Loaded by the widget itself; safe to load more than once.
(function () {
  "use strict";
  if (window.vrFiles) {
    window.vrFiles.scan(document);
    return;
  }

  function size(bytes) {
    if (bytes < 1024) return bytes + " bytes";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function extension(name) {
    var dot = name.lastIndexOf(".");
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  }

  function init(field) {
    if (field.vrFilesReady) return;
    field.vrFilesReady = true;
    var input = field.querySelector("input[type=file]");
    var list = field.querySelector("[data-vr-files-list]");
    var problem = field.querySelector("[data-vr-files-problem]");
    var drop = field.querySelector(".vr-files-drop");
    var allowed = (input.getAttribute("accept") || "").split(",").filter(function (item) {
      return item.charAt(0) === ".";
    }).map(function (item) { return item.slice(1); });
    var maxFiles = parseInt(input.getAttribute("data-max-files"), 10);
    var maxFile = parseInt(input.getAttribute("data-max-file-bytes"), 10);
    var maxTotal = parseInt(input.getAttribute("data-max-total-bytes"), 10);
    var canEdit = typeof DataTransfer === "function";
    var chosen = [];

    function sync() {
      if (!canEdit) return;
      var transfer = new DataTransfer();
      chosen.forEach(function (file) { transfer.items.add(file); });
      input.files = transfer.files;
    }

    function problems() {
      var found = [];
      var total = 0;
      chosen.forEach(function (file) {
        total += file.size;
        var ext = extension(file.name);
        var image = /^image\//.test(file.type);
        if (allowed.indexOf(ext) < 0 && !(image && ["jpg", "jpeg", "png", "gif", "webp", "tif", "tiff"].indexOf(ext) >= 0)) {
          found.push(file.name + ": this type of file is not accepted.");
        }
        if (file.size === 0) found.push(file.name + ": the file is empty.");
        if (file.size > maxFile) found.push(file.name + ": larger than " + size(maxFile) + ".");
      });
      if (chosen.length > maxFiles) found.push("Attach at most " + maxFiles + " files at a time.");
      if (total > maxTotal) found.push("The files add up to " + size(total) + "; the limit is " + size(maxTotal) + ".");
      return found;
    }

    function render() {
      list.textContent = "";
      chosen.forEach(function (file, index) {
        var item = document.createElement("li");
        item.className = "vr-files-item";
        var label = document.createElement("span");
        label.className = "vr-files-name";
        label.textContent = file.name;
        var meta = document.createElement("span");
        meta.className = "vr-files-size vr-muted";
        meta.textContent = size(file.size);
        item.append(label, meta);
        if (canEdit) {
          var remove = window.vrUi.button("Remove", "quiet", true);
          remove.setAttribute("aria-label", "Remove " + file.name);
          remove.addEventListener("click", function () {
            chosen.splice(index, 1);
            sync();
            render();
            input.focus();
          });
          item.append(remove);
        }
        list.append(item);
      });
      var found = problems();
      problem.textContent = found.join(" ");
      problem.hidden = found.length === 0;
      input.setAttribute("aria-invalid", found.length ? "true" : "false");
      field.classList.toggle("vr-files-chosen", chosen.length > 0);
    }

    function add(files) {
      Array.prototype.forEach.call(files, function (file) {
        var known = chosen.some(function (other) {
          return other.name === file.name && other.size === file.size && other.lastModified === file.lastModified;
        });
        if (!known) chosen.push(file);
      });
      sync();
      render();
    }

    input.addEventListener("change", function () {
      if (canEdit) {
        add(input.files);
      } else {
        chosen = Array.prototype.slice.call(input.files);
        render();
      }
    });
    if (drop) {
      ["dragenter", "dragover"].forEach(function (name) {
        drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add("vr-drop-over"); });
      });
      ["dragleave", "drop"].forEach(function (name) {
        drop.addEventListener(name, function () { drop.classList.remove("vr-drop-over"); });
      });
      drop.addEventListener("drop", function (event) {
        event.preventDefault();
        if (canEdit && event.dataTransfer) add(event.dataTransfer.files);
      });
    }
    var form = input.form;
    if (form) {
      form.addEventListener("submit", function (event) {
        if (problems().length) {
          event.preventDefault();
          event.stopImmediatePropagation();
          render();
          problem.focus();
        }
      }, true);
    }
    problem.tabIndex = -1;
    field.classList.add("vr-file-ready", "vr-files-ready");
  }

  function scan(scope) {
    (scope.querySelectorAll ? scope : document).querySelectorAll("[data-vr-files-field]").forEach(init);
  }

  window.vrFiles = { scan: scan };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { scan(document); });
  } else {
    scan(document);
  }
  ["htmx:load", "htmx:afterSettle"].forEach(function (name) {
    document.addEventListener(name, function (event) { scan(event.target || document); });
  });
})();
