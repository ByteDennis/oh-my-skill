"""Chat + Extract routes for oh-my-skill.

`/api/chat/*` — Manus-style conversational panel scoped to a card. Lifted
from oh-my-image/routes/chat.py but the workspace is per-card, the CLAUDE.md
points at `card.md`, and the `oms-save` CLI persists changes back to the DB.

`/api/extract` — one-shot: rough user notes in → polished skill card out.
Streams text deltas to the UI, parses the title + body + TAGS line, and
creates a new card row when streaming completes. Returns the new card_id
so the UI can open it.
"""
import json
import os
import re
import sqlite3
import time
import uuid

from flask import Blueprint, Response, jsonify, request, stream_with_context

from oh_my_skill.shared import claude_chat, claude_stream, logger
from oh_my_skill.shared.config import get_setting
from oh_my_skill.shared.system_prompt import SKILL_SYSTEM_PROMPT


def _ai_unavailable_reason() -> str:
    """Returns '' if AI is reachable, else a short reason string."""
    import glob as _glob, shutil as _shutil
    bin_path = os.environ.get('CLAUDE_BIN') or _shutil.which('claude')
    if not bin_path:
        cands = sorted(_glob.glob('/opt/claude/versions/*'), key=os.path.getmtime)
        bin_path = cands[-1] if cands else None
    if not bin_path:
        return 'claude binary not found'
    if not get_setting('global', 'claude_code_oauth_token'):
        return 'no Claude OAuth token (Settings → Claude OAuth token)'
    return ''

chat_bp = Blueprint('chat', __name__)

DATA_DIR = os.environ.get('OMI_DATA_DIR', '/data')
PROJECTS_DIR = os.path.join(DATA_DIR, 'projects')
SKILLCARDS_DB = os.environ.get('SKILLCARDS_DB', '/data/skillcards.db')


# ── Card lookup / workspace prep ──────────────────────────────────
def _safe_slug(s: str) -> str:
    return ''.join(c if c.isalnum() or c in '-_' else '-' for c in (s or 'default'))


