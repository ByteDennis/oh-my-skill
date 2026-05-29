"""Browser tests for the Find/Replace highlight overlay alignment.

The find widget draws a parallel <div> over the editor textarea with the
same text and <mark> spans around matches. If the overlay's font, padding,
wrap rules, or scroll offset drift from the textarea, the visible
highlight no longer sits on top of the matched word — which is the bug
the user reported, especially with ``???`` collapsibles and other
nontrivial markdown source.

Each parametrized case loads a different markdown sample into the
editor, opens the Find widget, types a query, and asserts:

1. The number of <mark> elements equals the number of real matches.
2. Each <mark>'s viewport y-position matches the textarea's expected
   y for that character offset (within 2 px).
3. Each <mark>'s viewport x-position matches the textarea's expected
   x for that character offset (within 3 px — mono font, character
   width is stable).

These are the alignment invariants that, when broken, manifest as the
"highlight in the wrong place" bug.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest
import werkzeug.serving

# Skip the whole module if playwright or chromium isn't available so the
# rest of the test suite still runs on a fresh checkout.
playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

CHROMIUM_DIR = Path.home() / ".cache" / "ms-playwright"
if not any(CHROMIUM_DIR.glob("chromium-*")):
    pytest.skip("playwright chromium browser not installed", allow_module_level=True)


# ─── Live server ────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def _live_app_data(tmp_path_factory, monkeypatch_module):
    """Module-scoped variant of the `tmp_data` fixture so the live server
    points at a fresh DB shared across every parametrized case in this
    module. We don't want the server bouncing per test."""
    d = tmp_path_factory.mktemp("findhl") / "data"
    d.mkdir()
    monkeypatch_module.setenv("OMI_DATA_DIR", str(d))
    monkeypatch_module.setenv("SETTINGS_DB", str(d / "oh-my-skill.db"))
    monkeypatch_module.setenv("SKILLCARDS_DB", str(d / "skillcards.db"))
    monkeypatch_module.setenv("OMI_LOG_DB", str(d / "api_logs.db"))
    monkeypatch_module.setenv("OMI_LOG_FILE", str(d / "api.log"))
    monkeypatch_module.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-claude")
    import sys
    for mod in list(sys.modules):
        if mod.startswith("oh_my_skill"):
            sys.modules.pop(mod, None)
    return d


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def live_server(_live_app_data):
    from oh_my_skill.app import app as flask_app
    flask_app.config["TESTING"] = True
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = werkzeug.serving.make_server("127.0.0.1", port, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{port}", "app": flask_app}
    server.shutdown()
    thread.join(timeout=2)


# ─── Test cases ─────────────────────────────────────────────────────
# Each case: id, markdown content, query, expected match count.
# Keep lines well under the textarea's wrap width so we can predict
# column-from-offset without modelling soft wrapping.
CASES = [
    # 1. Plain single-line — sanity check
    ("plain-single",
     "Hello world",
     "world", 1),

    # 2. Multi-line, match on a later line
    ("multi-line",
     "alpha\nbeta\ngamma\ndelta", "gamma", 1),

    # 3. Many blank lines between matches — exercises y-offset accumulation
    ("with-blank-lines",
     "first\n\n\n\n\nsecond\n\n\nthird", "second", 1),

    # 4. Match at the very start
    ("at-start",
     "needle in haystack\nmore text", "needle", 1),

    # 5. Match at the very end
    ("at-end",
     "haystack with a needle", "needle", 1),

    # 6. Multiple matches on the same line
    ("repeat-same-line",
     "foo bar foo bar foo", "foo", 3),

    # 7. Multiple matches across lines
    ("repeat-across-lines",
     "the quick fox\nthe brown fox\nthe lazy fox", "fox", 3),

    # 8. Indented body (4-space, like ??? collapsible content)
    ("indented-body",
     '??? "Collapsible"\n    body line one\n    body needle two\n    body line three',
     "needle", 1),

    # 9. ??? collapsible source with match on the directive line
    ("collapsible-directive",
     '???+ "Open by default"\n    inner text', "Open", 1),

    # 10. Code block — match inside fenced block
    ("inside-code-block",
     "before code\n```python\nx = needle()\ny = 1\n```\nafter code",
     "needle", 1),

    # 11. Collapsible code block (```lang+) — recently added syntax
    ("collapsible-code-block",
     "preamble\n```python+ Title\nprint('needle')\n```\nepilogue",
     "needle", 1),

    # 12. Match inside a callout (> [!NOTE]) — recently fixed code-in-callout
    ("inside-callout",
     "> [!NOTE]\n> Remember to verify the needle\n> stays aligned.",
     "needle", 1),

    # 13. Special HTML chars (<, >, &) — overlay escapes these; misalignment
    #     here would prove an escapeHTML mismatch
    ("html-special-chars",
     "a < b > c & d\nneedle on next line\nend", "needle", 1),

    # 14. Unicode (CJK) — wide-width characters; mono font may or may not
    #     keep them at single-cell width. The case is informational; we
    #     skip the x-check via tolerance and still verify y-alignment.
    ("unicode-cjk",
     "ascii line\n中文测试 needle\nascii again", "needle", 1),

    # 15. Long-line URL (the prime suspect for the original wrap bug). Use
    #     a line short enough not to wrap at typical modal widths but
    #     containing a long unbreakable token.
    ("long-url-no-wrap",
     "see https://example.com/path/x and the needle is here", "needle", 1),

    # 16. KaTeX block delimiters — `$$ … $$` doesn't change the source
    #     character count but is visually distinctive
    ("math-block",
     "intro\n$$\nE = mc^2\n$$\nneedle after math", "needle", 1),

    # 17. Mixed markdown features — exercises the regression site
    ("mixed-collapsible-and-code",
     ('???+ "Section"\n'
      '    paragraph one\n'
      '    ```bash\n'
      '    echo needle\n'
      '    ```\n'
      '    paragraph two'),
     "needle", 1),
]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, live_server):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.goto(live_server["url"])
    pg.wait_for_load_state("networkidle")
    # Wait until the front-end CARDS array exists (initial fetch finishes).
    pg.wait_for_function("typeof CARDS !== 'undefined'")
    yield pg
    ctx.close()


