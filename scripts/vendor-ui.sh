#!/usr/bin/env bash
# Vendor the @dennisl0731/oh-my-ui design assets into the Python package.
#
# WHY: oh-my-skill ships as a pure-Python (pipx) package — it must NOT depend
# on node/npm at runtime. So we fetch the published npm tarball at *build /
# maintenance* time and commit the assets into oh_my_skill/static/. They then
# ride along in the wheel (see pyproject force-include) and a `pipx install`
# gets them with zero JS toolchain.
#
# The npm package only ships the DESIGN LANGUAGE (omi.css, nav.js, icons.svg).
# It does NOT ship the colour/font catalogs — those live in
# oh_my_skill/themes_data/{colors,fonts}.json and are served by /api/themes.
#
# Skill-specific CSS (markdown rendering, slash menu, collapsibles, find
# overlay, focus pane) lives in oh_my_skill/static/css/skill.css and is loaded
# AFTER omi.css in the templates — so re-vendoring omi.css never clobbers it.
#
# Usage:
#   scripts/vendor-ui.sh            # latest published version
#   scripts/vendor-ui.sh 0.2.3      # a specific version
#   PKG=@dennisl0731/oh-my-ui scripts/vendor-ui.sh   # override the package
set -euo pipefail

PKG="${PKG:-@dennisl0731/oh-my-ui}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/oh_my_skill/static"
REGISTRY="https://registry.npmjs.org"

# Resolve the version: explicit arg, else dist-tags.latest from the registry.
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION="$(curl -fsSL "$REGISTRY/$PKG" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["dist-tags"]["latest"])')"
fi

# Scoped-package tarball lives at  $REGISTRY/@scope/name/-/name-version.tgz
NAME="${PKG##*/}"
URL="$REGISTRY/$PKG/-/$NAME-$VERSION.tgz"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
echo "↓ $PKG@$VERSION"
curl -fsSL "$URL" | tar xz -C "$tmp"

# Vendor the design language (omi.css) and icons. We deliberately do NOT
# auto-overwrite nav.js: oh-my-skill pins its own nav integration (the
# `service-*` body-class contract). Newer @oh-my/ui nav.js switched to a
# `data-omi-theme` / window.__OMI_THEME__ theme-switcher contract that this
# app doesn't wire up — adopting it is a separate, deliberate change.
# To also pull nav.js, run with VENDOR_NAV=1.
install -D "$tmp/package/css/omi.css" "$DEST/css/omi.css"
[ -f "$tmp/package/icons.svg" ] && install -D "$tmp/package/icons.svg" "$DEST/icons.svg" || true
if [ "${VENDOR_NAV:-0}" = "1" ] && [ -f "$tmp/package/js/nav.js" ]; then
  install -D "$tmp/package/js/nav.js" "$DEST/js/nav.js"
  echo "  ⚠ also overwrote nav.js (VENDOR_NAV=1) — re-check the nav/theme wiring"
fi

# Record the pinned version for reproducibility.
printf '%s@%s\n' "$PKG" "$VERSION" > "$DEST/.oh-my-ui-version"

echo "✓ vendored omi.css$([ -f "$DEST/icons.svg" ] && echo ' + icons.svg') → oh_my_skill/static/"
echo "  pinned: $(cat "$DEST/.oh-my-ui-version")"
echo "  reminder: skill-only CSS is in static/css/skill.css (loaded after omi.css)."
echo "  bump the ?v= cache-buster in templates after committing."