def _lookup_card(card_id: str) -> dict | None:
    if not card_id:
        return None
    conn = sqlite3.connect(SKILLCARDS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d['tags'] = json.loads(d.get('tags') or '[]')
    except Exception:
        d['tags'] = []
    return d


def _claude_md_for_card(card: dict) -> str:
    return f"""# Editing skill card {card['id']}

You are scoped to a single skill card. The user is editing it in the
oh-my-skill web UI right now — they want help refining it.

## Current card

- **id**: `{card['id']}`
- **title**: {card['title']!r}
- **tags**: {card.get('tags') or []}
- **updated**: `{card.get('updated_at', '')}`

## Where to make edits

The card content lives in `./card.md` in this folder. Edit it there.
The H1 in card.md becomes the title; everything below becomes the body.

## CLI helpers

```bash
oms-save        # save ./card.md back to the DB (title from H1, body from rest)
oms-tag <tag>   # add a tag
oms-untag <tag> # remove a tag
oms-show        # print the current saved version
```

## How to make changes

1. **Read** `./card.md` first.
2. Edit it — preserve the H1 unless asked to rename.
3. Run `oms-save` to persist back. The web UI picks up your changes.
4. The user sees the UI live — confirm any destructive change in one
   short sentence first, then act.

## Style brain

The skill-card style brain is appended to your system prompt. Follow it:
精简, scannable, table-heavy, fenced code blocks for multi-line snippets.
"""


def _claude_md_workspace(project: str) -> str:
    return f"""# {project} workspace

Free-form oh-my-skill workspace.

## Tools available

- `oms-save` / `oms-tag` / `oms-untag` / `oms-show` — card CLI helpers
- `Read`, `Edit`, `Write`, `Bash`, `Glob`, `Grep`

The skill-card style brain is appended to your system prompt.
"""


def _ensure_workspace(project: str, card_id: str = '') -> tuple[str, dict | None]:
    """Returns (cwd, card_row). Writes a fresh CLAUDE.md every call so the AI
    always sees current state. Writes ./card.md when scoped."""
    cwd = os.path.join(PROJECTS_DIR, _safe_slug(project))
    os.makedirs(cwd, exist_ok=True)
    card = _lookup_card(card_id) if card_id else None
    if card:
        with open(os.path.join(cwd, 'card.md'), 'w') as f:
            f.write(f"# {card['title']}\n\n{card.get('content', '')}\n")
        with open(os.path.join(cwd, 'CLAUDE.md'), 'w') as f:
            f.write(_claude_md_for_card(card))
    else:
        md = os.path.join(cwd, 'CLAUDE.md')
        if not os.path.exists(md):
            with open(md, 'w') as f:
                f.write(_claude_md_workspace(project))
    return cwd, card


# ── Manus-style chat routes ──────────────────────────────────────
def _card_meta_for(card):
    if not card:
        return None
    return {
        'title': card['title'],
        'tags': card.get('tags') or [],
        'updated_at': card.get('updated_at') or '',
    }


@chat_bp.route('/api/chat/new', methods=['POST'])
def chat_new():
    """Open a chat session.

    Default: if `card_id` is given, RESUME the latest non-closed chat for
    that card. Pass `force_new: true` to start fresh."""
    data = request.get_json() or {}
    card_id = (data.get('card_id') or '').strip()
    force_new = bool(data.get('force_new', False))
    project = data.get('project') or (f'card-{card_id[:8]}' if card_id else 'default')

    cwd, card = _ensure_workspace(project, card_id)
    if card_id and not card:
        return jsonify({'error': f'card {card_id} not found'}), 404

    if force_new or not card_id:
        chat_id = claude_chat.new_session(cwd, card_id=card_id)
        history, resumed = [], False
    else:
        chat_id, created = claude_chat.find_or_create_for_card(cwd, card_id)
        sess = claude_chat.get_session(chat_id) or {}
        history = sess.get('history') or []
        resumed = not created

    return jsonify({
        'chat_id': chat_id, 'project': project, 'cwd': cwd,
        'card_id': card_id or None,
        'card_meta': _card_meta_for(card),
        'resumed': resumed,
        'history': history,
    })


@chat_bp.route('/api/chat/<chat_id>', methods=['GET'])
def chat_history(chat_id):
    s = claude_chat.get_session(chat_id)
    if not s:
        return jsonify({'error': 'unknown chat_id'}), 404
    return jsonify({
        'chat_id': chat_id, 'cwd': s['cwd'],
        'card_id': s.get('card_id'),
        'claude_session_id': s.get('claude_session_id'),
        'history': s['history'],
    })


@chat_bp.route('/api/chat/<chat_id>/send', methods=['POST'])
def chat_send(chat_id):
    data = request.get_json() or {}
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'error': 'message required'}), 400
    s = claude_chat.get_session(chat_id)
    if not s:
        return jsonify({'error': 'unknown chat_id'}), 404
    reason = _ai_unavailable_reason()
    if reason:
        return jsonify({'error': f'AI not configured: {reason}'}), 503

    # Refresh card workspace every turn so AI sees latest DB state
    if s.get('card_id'):
        _ensure_workspace(s['cwd'].rsplit('/', 1)[-1], s['card_id'])

    log_id = logger.start('chat-send', model='sonnet',
                          detail=msg[:200], chat_id=chat_id,
                          card_id=s.get('card_id') or '')

    @stream_with_context
    def gen():
        t0 = time.time()
        full_text = ''
        had_error = False
        try:
            for chunk in claude_chat.send(chat_id, msg):
                if chunk.startswith('data: ') and chunk != 'data: [DONE]\n\n':
                    try:
                        ev = json.loads(chunk[6:].rstrip())
                        if ev.get('type') == 'text_delta':
                            full_text += ev.get('text', '')
                        elif ev.get('type') == 'error':
                            had_error = True
                    except Exception:
                        pass
                yield chunk
        finally:
            logger.finish(log_id,
                          status='error' if had_error else 'done',
                          duration_ms=int((time.time() - t0) * 1000),
                          response_summary=full_text[:200])

    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@chat_bp.route('/api/chat/<chat_id>/stop', methods=['POST'])
def chat_stop(chat_id):
    return jsonify({'stopped': claude_chat.stop(chat_id)})


