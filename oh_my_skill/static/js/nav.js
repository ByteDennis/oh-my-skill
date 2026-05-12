/* @oh-my/ui — shared top-nav + theme bootstrap.
 * Drop-in: <script src="/omi/ui/nav.js" data-service="image|slide|skill|clipboard"></script>
 *
 * Renders nav links to all services (ports configurable via window.__OMI_NAV__
 * if set, otherwise sensible defaults). The brand element (.omi-brand) is
 * also wired as a link to the current service's home.
 */
(function () {
  const services = window.__OMI_NAV__ || [
    { id: 'image',     name: 'image',     port: 5006, label: '🎨 image' },
    { id: 'clipboard', name: 'clipboard', port: 5007, label: '📋 clipboard' },
    { id: 'slide',     name: 'slide',     port: 5008, label: '🖼 slide' },
    { id: 'skill',     name: 'skill',     port: 5009, label: '🧠 skill' },
  ];
  const script = document.currentScript;
  const current = (script && script.dataset.service) || '';
  document.body.classList.add('service-' + current);

  function render() {
    const nav = document.querySelector('.omi-nav');
    if (nav) {
      nav.innerHTML = services.map(s => {
        const isCurrent = s.id === current;
        const url = isCurrent ? '/' : `${location.protocol}//${location.hostname}:${s.port}/`;
        return `<a href="${url}" class="${isCurrent ? 'active' : ''}">${s.label}</a>`;
      }).join('');
    }
    // Make the brand text clickable — routes to current service home.
    const brand = document.querySelector('.omi-brand');
    if (brand && !brand.closest('a')) {
      brand.style.cursor = 'pointer';
      brand.addEventListener('click', () => { location.href = '/'; });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
