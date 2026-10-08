// Loaded in <head> so the theme is applied before first paint.
(function () {
  var saved = null;
  try { saved = localStorage.getItem('theme'); } catch (e) { /* private mode */ }
  var dark = saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
