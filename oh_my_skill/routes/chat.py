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

from oh_my_skill.shared import chat_store, claude_chat, claude_stream, codex_chat, logger
from oh_my_skill.shared.ai_providers import (
    active_chat_status, claude_status, codex_status, get_chat_model,
    get_chat_provider,
)
from oh_my_skill.shared.system_prompt import SKILL_SYSTEM_PROMPT


def _chat_backend(provider: str | None = None):
    provider = provider or get_chat_provider()
    return codex_chat if provider == 'codex' else claude_chat


def _chat_unavailable_reason(provider: str | None = None) -> str:
    if provider in (None, get_chat_provider()):
        status = active_chat_status()
    else:
        status = codex_status() if provider == 'codex' else claude_status()
    return '' if status.get('configured') else (status.get('reason') or 'AI not configured')

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


def _lookup_lessons_card() -> dict | None:
    """Return the first card tagged 'meta-lessons', if any.
    Used to inject self-maintained lessons into every CLAUDE.md."""
    try:
        conn = sqlite3.connect(SKILLCARDS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM cards').fetchall()
        conn.close()
    except Exception:
        return None
    for r in rows:
        try:
            tags = json.loads(r['tags'] or '[]')
        except Exception:
            tags = []
        if 'meta-lessons' in tags:
            d = dict(r); d['tags'] = tags
            return d
    return None


def _lessons_section() -> str:
    """A heading to splice into CLAUDE.md. Empty if no lessons card yet."""
    card = _lookup_lessons_card()
    if not card or not (card.get('content') or '').strip():
        return ("\n## Self-growing lessons\n\n"
                "_(no lessons yet — when the user has to correct you, "
                "run `oms-lesson \"<one-line summary>\"` BEFORE continuing.)_\n")
    body = card['content'].strip()
    return ("\n## Self-growing lessons (read these BEFORE editing — DO NOT repeat)\n\n"
            f"{body}\n\n"
            "_If the user has to correct you about something not yet listed above, "
            "run `oms-lesson \"<one-line summary>\"` BEFORE continuing the work — "
            "future sessions will read it and avoid the same mistake._\n")


def _codex_md_for_card(card: dict) -> str:
    return f"""# Card {card['id']}
id: {card['id']} | title: {card['title']!r} | tags: {card.get('tags') or []}
Edit ./card.md then run `oms-save`. H1 = title, rest = body.
"""


def _claude_md_for_card(card: dict) -> str:
    return f"""# Card {card['id']}
You are Claude by Anthropic.
id: {card['id']} | title: {card['title']!r} | tags: {card.get('tags') or []}
Edit ./card.md then run `oms-save`. H1 = title, rest = body.
"""


def _claude_md_workspace(project: str) -> str:
    return f"# {project} workspace\nEdit files, run `oms-save` to persist.\n"


_PREAMBLE_PATH = os.path.join(os.environ.get('OMI_DATA_DIR', '/data'), 'context.md')
_PREAMBLE_DEFAULT = """\
## CLI helpers (scoped to THIS card via $OMI_CARD_ID)
oms-save — save ./card.md back to DB
oms-tag <tag> / oms-untag <tag> — manage tags
oms-show — print current saved version
oms-lesson "<text>" — append a lesson to the lessons card
oms-guide — print the markdown style/feature reference (read before editing)
oms-refine — emit a prompt to rewrite THIS card to the house style/syntax

## CLI helpers (any card — also runnable as `oms <cmd>`)
oms add  --title T [--tags a,b] [--file F | --content S | <stdin>] — create
oms edit <id> [--title T] [--add-tag t] [--rm-tag t] [--file F | <stdin>] — update
oms rm   <id> --yes — delete
NEVER hand-escape JSON for content — pass --file or pipe via stdin.

## How to edit
1. Read `./card.md` first.
2. Edit — preserve the H1 unless asked to rename.
3. Run `oms-save` to persist. The web UI picks up changes live.

## Style
Concise, scannable, table-heavy, fenced code blocks for multi-line snippets.

## Markdown features
For supported syntax (collapsible H1/H2, callouts incl. code-in-callout,
collapsible code ```lang+/-, tabs, timeline, wiki-links, image sizing, math,
frontmatter), read the reference first: `oms-guide | head -250`.
To reformat the current card to that reference, run `oms-refine` and apply it.
"""


def _get_preamble() -> str:
    """Read the user-editable preamble file, seeding defaults if missing."""
    if not os.path.exists(_PREAMBLE_PATH):
        with open(_PREAMBLE_PATH, 'w') as f:
            f.write(_PREAMBLE_DEFAULT)
    try:
        with open(_PREAMBLE_PATH) as f:
            return f.read()
    except OSError:
        return _PREAMBLE_DEFAULT


def _ensure_workspace(project: str, card_id: str = '', provider: str = 'claude') -> tuple[str, dict | None]:
    """Returns (cwd, card_row). Writes CLAUDE.md (for Claude) or AGENTS.md
    (for Codex) so the AI always sees current state. Writes ./card.md when scoped."""
    cwd = os.path.join(PROJECTS_DIR, _safe_slug(project))
    os.makedirs(cwd, exist_ok=True)
    # Provider-appropriate project instructions file; remove the other
    # so the AI doesn't ingest both (doubles token cost).
    inst_file = 'AGENTS.md' if provider == 'codex' else 'CLAUDE.md'
    stale_file = 'CLAUDE.md' if provider == 'codex' else 'AGENTS.md'
    stale_path = os.path.join(cwd, stale_file)
    if os.path.exists(stale_path):
        try:
            os.remove(stale_path)
        except OSError:
            pass
    card = _lookup_card(card_id) if card_id else None
    if card:
        meta = card.get('metadata') or {}
        if isinstance(meta, str):
            try:
                import json as _j
                meta = _j.loads(meta)
            except Exception:
                meta = {}
        fm_lines = []
        if meta.get('parent_id'):
            fm_lines.append(f"parent: {meta['parent_id']}")
        if meta.get('links'):
            fm_lines.append('links: [' + ', '.join(meta['links']) + ']')
        fm_block = ('---\n' + '\n'.join(fm_lines) + '\n---\n\n') if fm_lines else ''
        with open(os.path.join(cwd, 'card.md'), 'w') as f:
            f.write(f"{fm_block}# {card['title']}\n\n{card.get('content', '')}\n")
        inst_content = _codex_md_for_card(card) if provider == 'codex' else _claude_md_for_card(card)
        # Both are now minimal — detailed context goes in first-turn preamble
        with open(os.path.join(cwd, inst_file), 'w') as f:
            f.write(inst_content)
    else:
        md = os.path.join(cwd, inst_file)
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
    provider = get_chat_provider()
    backend = _chat_backend(provider)

    cwd, card = _ensure_workspace(project, card_id, provider=provider)
    if card_id and not card:
        return jsonify({'error': f'card {card_id} not found'}), 404

    if force_new or not card_id:
        chat_id = backend.new_session(cwd, card_id=card_id)
        history, resumed = [], False
    else:
        chat_id, created = backend.find_or_create_for_card(cwd, card_id)
        sess = backend.get_session(chat_id) or {}
        history = sess.get('history') or []
        resumed = not created

    return jsonify({
        'chat_id': chat_id, 'project': project, 'cwd': cwd,
        'provider': provider,
        'model': get_chat_model(provider),
        'card_id': card_id or None,
        'card_meta': _card_meta_for(card),
        'resumed': resumed,
        'history': history,
    })


@chat_bp.route('/api/chat/<chat_id>', methods=['GET'])
def chat_history(chat_id):
    s = chat_store.get(chat_id)
    if not s:
        return jsonify({'error': 'unknown chat_id'}), 404
    return jsonify({
        'chat_id': chat_id, 'cwd': s['cwd'],
        'provider': s.get('provider') or 'claude',
        'model': get_chat_model(s.get('provider') or 'claude'),
        'card_id': s.get('card_id'),
        'closed': bool(s.get('closed')),
        'claude_session_id': s.get('claude_session_id'),
        'provider_session_id': s.get('provider_session_id'),
        'history': s['history'],
    })


@chat_bp.route('/api/chat/<chat_id>/send', methods=['POST'])
def chat_send(chat_id):
    data = request.get_json() or {}
    msg = (data.get('message') or '').strip()
    mode = data.get('mode') or 'edit'
    if mode not in ('edit', 'explain'):
        mode = 'edit'
    if not msg:
        return jsonify({'error': 'message required'}), 400
    s = chat_store.get(chat_id)
    if not s:
        return jsonify({'error': 'unknown chat_id'}), 404
    # Frontend may override provider (e.g. user switched dropdown mid-chat)
    requested_provider = (data.get('provider') or '').strip().lower()
    provider = s.get('provider') or get_chat_provider()
    if requested_provider in ('claude', 'codex') and requested_provider != provider:
        # Provider mismatch — create a new session with the correct provider
        project = s['cwd'].rsplit('/', 1)[-1]
        card_id = s.get('card_id') or ''
        new_backend = _chat_backend(requested_provider)
        cwd, _ = _ensure_workspace(project, card_id, provider=requested_provider)
        new_chat_id = new_backend.new_session(cwd, card_id=card_id)
        chat_id = new_chat_id
        s = chat_store.get(chat_id)
        provider = requested_provider
    backend = _chat_backend(provider)
    reason = _chat_unavailable_reason(provider)
    if reason:
        return jsonify({'error': f'AI not configured: {reason}'}), 503

    # Refresh card workspace every turn so AI sees latest DB state
    if s.get('card_id'):
        _ensure_workspace(s['cwd'].rsplit('/', 1)[-1], s['card_id'], provider=provider)

    # First turn of a new chat: prepend the shared preamble (CLI helpers,
    # style brain, etc.) so the AI has full context without bloating the
    # project instructions file on every subsequent turn.
    history = s.get('history') or []
    is_first_turn = not any(m.get('role') == 'user' for m in history)
    actual_msg = msg
    if is_first_turn and s.get('card_id'):
        preamble = _get_preamble().strip()
        if preamble:
            actual_msg = preamble + '\n\n---\n\n' + msg

    # Capture the full prompt that will be sent to the AI for debug inspection
    _debug_prompt = ''
    _debug_cmd = ''
    if provider == 'codex':
        from oh_my_skill.shared.codex_chat import _initial_prompt, _build_cmd
        _debug_prompt = _initial_prompt(s, actual_msg, mode)
        _debug_cmd_list, _ = _build_cmd(s, _debug_prompt, mode)
        _debug_cmd = ' '.join(_debug_cmd_list[:-1]) + ' <prompt>'
    else:
        _debug_cmd = f'claude -p --model {get_chat_model(provider)} --allowedTools ...'
        _debug_prompt = actual_msg

    # List workspace files for token audit
    import glob as _g
    _wfiles = {}
    for fp in _g.glob(os.path.join(s['cwd'], '*')):
        if os.path.isfile(fp):
            try:
                _wfiles[os.path.basename(fp)] = os.path.getsize(fp)
            except OSError:
                pass

    log_id = logger.start('chat-send', model=get_chat_model(provider),
                          detail=msg[:200], chat_id=chat_id,
                          card_id=s.get('card_id') or '', mode=mode, provider=provider,
                          prompt=_debug_prompt, cmd=_debug_cmd,
                          workspace_files=_wfiles, cwd=s['cwd'])

    @stream_with_context
    def gen():
        t0 = time.time()
        full_text = ''
        had_error = False
        try:
            for chunk in backend.send(chat_id, actual_msg, mode=mode):
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
    s = chat_store.get(chat_id)
    if not s:
        return jsonify({'stopped': False})
    return jsonify({'stopped': _chat_backend(s.get('provider')).stop(chat_id)})


@chat_bp.route('/api/chat', methods=['GET'])
def chat_list():
    card_id = request.args.get('card_id')
    provider = request.args.get('provider') or get_chat_provider()
    return jsonify(_chat_backend(provider).list_sessions(
        limit=int(request.args.get('limit', 30)),
        card_id=card_id,
    ))


@chat_bp.route('/api/chat/<chat_id>/close', methods=['POST'])
def chat_close(chat_id):
    s = chat_store.get(chat_id)
    if s:
        _chat_backend(s.get('provider')).close_session(chat_id)
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/<chat_id>/reopen', methods=['POST'])
def chat_reopen(chat_id):
    """Reactivate a previously closed session."""
    s = chat_store.get(chat_id)
    if s:
        _chat_backend(s.get('provider')).reopen_session(chat_id)
    s = chat_store.get(chat_id)
    if not s:
        return jsonify({'error': 'unknown chat_id'}), 404
    return jsonify({'ok': True, 'provider': s.get('provider') or 'claude',
                    'model': get_chat_model(s.get('provider') or 'claude'),
                    'history': s.get('history') or []})


@chat_bp.route('/api/chat/<chat_id>/clear', methods=['POST'])
def chat_clear(chat_id):
    """Clear history + claude context — next send starts a fresh conversation."""
    s = chat_store.get(chat_id)
    if not s:
        return jsonify({'error': 'unknown chat_id'}), 404
    _chat_backend(s.get('provider')).clear_session_context(chat_id)
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/<chat_id>', methods=['DELETE'])
def chat_delete(chat_id):
    s = chat_store.get(chat_id)
    if s:
        _chat_backend(s.get('provider')).delete_session(chat_id)
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
    cstat = claude_status()
    reason = '' if cstat.get('configured') else cstat.get('reason')
    if reason:
        return jsonify({'error': f'AI not configured: {reason}'}), 503
    job_id = data.get('job_id') or str(uuid.uuid4())

    log_id = logger.start('extract-skill', model=get_chat_model('claude'),
                          detail=rough[:200], job_id=job_id)

    @stream_with_context
    def gen():
        yield f"data: {json.dumps({'job_id': job_id, 'log_id': log_id})}\n\n"
        full = ''
        err = ''
        t0 = time.time()
        try:
            for chunk in claude_stream.stream(job_id, SKILL_SYSTEM_PROMPT, rough,
                                              model=get_chat_model('claude')):
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
