"""oms — CLI for the Claude chat workspace inside oh-my-skill.

Used by the per-card chat session to read/edit/tag the *currently scoped*
skill card, persisting back through the web app's HTTP API.

Subcommands:
    save                   Save ./card.md back to the DB
    show                   Print the current saved version
    tag <name>             Add a tag
    untag <name>           Remove a tag
    list                   List all cards (id + title)
    lesson <text>          Append a one-line lesson to the lessons-learned card
                           (tagged 'meta-lessons'). Creates it if missing.

Env:
    OMI_CARD_ID    The card the chat session is scoped to (set by the
                   server when the workspace is created).
    OMI_API_BASE   Base URL of the oh-my-skill server. Defaults to
                   http://localhost (Docker) or http://localhost:5009
                   (pipx local install) — auto-detected.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _api_base() -> str:
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
    p = argparse.ArgumentParser(prog='oms', description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('save').set_defaults(func=cmd_save)
    s = sub.add_parser('show'); s.add_argument('card_id', nargs='?', help='card id (default: $OMI_CARD_ID)'); s.set_defaults(func=cmd_show)
    sub.add_parser('list').set_defaults(func=cmd_list)
    t = sub.add_parser('tag'); t.add_argument('tag'); t.set_defaults(func=cmd_tag)
    u = sub.add_parser('untag'); u.add_argument('tag'); u.set_defaults(func=cmd_untag)
    l = sub.add_parser('lesson')
    l.add_argument('text', nargs='+', help='one-line lesson text')
    l.set_defaults(func=cmd_lesson)
    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
