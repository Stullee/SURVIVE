// Applies the saved color theme before the page paints (kept out of app.js so it can run
// synchronously without an inline script, which the Content-Security-Policy forbids).
(function () {
  try {
    var saved = window.localStorage.getItem("ember-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    // Storage can be unavailable (private mode, blocked site data); the OS theme applies.
  }
})();
