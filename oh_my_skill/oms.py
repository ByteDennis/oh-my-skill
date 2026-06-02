"""oms — CLI for managing oh-my-skill cards from anywhere.

Two audiences:

1. The per-card chat workspace (save/show/tag/untag/lesson) — operates on
   the card the chat is scoped to via $OMI_CARD_ID.
2. Any external agent or human (add/edit/rm/list/show) — operates on any
   card by id or title, against a local or remote server. This is the path
   to use when you just want to "write/edit/delete a card" without poking
   at the HTTP API by hand.

Subcommands:
    add                    Create a card. Content from --file / --content /
                           stdin. Title from --title or the leading "# H1".
    edit <id|--match T>    Update a card. Only the fields you pass change;
                           parent/links are preserved unless you set them.
    rm  <id|--match T>     Delete a card.
    list                   List all cards (id + title + tags)
    show [id]              Print a saved card (default: $OMI_CARD_ID)
    save                   Save ./card.md back to the DB (chat workspace)
    tag / untag <name>     Add / remove a tag on the scoped card
    lesson <text>          Append a one-line lesson to the lessons card

Content with newlines: pass --file PATH or pipe via stdin — NEVER hand-escape
JSON. Examples:
    oms add --title "Disk triage" --tags linux,disk --file notes.md
    oms add --tags linux <<'EOF'
    # Disk triage
    body with **markdown**, code blocks, etc.
    EOF
    oms edit 6a1e6634a601f --file revised.md
    oms edit 6a1e6634a601f --add-tag urgent --title "New title"
    oms rm 6a1e6634a601f --yes

Env / flags:
    --base URL / OMI_API_BASE   oh-my-skill server base URL. Auto-detected
                                (localhost / :5009) if unset. For a remote
                                box: --base http://HOST:5009
    OMI_CARD_ID                 Card the chat session is scoped to (only
                                used by save/show/tag/untag).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Overridden by the global --base flag before any request is made.
_BASE_OVERRIDE: str | None = None


def _api_base() -> str:
    if _BASE_OVERRIDE:
        return _BASE_OVERRIDE.rstrip('/')
    if v := os.environ.get('OMI_API_BASE'):
        return v.rstrip('/')
    # Try Docker default first, then common pipx default
    for cand in ('http://localhost', 'http://localhost:5009', 'http://127.0.0.1:5009'):
        try:
            with urllib.request.urlopen(cand + '/healthz', timeout=1) as r:
                if r.status == 200:
                    return cand
        except Exception:
            pass
    return 'http://localhost:5009'


def _http(method: str, path: str, body: dict | None = None, timeout=10) -> dict:
    url = _api_base() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode('utf-8', errors='replace')
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return {'_raw': txt}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode('utf-8', errors='replace'))
        except Exception:
            err = {'error': f'HTTP {e.code}'}
        err['_status'] = e.code
        return err
    except Exception as e:
        return {'error': str(e)}


def _require_card_id() -> str:
    cid = os.environ.get('OMI_CARD_ID', '').strip()
    if not cid:
        sys.stderr.write('error: OMI_CARD_ID not set; this CLI runs inside a per-card chat workspace.\n')
        sys.exit(2)
    return cid


def _get_card(card_id: str) -> dict | None:
    cards = _http('GET', '/skill-cards/api/cards')
    if not isinstance(cards, list):
        return None
    for c in cards:
        if c.get('id') == card_id:
            return c
    return None


def _resolve_card(ident: str | None, match: str | None) -> dict:
    """Resolve a card from a positional id OR a --match title (case-
    insensitive, exact then substring). Exits with a clear error on
    miss / ambiguity. One of ident/match must be given."""
    if not ident and not match:
        sys.stderr.write('error: pass a card id or --match TITLE\n'); sys.exit(2)
    cards = _http('GET', '/skill-cards/api/cards')
    if not isinstance(cards, list):
        sys.stderr.write(f'error: could not list cards: {cards}\n'); sys.exit(1)
    if ident:
        for c in cards:
            if c.get('id') == ident:
                return c
        sys.stderr.write(f'error: no card with id {ident}\n'); sys.exit(1)
    needle = match.strip().lower()
    exact = [c for c in cards if (c.get('title') or '').strip().lower() == needle]
    hits = exact or [c for c in cards if needle in (c.get('title') or '').lower()]
    if not hits:
        sys.stderr.write(f'error: no card title matching {match!r}\n'); sys.exit(1)
    if len(hits) > 1:
        sys.stderr.write(f'error: {len(hits)} cards match {match!r}; pass an explicit id:\n')
        for c in hits[:10]:
            sys.stderr.write(f'  {c["id"]:14s}  {c.get("title","")}\n')
        sys.exit(1)
    return hits[0]


def _read_content(args) -> str | None:
    """Resolve card body from --file, --content, or piped stdin (in that
    priority). Returns None when no source was supplied (so callers can
    decide whether content is required).

    stdin is consumed ONLY when it's a redirect/heredoc/pipe with data
    *ready to read* — never an interactive terminal, and never a
    non-tty fd that would block (e.g. an inherited stdin in a script
    where the caller only meant to change --title). `--file -` forces
    stdin explicitly."""
    f = getattr(args, 'file', None)
    if f and f != '-':
        path = os.path.expanduser(f)
        if not os.path.isfile(path):
            sys.stderr.write(f'error: --file not found: {path}\n'); sys.exit(2)
        with open(path, encoding='utf-8') as fh:
            return fh.read()
    if getattr(args, 'content', None) is not None:
        return args.content
    if f == '-':                       # explicit stdin request — always read
        return sys.stdin.read()
    if sys.stdin.isatty():
        return None
    # Non-tty stdin: only read if data is actually waiting, so we don't
    # hang when stdin is just an inherited fd with nothing to give.
    try:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
    except Exception:
        pass  # select unavailable (rare) — fall through to a best-effort read
    data = sys.stdin.read()
    return data if data else None


def _split_tags(spec: str | None) -> list[str]:
    """'a, b ,c' -> ['a','b','c']. Empty/None -> []."""
    if not spec:
        return []
    return [t.strip() for t in spec.replace('\n', ',').split(',') if t.strip()]


def _title_from_content(content: str) -> str | None:
    """First leading '# H1' line becomes the title."""
    for line in (content or '').splitlines():
        s = line.strip()
        if s.startswith('# '):
            return s[2:].strip()
        if s:
            break  # first non-blank line isn't an H1
    return None


def _card_url(card_id: str) -> str:
    return f"{_api_base()}/#card={card_id}"


def cmd_add(args):
    content = _read_content(args)
    if content is None:
        content = ''
    title = (args.title or '').strip() or _title_from_content(content)
    if not title:
        sys.stderr.write('error: no title — pass --title or start the content '
                          'with a "# Heading" line\n'); sys.exit(2)
    body = {
        'title': title,
        'content': content,
        'tags': _split_tags(args.tags),
    }
    if args.parent:
        body['parent_id'] = args.parent
    if args.link:
        body['links'] = list(args.link)
    res = _http('POST', '/skill-cards/api/cards', body)
    if not isinstance(res, dict) or res.get('error') or not res.get('id'):
        sys.stderr.write(f'add failed: {res.get("error", res)}\n'); sys.exit(1)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"added · {res['id']} · {res['title']}")
        print(f"  {_card_url(res['id'])}")
    return res


def cmd_edit(args):
    card = _resolve_card(args.card_id, args.match)
    cid = card['id']
    meta = card.get('metadata') or {}
    new_content = _read_content(args)
    new_title = (args.title or '').strip() or None

    # Tag resolution: --tags replaces wholesale; --add-tag / --rm-tag mutate
    # the existing set. They can combine (replace first, then add/remove).
    tags = list(card.get('tags') or [])
    if args.tags is not None:
        tags = _split_tags(args.tags)
    for t in (args.add_tag or []):
        if t not in tags:
            tags.append(t)
    for t in (args.rm_tag or []):
        tags = [x for x in tags if x != t]

    body = {
        'title': new_title or card.get('title') or 'Untitled',
        'content': new_content if new_content is not None else card.get('content', ''),
        'tags': tags,
    }
    # Only touch parent/links when explicitly given — the server preserves
    # them when these keys are absent (None == "leave untouched").
    if args.parent is not None:
        body['parent_id'] = args.parent           # '' clears it
    if args.link is not None:
        body['links'] = list(args.link)           # [] clears them

    res = _http('PUT', f'/skill-cards/api/cards/{cid}', body)
    if not isinstance(res, dict) or res.get('error'):
        sys.stderr.write(f'edit failed: {res.get("error", res)}\n'); sys.exit(1)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        changed = []
        if new_content is not None: changed.append('content')
        if new_title: changed.append('title')
        if args.tags is not None or args.add_tag or args.rm_tag: changed.append('tags')
        if args.parent is not None: changed.append('parent')
        if args.link is not None: changed.append('links')
        print(f"edited · {cid} · {body['title']} · changed: {', '.join(changed) or 'nothing'}")
        print(f"  {_card_url(cid)}")
    return res


def cmd_rm(args):
    card = _resolve_card(args.card_id, args.match)
    cid, title = card['id'], card.get('title', '')
    if not args.yes:
        sys.stderr.write(f"about to delete {cid} · {title!r}\n"
                         f"re-run with --yes to confirm.\n")
        sys.exit(2)
    res = _http('DELETE', f'/skill-cards/api/cards/{cid}')
    # DELETE may return {} or {'ok': True}; treat an explicit error as failure.
    if isinstance(res, dict) and res.get('error'):
        sys.stderr.write(f'rm failed: {res["error"]}\n'); sys.exit(1)
    print(f"deleted · {cid} · {title}")


def _find_guide_card(explicit: str | None) -> dict | None:
    """Locate the markdown syntax guide the agent should read/refine against.
    This is the CONCISE agent-facing card (the 'MD Cheatsheet'), not the
    verbose user reference. Resolution order:
       1. explicit id (flag) or $OMI_GUIDE_CARD
       2. a card tagged 'md-guide'
       3. a card whose title is 'MD Cheatsheet'
       4. a card tagged both 'markdown' and 'reference' (legacy)
       5. a card whose title is 'Markdown Editor Reference' (legacy)
    Returns the card dict, or None if nothing matches."""
    gid = (explicit or os.environ.get('OMI_GUIDE_CARD') or '').strip()
    cards = _http('GET', '/skill-cards/api/cards')
    if not isinstance(cards, list):
        return None
    if gid:
        return next((c for c in cards if c.get('id') == gid), None)

    def by_tag(tag):
        return next((c for c in cards
                     if tag in {t.lower() for t in (c.get('tags') or [])}), None)
    def by_title(title):
        return next((c for c in cards
                     if (c.get('title') or '').strip().lower() == title), None)

    return (by_tag('md-guide')
            or by_title('md cheatsheet')
            or next((c for c in cards
                     if {'markdown', 'reference'} <=
                        {t.lower() for t in (c.get('tags') or [])}), None)
            or by_title('markdown editor reference'))


def cmd_guide(args):
    """Print the markdown reference card (read-only). For the agent to read
    the supported syntax / house style before editing."""
    card = _find_guide_card(args.guide)
    if not card:
        sys.stderr.write('error: no markdown-reference card found '
                         '(tag a card with "markdown"+"reference", or pass --guide ID)\n')
        sys.exit(1)
    print(f"# {card['title']}  ({card['id']})\n")
    print(card.get('content', ''))


def cmd_refine(args):
    """Emit an actionable instruction block: the house-style reference + the
    target card's current content + a directive to rewrite it to match.

    This command does NOT call an LLM — it assembles the prompt the *agent*
    then acts on. The agent reads this output, rewrites the card body to
    follow the reference conventions, and persists (./card.md + `oms save`
    in a chat workspace, or `oms edit <id> --file …` elsewhere)."""
    guide = _find_guide_card(args.guide)
    if not guide:
        sys.stderr.write('error: no markdown-reference card found '
                         '(tag a card with "markdown"+"reference", or pass --guide ID)\n')
        sys.exit(1)
    # Target: positional id / --match / $OMI_CARD_ID (chat workspace).
    ident = args.card_id
    if not ident and not args.match:
        ident = os.environ.get('OMI_CARD_ID', '').strip() or None
    target = _resolve_card(ident, args.match)
    if target['id'] == guide['id']:
        sys.stderr.write('refusing to refine the reference card against itself\n')
        sys.exit(2)

    in_workspace = (os.environ.get('OMI_CARD_ID', '').strip() == target['id']
                    and os.path.isfile('./card.md'))
    persist = ('edit ./card.md, then run `oms save`'
               if in_workspace else
               f"write the result to a file, then run `oms edit {target['id']} --file <file>`")

    out = []
    out.append('===== REFINE TASK =====')
    out.append(f"Goal: rewrite the card below so its markdown follows the house "
               f"style and uses the supported syntax documented in the REFERENCE. "
               f"Preserve every fact and the H1 title; improve structure, tables, "
               f"callouts, collapsibles, code fences, and consistency. Do NOT invent content.")
    out.append(f"When done: {persist}.")
    out.append('')
    out.append(f"----- REFERENCE: {guide['title']} ({guide['id']}) -----")
    out.append(guide.get('content', '').strip())
    out.append('')
    out.append(f"----- CURRENT CARD: {target['title']} ({target['id']}) "
               f"tags={target.get('tags') or []} -----")
    out.append(target.get('content', '').rstrip())
    out.append('')
    out.append('----- END. Apply the reference conventions to the card above, '
               f'then persist: {persist}. -----')
    print('\n'.join(out))


def cmd_save(_):
    cid = _require_card_id()
    cwd = os.getcwd()
    res = _http('POST', f'/api/cards/{cid}/sync-from-disk', {'cwd': cwd})
    if 'error' in res:
        sys.stderr.write(f'save failed: {res["error"]}\n')
        sys.exit(1)
    print(f"saved · {res.get('title', '?')} · {res.get('updated_at', '')}")


def cmd_show(args):
    # Optional explicit card_id argument; falls back to OMI_CARD_ID env.
    cid = (getattr(args, 'card_id', None) or '').strip() or _require_card_id()
    card = _get_card(cid)
    if not card:
        sys.stderr.write(f'card {cid} not found\n')
        sys.exit(1)
    print(f"# {card['title']}\n")
    if card.get('tags'):
        print(f"tags: {card['tags']}\n")
    print(card.get('content', ''))


def _set_tags(card_id: str, new_tags: list[str]) -> dict:
    card = _get_card(card_id)
    if not card:
        return {'error': f'card {card_id} not found'}
    body = {'title': card['title'], 'content': card.get('content', ''), 'tags': new_tags}
    return _http('PUT', f'/skill-cards/api/cards/{card_id}', body)


def cmd_tag(args):
    cid = _require_card_id()
    card = _get_card(cid)
    if not card:
        sys.stderr.write(f'card {cid} not found\n'); sys.exit(1)
    tags = list(card.get('tags') or [])
    if args.tag in tags:
        print(f"already tagged: {args.tag}"); return
    tags.append(args.tag)
    res = _set_tags(cid, tags)
    if 'error' in res:
        sys.stderr.write(f'tag failed: {res["error"]}\n'); sys.exit(1)
    print(f"+tag {args.tag} · now {tags}")


def cmd_untag(args):
    cid = _require_card_id()
    card = _get_card(cid)
    if not card:
        sys.stderr.write(f'card {cid} not found\n'); sys.exit(1)
    tags = [t for t in (card.get('tags') or []) if t != args.tag]
    res = _set_tags(cid, tags)
    if 'error' in res:
        sys.stderr.write(f'untag failed: {res["error"]}\n'); sys.exit(1)
    print(f"-tag {args.tag} · now {tags}")


def cmd_list(_):
    cards = _http('GET', '/skill-cards/api/cards')
    if not isinstance(cards, list):
        sys.stderr.write(f'list failed: {cards}\n'); sys.exit(1)
    for c in cards:
        tags = ', '.join(c.get('tags') or [])
        print(f"{c['id']:14s}  {c['title']}  [{tags}]")


def _find_lessons_card() -> dict | None:
    """Look for any card with the 'meta-lessons' tag. The first one wins."""
    cards = _http('GET', '/skill-cards/api/cards')
    if not isinstance(cards, list):
        return None
    for c in cards:
        if 'meta-lessons' in (c.get('tags') or []):
            return c
    return None


def cmd_lesson(args):
    """Append a one-line lesson to the lessons-learned card. Creates the
    card if it doesn't exist (tagged 'meta-lessons')."""
    text = ' '.join(args.text).strip()
    if not text:
        sys.stderr.write('error: lesson text is empty\n'); sys.exit(2)
    bullet = f"- {text}"
    card = _find_lessons_card()
    from datetime import datetime
    stamp = datetime.utcnow().strftime('%Y-%m-%d')
    if card:
        body = (card.get('content') or '').rstrip()
        # If body already ends with the same bullet, skip (cheap dedup)
        if bullet in body.splitlines()[-30:] if body else False:
            print(f"already recorded: {text}"); return
        new_body = (body + f"\n{bullet}  _<{stamp}>_\n").lstrip('\n')
        res = _http('PUT', f"/skill-cards/api/cards/{card['id']}", {
            'title': card['title'], 'content': new_body,
            'tags': card.get('tags') or [],
        })
        if 'error' in res:
            sys.stderr.write(f'lesson failed: {res["error"]}\n'); sys.exit(1)
        print(f"+lesson · {card['title']} · {len(new_body.splitlines())} lines")
    else:
        body = ("# Lessons Learned\n\n"
                "Self-maintained list of mistakes Claude made in oh-my-skill chats.\n"
                "Read these BEFORE editing a card — don't repeat them.\n\n"
                "## Lessons\n\n"
                f"{bullet}  _<{stamp}>_\n")
        res = _http('POST', '/skill-cards/api/cards', {
            'title': 'Lessons Learned',
            'content': body,
            'tags': ['meta-lessons'],
        })
        if 'error' in res:
            sys.stderr.write(f'lesson failed: {res["error"]}\n'); sys.exit(1)
        print(f"+lesson (created card · id {res.get('id', '?')})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog='oms', description=__doc__.split('\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Run `oms <cmd> -h` for per-command flags. Content with '
               'newlines: use --file or stdin, never hand-escaped JSON.')
    p.add_argument('--base', metavar='URL',
                   help='server base URL (overrides $OMI_API_BASE; '
                        'auto-detected if unset)')
    sub = p.add_subparsers(dest='cmd', required=True)

    # ── create / edit / delete (work on any card, no $OMI_CARD_ID needed) ──
    a = sub.add_parser('add', aliases=['new'], help='create a card')
    a.add_argument('--title', help='card title (else taken from leading "# H1")')
    a.add_argument('--tags', help='comma-separated tags, e.g. linux,disk')
    a.add_argument('--parent', help='parent card id (tree hierarchy)')
    a.add_argument('--link', action='append', metavar='ID',
                   help='related card id (repeatable)')
    a.add_argument('--file', metavar='PATH', help='read body from a file')
    a.add_argument('--content', help='body as an inline string')
    a.add_argument('--json', action='store_true', help='print the raw JSON result')
    a.set_defaults(func=cmd_add)

    e = sub.add_parser('edit', aliases=['update'], help='update a card')
    e.add_argument('card_id', nargs='?', help='card id to edit')
    e.add_argument('--match', metavar='TITLE', help='find the card by title instead of id')
    e.add_argument('--title', help='new title')
    e.add_argument('--tags', help='replace tags wholesale (comma-separated)')
    e.add_argument('--add-tag', action='append', metavar='TAG', help='add a tag (repeatable)')
    e.add_argument('--rm-tag', action='append', metavar='TAG', help='remove a tag (repeatable)')
    e.add_argument('--parent', help="set parent id ('' clears it)")
    e.add_argument('--link', action='append', metavar='ID',
                   help='set related links (repeatable; pass none with --tags-style clear via empty)')
    e.add_argument('--file', metavar='PATH', help='replace body from a file')
    e.add_argument('--content', help='replace body with an inline string')
    e.add_argument('--json', action='store_true', help='print the raw JSON result')
    e.set_defaults(func=cmd_edit)

    r = sub.add_parser('rm', aliases=['delete'], help='delete a card')
    r.add_argument('card_id', nargs='?', help='card id to delete')
    r.add_argument('--match', metavar='TITLE', help='find the card by title instead of id')
    r.add_argument('--yes', action='store_true', help='confirm deletion')
    r.set_defaults(func=cmd_rm)

    g = sub.add_parser('guide', help='print the markdown reference card (read-only)')
    g.add_argument('--guide', metavar='ID', help='reference card id (default: auto-detect / $OMI_GUIDE_CARD)')
    g.set_defaults(func=cmd_guide)

    rf = sub.add_parser('refine', help='emit a prompt to rewrite a card to house style')
    rf.add_argument('card_id', nargs='?', help='card to refine (default: $OMI_CARD_ID)')
    rf.add_argument('--match', metavar='TITLE', help='find the target card by title instead of id')
    rf.add_argument('--guide', metavar='ID', help='reference card id (default: auto-detect / $OMI_GUIDE_CARD)')
    rf.set_defaults(func=cmd_refine)

    # ── chat-workspace + read commands ──
    sub.add_parser('save', help='save ./card.md back to the DB ($OMI_CARD_ID)').set_defaults(func=cmd_save)
    s = sub.add_parser('show', help='print a saved card')
    s.add_argument('card_id', nargs='?', help='card id (default: $OMI_CARD_ID)')
    s.set_defaults(func=cmd_show)
    sub.add_parser('list', help='list all cards').set_defaults(func=cmd_list)
    t = sub.add_parser('tag', help='add a tag to $OMI_CARD_ID'); t.add_argument('tag'); t.set_defaults(func=cmd_tag)
    u = sub.add_parser('untag', help='remove a tag from $OMI_CARD_ID'); u.add_argument('tag'); u.set_defaults(func=cmd_untag)
    l = sub.add_parser('lesson', help='append a lesson to the lessons card')
    l.add_argument('text', nargs='+', help='one-line lesson text')
    l.set_defaults(func=cmd_lesson)

    args = p.parse_args(argv)
    global _BASE_OVERRIDE
    if args.base:
        _BASE_OVERRIDE = args.base
    args.func(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