@chat_bp.route('/api/chat', methods=['GET'])
def chat_list():
    card_id = request.args.get('card_id')
    return jsonify(claude_chat.list_sessions(
        limit=int(request.args.get('limit', 30)),
        card_id=card_id,
    ))


@chat_bp.route('/api/chat/<chat_id>/close', methods=['POST'])
def chat_close(chat_id):
    claude_chat.close_session(chat_id)
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/<chat_id>', methods=['DELETE'])
def chat_delete(chat_id):
    claude_chat.delete_session(chat_id)
    return jsonify({'ok': True})


# ── One-shot: rough notes → skill card ──────────────────────────────
@chat_bp.route('/api/extract', methods=['POST'])
def extract_skill():
    """Stream Claude with SKILL_SYSTEM_PROMPT to convert rough notes into a
    polished skill card. On completion, parses the output and creates a new
    card row. The final SSE event is `{done: true, card_id, title, tags}`."""
    data = request.get_json() or {}
    rough = (data.get('notes') or '').strip()
    if not rough:
        return jsonify({'error': 'notes required'}), 400
    reason = _ai_unavailable_reason()
    if reason:
        return jsonify({'error': f'AI not configured: {reason}'}), 503
    job_id = data.get('job_id') or str(uuid.uuid4())

    log_id = logger.start('extract-skill', model='sonnet',
                          detail=rough[:200], job_id=job_id)

    @stream_with_context
    def gen():
        yield f"data: {json.dumps({'job_id': job_id, 'log_id': log_id})}\n\n"
        full = ''
        err = ''
        t0 = time.time()
        try:
            for chunk in claude_stream.stream(job_id, SKILL_SYSTEM_PROMPT, rough,
                                              model='sonnet'):
                if chunk.startswith('data: '):
                    try:
                        ev = json.loads(chunk[6:].rstrip())
                        if 'text' in ev:
                            full += ev['text']
                        if 'error' in ev:
                            err = ev['error']
                    except Exception:
                        pass
                yield chunk

            if not err and full.strip():
                title, body, tags = _parse_skill_output(full)
                card_id = _create_card(title, body, tags)
                yield f"data: {json.dumps({'created': True, 'card_id': card_id, 'title': title, 'tags': tags})}\n\n"
        finally:
            logger.finish(log_id,
                          status='error' if err else 'done',
                          error=err,
                          duration_ms=int((time.time() - t0) * 1000),
                          response_summary=full[:300])

    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@chat_bp.route('/api/extract/cancel', methods=['POST'])
def cancel_extract():
    data = request.get_json() or {}
    return jsonify({'cancelled': claude_stream.cancel(data.get('job_id', ''))})


def _parse_skill_output(text: str) -> tuple[str, str, list[str]]:
    """Split Claude's output into (title, body, tags). Tolerant to small
    deviations: title can be missing → 'Untitled'; tags line can be absent."""
    raw = text.strip()
    # Strip outer ```markdown fences if Claude added them despite instructions
    if raw.startswith('```'):
        raw = re.sub(r'^```\w*\s*\n', '', raw)
        raw = re.sub(r'\n```\s*$', '', raw)
    lines = raw.splitlines()

    # Tags: last line starting "TAGS:" (case-insensitive)
    tags: list[str] = []
    if lines:
        for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
            m = re.match(r'\s*TAGS\s*:\s*(.*)', lines[i], re.IGNORECASE)
            if m:
                tags = [t.strip().lower() for t in m.group(1).split(',') if t.strip()]
                lines = lines[:i]
                break

    # Drop trailing blank lines
    while lines and not lines[-1].strip():
        lines.pop()

    # Title: first H1
    title = 'Untitled'
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.startswith('# '):
            title = ln[2:].strip() or 'Untitled'
            body_start = i + 1
            break
    body = '\n'.join(lines[body_start:]).lstrip('\n')
    return title, body, tags[:8]


def _create_card(title: str, content: str, tags: list[str]) -> str:
    """Insert a new card row. Returns the new id."""
    from datetime import datetime
    cid = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat() + 'Z'
    conn = sqlite3.connect(SKILLCARDS_DB)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        '''INSERT INTO cards (id, title, content, tags, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (cid, title, content, json.dumps(tags), '{}', now, now),
    )
    conn.commit()
    conn.close()
    return cid
