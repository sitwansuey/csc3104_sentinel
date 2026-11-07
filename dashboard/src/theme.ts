// Restore the chosen theme before the React entry point renders.
(() => {
  let theme = "dark";
  try { if (localStorage.getItem("sentinel-theme") === "light") theme = "light"; } catch {}
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.style.colorScheme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#0a0c10" : "#f8f7f4");
})();
