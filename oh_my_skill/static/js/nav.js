/* @oh-my/ui - shared nav + theme bootstrap + UX helpers.
 * Drop-in: <script src="/omi/ui/nav.js" data-service="image|slide|skill"></script>
 *
 * Enhances existing markup only:
 *   - nav links from `window.__OMI_NAV__`
 *   - <select data-omi-theme-switch> auto-populated and persisted via
 *     localStorage['omi-theme']; built-in themes paper/forest/ocean/sunset/
 *     grape/graphite/rose
 *   - <button data-omi-copy="text">label</button> auto-wired to clipboard
 *     copy with HTTPS fallback + press animation + toast confirmation
 *
 * Globals exposed for imperative use:
 *   window.omiToast({title, body, kind, ms})  spawn a toast
 *   window.omiCopy(text)                      Promise<boolean>, HTTPS fallback
 *   window.omiWireCopy(root?)                 rescan DOM for data-omi-copy
 */
(function () {
  const services = Array.isArray(window.__OMI_NAV__) ? window.__OMI_NAV__ : [];
  const themes = (window.__OMI_THEME__ && window.__OMI_THEME__.themes) || [
    { id: 'paper', label: 'Paper' },
    { id: 'forest', label: 'Forest' },
    { id: 'ocean', label: 'Ocean' },
    { id: 'sunset', label: 'Sunset' },
    { id: 'grape', label: 'Grape' },
    { id: 'graphite', label: 'Graphite' },
    { id: 'rose', label: 'Rose' },
    { id: 'dark', label: 'Dark' },
    { id: 'light', label: 'Light' },
    { id: 'catppuccin', label: 'Catppuccin' },
    { id: 'nord', label: 'Nord' },
  ];
  const themeConfig = window.__OMI_THEME__ || {};
  const storageKey = themeConfig.storageKey || 'omi-theme';
  const script = document.currentScript;
  const current = (script && script.dataset.service) || '';
  const themeSwitchEnabled = !script || script.dataset.themeSwitch !== 'false';

  function getInitialTheme() {
    try {
      const saved = window.localStorage.getItem(storageKey);
      if (saved && themes.some((theme) => theme.id === saved)) return saved;
    } catch (_) {}
    const configured = (script && script.dataset.theme) || themeConfig.defaultTheme;
    if (configured && themes.some((theme) => theme.id === configured)) return configured;
    return themes[0] ? themes[0].id : 'paper';
  }

  function applyTheme(themeId) {
    const root = document.documentElement;
    if (!root || !themeId) return;
    root.dataset.omiTheme = themeId;
    try {
      window.localStorage.setItem(storageKey, themeId);
    } catch (_) {}
    document.dispatchEvent(new CustomEvent('omi:themechange', {
      detail: { theme: themeId },
    }));
  }

  function wireThemePickers(activeTheme) {
    if (!themeSwitchEnabled) return;
    const nodes = document.querySelectorAll('[data-omi-theme-switch]');
    nodes.forEach((node, index) => {
      const select = node.matches('select') ? node : node.querySelector('select');
      if (!select) return;
      if (!select.id) select.id = `omi-theme-select-${index + 1}`;
      if (!select.options.length) {
        select.innerHTML = themes.map((theme) => (
          `<option value="${theme.id}">${theme.label}</option>`
        )).join('');
      }
      select.value = activeTheme;
      select.onchange = function () {
        applyTheme(this.value);
      };
    });
  }

  // ── Toast helper (uses .omi-toast / .omi-toast-stack from omi.css) ────
  function getToastStack() {
    let stack = document.querySelector('.omi-toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'omi-toast-stack';
      document.body.appendChild(stack);
    }
    return stack;
  }
  window.omiToast = function (titleOrOpts, body, kind, ms) {
    const opts = (typeof titleOrOpts === 'object' && titleOrOpts !== null)
      ? titleOrOpts
      : { title: titleOrOpts, body: body, kind: kind, ms: ms };
    const stack = getToastStack();
    const el = document.createElement('div');
    el.className = 'omi-toast' + (opts.kind ? ' ' + opts.kind : '');
    el.style.transition = 'opacity 0.18s ease, transform 0.18s ease';
    if (opts.title) {
      const t = document.createElement('div'); t.className = 'omi-toast-title';
      t.textContent = opts.title; el.appendChild(t);
    }
    if (opts.body) {
      const b = document.createElement('div'); b.className = 'omi-toast-body';
      b.textContent = opts.body; el.appendChild(b);
    }
    stack.appendChild(el);
    const lifetime = opts.ms != null ? opts.ms : (opts.kind === 'error' ? 4200 : 2200);
    setTimeout(() => {
      el.style.opacity = '0'; el.style.transform = 'translateY(-6px)';
      setTimeout(() => el.remove(), 220);
    }, lifetime);
    return el;
  };

  // ── Clipboard write with HTTPS fallback ───────────────────────────────
  // navigator.clipboard.writeText only works in secure contexts (https or
  // localhost). On plain HTTP it silently rejects, which is the #1 reason
  // "copy doesn't work on the remote dev server" bug reports happen. Fall
  // back to execCommand via a hidden textarea, which works anywhere.
  window.omiCopy = async function (text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, ta.value.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
    return ok;
  };

  // ── Declarative copy: <button data-omi-copy="text">label</button> ────
  // Wraps omiCopy with press animation, .confirmed ✓ floater, and a toast.
  // Re-runnable after dynamic DOM updates via window.omiWireCopy(root?).
  window.omiWireCopy = function (root) {
    const scope = root || document;
    scope.querySelectorAll('[data-omi-copy]').forEach((el) => {
      if (el.dataset.omiCopyWired) return;
      el.dataset.omiCopyWired = '1';
      el.addEventListener('click', async (e) => {
        e.stopPropagation();
        const text = el.dataset.omiCopy;
        if (!text) return;
        el.classList.add('pressed');
        setTimeout(() => el.classList.remove('pressed'), 340);
        const ok = await window.omiCopy(text);
        if (ok) {
          el.classList.add('confirmed');
          setTimeout(() => el.classList.remove('confirmed'), 1150);
        }
        window.omiToast({
          title: ok ? 'Copied' : 'Copy failed',
          body: ok ? (el.dataset.omiCopyLabel || text.slice(0, 80)) : 'Browser blocked clipboard write.',
          kind: ok ? 'success' : 'error',
        });
      });
    });
  };

  function render() {
    document.body.classList.add('service-' + current);
    const activeTheme = getInitialTheme();
    applyTheme(activeTheme);

    const nav = document.querySelector('.omi-nav');
    if (nav && services.length) {
      nav.innerHTML = services.map((service) => {
        const isCurrent = service.id === current;
        const url = isCurrent ? '#' : (service.href || `${location.protocol}//${location.hostname}:${service.port}/`);
        return `<a href="${url}" class="${isCurrent ? 'active' : ''}">${service.label}</a>`;
      }).join('');
    }

    wireThemePickers(activeTheme);
    window.omiWireCopy();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