def _create_card(app, title: str, content: str) -> str:
    """Use the real API (via test_client) so the live server's DB is the
    authoritative one — playwright will see the row immediately."""
    with app.test_client() as cli:
        r = cli.post("/skill-cards/api/cards", json={
            "title": title, "content": content, "tags": []
        })
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]


# JS executed in the page to measure alignment. Returns one record per
# <mark> with the actual position and the expected position (computed by
# placing the same character offset into a sibling textarea that mirrors
# the editor textarea's font/padding/border/wrap exactly).
_MEASURE_JS = r"""
({query}) => {
  const ta = document.getElementById('emContent');
  const hl = document.getElementById('findHighlights');
  const text = ta.value;

  // Recompute matches in JS so we don't have to plumb them through.
  const re = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  const offsets = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m[0].length === 0) { re.lastIndex++; continue; }
    offsets.push(m.index);
  }

  // The "ground truth" position of character offset N in the textarea is
  // computed via a clone of the textarea with the same metrics. The clone
  // is a textarea (not a div) so its wrap behaviour is identical to the
  // editor's. We set its selection to (N, N), make it scrollTop=0, and
  // read where the caret-line would be using a mirror div under it.
  // To avoid layout pollution, we use the well-known mirror-div trick
  // against the textarea's own styles, then translate the result into
  // the editor textarea's viewport position.
  function expectedViewportPos(offset) {
    const cs = getComputedStyle(ta);
    const mirror = document.createElement('div');
    const props = ['fontFamily','fontSize','fontWeight','fontStyle','lineHeight',
                   'letterSpacing','wordSpacing',
                   'paddingTop','paddingRight','paddingBottom','paddingLeft',
                   'borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth',
                   'boxSizing','tabSize','textIndent','textTransform'];
    props.forEach(p => mirror.style[p] = cs[p]);
    mirror.style.position = 'absolute';
    mirror.style.visibility = 'hidden';
    mirror.style.whiteSpace = 'pre-wrap';
    mirror.style.overflowWrap = 'normal';
    mirror.style.wordBreak = 'normal';
    mirror.style.width = ta.clientWidth + 'px';
    mirror.style.left = '-99999px';
    mirror.style.top = '0';
    mirror.appendChild(document.createTextNode(text.slice(0, offset)));
    const marker = document.createElement('span');
    marker.textContent = '​';
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    const taRect = ta.getBoundingClientRect();
    const mRect  = marker.getBoundingClientRect();
    const mirRect = mirror.getBoundingClientRect();
    document.body.removeChild(mirror);
    // marker x/y relative to mirror's border-box. Translate into the
    // textarea's viewport position, adjusting for scroll.
    const x = mRect.left - mirRect.left;
    const y = mRect.top  - mirRect.top;
    return {
      x: taRect.left + x - ta.scrollLeft,
      y: taRect.top  + y - ta.scrollTop,
    };
  }

  const marks = Array.from(hl.querySelectorAll('mark'));
  const results = [];
  for (let i = 0; i < marks.length; i++) {
    const mr = marks[i].getBoundingClientRect();
    const offset = offsets[i];
    const exp = expectedViewportPos(offset);
    results.push({
      offset,
      char: text.slice(offset, offset + 6),
      mark_x: mr.left, mark_y: mr.top,
      exp_x: exp.x, exp_y: exp.y,
      dx: Math.abs(mr.left - exp.x),
      dy: Math.abs(mr.top  - exp.y),
    });
  }
  return {markCount: marks.length, results};
}
"""


