'use strict';

// Loaded before the page paints, so a saved theme never flashes the other one
// first. app.js handles changes after load.
(() => {
  let theme = null;
  try { theme = localStorage.getItem('sponsorscan-theme'); } catch { /* storage can be blocked; the system theme still applies */ }
  const dark = theme === 'dark' || (theme !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
