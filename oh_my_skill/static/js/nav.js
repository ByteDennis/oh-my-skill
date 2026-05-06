/* @oh-my/ui — shared top-nav + theme bootstrap.
 * Drop-in: <script src="/omi/ui/nav.js" data-service="image|slide|skill"></script>
 *
 * Renders nav links to all 3 services (ports configurable via window.__OMI_NAV__
 * if set, otherwise sensible defaults).
 */
(function () {
  const services = window.__OMI_NAV__ || [
    { id: 'image', name: 'image', port: 5006, label: '🎨 image' },
    { id: 'slide', name: 'slide', port: 5008, label: '🖼 slide' },
    { id: 'skill', name: 'skill', port: 5009, label: '🧠 skill' },
  ];
  const script = document.currentScript;
  const current = (script && script.dataset.service) || '';
  document.body.classList.add('service-' + current);

  function render() {
    const nav = document.querySelector('.omi-nav');
    if (!nav) return;
    nav.innerHTML = services.map(s => {
      const isCurrent = s.id === current;
      const url = isCurrent ? '#' : `${location.protocol}//${location.hostname}:${s.port}/`;
      return `<a href="${url}" class="${isCurrent ? 'active' : ''}">${s.label}</a>`;
    }).join('');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