def _open_find_for(page, app, content: str, query: str, label: str):
    """Create a card with `content`, open it in the editor modal, then
    open the Find widget and type `query`. Returns nothing — the page is
    left in a state where #findHighlights is rendered."""
    card_id = _create_card(app, f"find-hl-{label}", content)
    # Refresh the in-memory CARDS so openCard finds it.
    page.evaluate("async () => { await loadCards(); }")
    page.evaluate(f"window.openCard({card_id!r})")
    page.wait_for_selector("#editorModal[open]")
    # Make sure the textarea is the actually-rendered editor (split mode
    # might hide it on small viewports, but our viewport is wide).
    page.wait_for_selector("#emContent:not([hidden])")
    # Open find via Ctrl+F (the binding only triggers when modal has focus
    # and we're not inside a ref-window).
    page.focus("#emContent")
    page.keyboard.press("Control+f")
    page.wait_for_selector("#findWidget.show")
    # Type the query into the find input
    page.fill("#fwFind", query)
    # Wait for either matches or an explicit "no results" state to settle.
    page.wait_for_function(
        "document.querySelectorAll('.find-highlights mark').length > 0 || "
        "document.getElementById('fwCount').textContent === 'No results'"
    )


# ─── The actual parametrized test ──────────────────────────────────
@pytest.mark.parametrize("label,content,query,expected_count",
                         CASES, ids=[c[0] for c in CASES])
def test_find_highlight_alignment(page, live_server, label, content, query, expected_count):
    app = live_server["app"]
    _open_find_for(page, app, content, query, label)

    # 1. Mark count
    rec = page.evaluate(_MEASURE_JS, {"query": query})
    assert rec["markCount"] == expected_count, (
        f"[{label}] expected {expected_count} <mark> elements, got {rec['markCount']}"
    )
    assert len(rec["results"]) == expected_count

    # Unicode width is font-dependent; relax the x check for that case.
    x_tol = 12 if label == "unicode-cjk" else 3
    y_tol = 2

    for r in rec["results"]:
        # 2. y-alignment
        assert r["dy"] <= y_tol, (
            f"[{label}] mark for offset {r['offset']} ({r['char']!r}) is "
            f"vertically misaligned by {r['dy']:.2f}px "
            f"(mark.y={r['mark_y']:.1f}, expected={r['exp_y']:.1f})"
        )
        # 3. x-alignment
        assert r["dx"] <= x_tol, (
            f"[{label}] mark for offset {r['offset']} ({r['char']!r}) is "
            f"horizontally misaligned by {r['dx']:.2f}px "
            f"(mark.x={r['mark_x']:.1f}, expected={r['exp_x']:.1f})"
        )


# Extra invariant: the overlay must clear itself when the modal closes —
# the bug the user reported ("highlight stays when card is closed").
def test_find_highlight_clears_on_modal_close(page, live_server):
    app = live_server["app"]
    _open_find_for(page, app, "hello world\nfoo bar foo\n", "foo", "clears")
    # Sanity: highlights are present
    n = page.evaluate("document.querySelectorAll('.find-highlights mark').length")
    assert n == 2

    # Close the modal the same way the close button does. Chromium fires the
    # dialog's 'close' event in a separate microtask, so wait for the close
    # handler to actually run (it strips the .show class from the widget) —
    # not just for the dialog's open flag to flip.
    page.evaluate("document.getElementById('editorModal').close()")
    page.wait_for_function(
        "!document.getElementById('editorModal').open && "
        "!document.getElementById('findWidget').classList.contains('show')"
    )

    # Overlay should be empty AND inactive
    state = page.evaluate("""() => ({
        mark_count: document.querySelectorAll('.find-highlights mark').length,
        has_active: document.getElementById('findHighlights').classList.contains('active'),
        widget_open: document.getElementById('findWidget').classList.contains('show'),
    })""")
    assert state["mark_count"] == 0, "overlay <mark>s should be cleared when modal closes"
    assert state["has_active"] is False, ".find-highlights should drop .active on modal close"
    assert state["widget_open"] is False, "find widget should hide when modal closes"
