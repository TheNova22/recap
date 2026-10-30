(() => {
  const root = document.documentElement;
  let saved = "";
  try {
    saved = localStorage.getItem("recap-theme") || "";
  } catch {
    // Theme follows the system preference when browser storage is unavailable.
  }
  root.dataset.theme = saved === "light" || saved === "dark"
    ? saved
    : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = root.dataset.theme === "dark" ? "#14211e" : "#e6ece7";
})();
