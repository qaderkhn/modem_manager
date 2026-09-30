// Run before CSS so the first styled paint uses the resolved preference.
(() => {
  const storageKey = 'modem-manager-theme';
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const validTheme = value => value === 'light' || value === 'dark';
  let preference = null;
  let toggle;

  try {
    const stored = localStorage.getItem(storageKey);
    if (validTheme(stored)) preference = stored;
  } catch {
    // Storage can be unavailable; keep the selection for this page.
  }

  const applyTheme = () => {
    const theme = preference || (systemTheme.matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    if (toggle) {
      const next = theme === 'dark' ? 'light' : 'dark';
      toggle.setAttribute('aria-label', `Switch to ${next} mode`);
      toggle.querySelector('[data-theme-label]').textContent = `${next === 'dark' ? 'Dark' : 'Light'} mode`;
      toggle.querySelector('[data-theme-icon]').textContent = next === 'dark' ? '☾' : '☀';
    }
  };

  applyTheme();
  systemTheme.addEventListener('change', () => {
    if (!preference) applyTheme();
  });
  window.addEventListener('storage', event => {
    if (event.storageArea !== window.localStorage) return;
    if (event.key === storageKey || event.key === null) {
      preference = validTheme(event.newValue) ? event.newValue : null;
      applyTheme();
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    toggle = document.querySelector('[data-theme-toggle]');
    if (!toggle) return;
    toggle.addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try {
        localStorage.setItem(storageKey, preference);
      } catch {
        // Theme switching still works when persistence is blocked.
      }
      applyTheme();
    });
    applyTheme();
    toggle.hidden = false;
  });
})();
