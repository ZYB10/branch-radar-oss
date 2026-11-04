(function bootstrapTheme() {
  "use strict";

  try {
    const saved = localStorage.getItem("branch-radar-theme");
    const theme = saved === "light" || saved === "dark"
      ? saved
      : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    const themeColor = document.getElementById("theme-color-meta");
    if (themeColor) themeColor.content = theme === "dark" ? "#0f1722" : "#f3f6fa";
  } catch (_error) {
    document.documentElement.dataset.theme = "light";
  }
})();
