// Runs before styles and React to restore the chosen theme without a flash.
// Kept external so the dashboard's same-origin script policy stays intact.
(() => {
  let theme = "dark";
  try { if (localStorage.getItem("sentinel-theme") === "light") theme = "light"; } catch {}
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.style.colorScheme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#0a0c10" : "#f8f7f4");
})();
