"""Conversational Claude with rich event parsing.

Spawns `claude -p --output-format stream-json --verbose [--resume <id>]`,
parses the FULL Anthropic stream-event format, and yields typed events the
chat UI can render as message bubbles, collapsible thinking blocks, and
tool-use cards.

Sessions are stored in-process (dict). Each session = workspace cwd +
claude session_id (assigned by claude on first turn, used with --resume
on subsequent turns) + message history.

Lifted from the slim parser in shared/claude_stream.py:_stream_claude
(devhub/routes/slider.py:1956-2020) but extends it to all stream_event
sub-types.
"""
import glob
import json
import os
import subprocess
import threading
import time
import uuid

from . import chat_store
from .config import get_setting

CLAUDE_BIN = os.environ.get('CLAUDE_BIN') or (
    sorted(glob.glob('/opt/claude/versions/*'), key=os.path.getmtime)[-1]
    if glob.glob('/opt/claude/versions/*') else 'claude'
)

_lock = threading.Lock()
_active_procs: dict = {}  # chat_id -> Popen


def new_session(cwd: str, card_id: str = '') -> str:
    """Create a new chat session anchored to a workspace folder."""
    chat_id = str(uuid.uuid4())
    chat_store.create(chat_id, cwd, card_id=card_id)
    return chat_id


def get_session(chat_id: str) -> dict | None:
    """Returns the persisted session as a dict (with `history` re-hydrated)."""
    s = chat_store.get(chat_id)
    if not s:
        return None
    s['claude_session_id'] = s.get('claude_session_id') or None
    return s


def find_or_create_for_card(cwd: str, card_id: str) -> tuple[str, bool]:
    """Resume the latest non-closed chat for card_id, or create a new one."""
    if card_id:
        existing = chat_store.find_active_for_card(card_id)
        if existing:
            return existing['id'], False
    return new_session(cwd, card_id=card_id), True


def list_sessions(limit: int = 30, card_id: str | None = None) -> list[dict]:
    rows = chat_store.list_recent(limit=limit, card_id=card_id)
    return [{
        'chat_id': s['id'], 'cwd': s['cwd'],
        'card_id': s.get('card_id') or '',
        'claude_session_id': s.get('claude_session_id') or '',
        'message_count': len(s.get('history') or []),
        'created_at': s['created_at'], 'updated_at': s['updated_at'],
        'closed': bool(s.get('closed')),
        'preview': _first_user_text(s)[:120],
    } for s in rows]


def close_session(chat_id: str):
    chat_store.close(chat_id)


def delete_session(chat_id: str):
    chat_store.delete(chat_id)


def _first_user_text(session: dict) -> str:
    for m in session.get('history') or []:
        if m.get('role') == 'user':
            for b in m.get('blocks', []):
                if b.get('type') == 'text':
                    return b.get('text', '')
    return ''


def stop(chat_id: str) -> bool:
    proc = _active_procs.get(chat_id)
    if not proc:
        return False
    try:
        proc.kill()
        return True
    except Exception:
        return False


def _build_cmd(session: dict) -> list[str]:
    cmd = [
        CLAUDE_BIN, '-p',
        '--output-format', 'stream-json',
        '--verbose',
        '--permission-mode', 'acceptEdits',
        '--allowedTools', 'Edit,Write,Read,Bash,Glob,Grep',
        '--model', 'sonnet',
    ]
    # Append the SKILL_SYSTEM_PROMPT brain so chat-edits inherit the
    # extraction style. Render-on-disk path is set up by app.py at boot.
    brain = os.path.join(os.environ.get('OMI_DATA_DIR', '/data'),
                         '.skill-system-prompt.md')
    if os.path.isfile(brain):
        cmd += ['--append-system-prompt-file', brain]
    if session['claude_session_id']:
        cmd += ['--resume', session['claude_session_id']]
    return cmd


def _build_env() -> dict:
    env = os.environ.copy()
    env['HOME'] = env.get('HOME', '/root')
    token = get_setting('global', 'claude_code_oauth_token')
    if token:
        env['CLAUDE_CODE_OAUTH_TOKEN'] = token
    return env


def send(chat_id: str, user_message: str):
    """Stream typed events for one user turn.

    Yields strings already wrapped as SSE data lines:
        "data: {...}\\n\\n"

    Event types emitted (the wire format the chat UI consumes):
        {type: 'session_start', chat_id, claude_session_id?}
        {type: 'user_message', text}
        {type: 'message_start'}
        {type: 'block_start', block_type: 'text'|'thinking'|'tool_use', index, id?, name?}
        {type: 'text_delta', index, text}
        {type: 'thinking_delta', index, text}
        {type: 'tool_input_delta', index, partial_json}
        {type: 'block_stop', index}
        {type: 'tool_result', tool_use_id, content, is_error}
        {type: 'message_stop'}
        {type: 'turn_done', claude_session_id, duration_ms, cost_usd?, num_turns?}
        {type: 'error', message}
        SSE sentinel: "data: [DONE]\\n\\n"
    """
    sess = chat_store.get(chat_id)
    if not sess:
        yield _ev({'type': 'error', 'message': f'unknown chat_id {chat_id}'})
        yield "data: [DONE]\n\n"
        return

    history = sess.get('history') or []
    history.append({'role': 'user', 'blocks': [{'type': 'text', 'text': user_message}],
                    'ts': time.time()})
    chat_store.update_history(chat_id, history)

    yield _ev({'type': 'session_start', 'chat_id': chat_id,
               'claude_session_id': sess.get('claude_session_id') or None})
    yield _ev({'type': 'user_message', 'text': user_message})

    cmd = _build_cmd({'claude_session_id': sess.get('claude_session_id') or None})
    env = _build_env()
    t0 = time.time()

    try:
        proc = subprocess.Popen(
            cmd, cwd=sess['cwd'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        # Mutable copy so we can update claude_session_id when system event arrives
        claude_sid = sess.get('claude_session_id') or None
    except FileNotFoundError as e:
        yield _ev({'type': 'error', 'message': f'claude binary not found: {e}'})
        yield "data: [DONE]\n\n"
        return

    _active_procs[chat_id] = proc
    try:
        proc.stdin.write(user_message)
        proc.stdin.close()

        # Track per-block state to assemble assistant message + serve UI
        assistant_blocks: dict = {}   # index -> {type, text|thinking|tool_use {name, input_json}}
        # Tool-use partial JSON accumulators (for input_json_delta)
        tool_partial: dict = {}       # index -> str
        # Counter for synthesizing block indices when the wrapper "assistant"
        # event arrives (no native indices in that path).
        next_idx = [0]

        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get('type', '')

            if etype == 'system':
                # init event has the session id we need for --resume
                sid = event.get('session_id')
                if sid and not claude_sid:
                    claude_sid = sid
                continue

            if etype == 'assistant':
                # In `-p` mode, claude emits whole assistant turns as one event
                # (NOT stream_event deltas). Each content block becomes a
                # synthesized start → delta → stop sequence so the UI renders
                # uniformly with both modes.
                msg = event.get('message', {}) or {}
                content = msg.get('content', []) or []
                yield _ev({'type': 'message_start'})
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get('type', '')
                    idx = next_idx[0]; next_idx[0] += 1
                    if btype == 'text':
                        text = block.get('text', '') or ''
                        yield _ev({'type': 'block_start', 'block_type': 'text', 'index': idx})
                        if text:
                            yield _ev({'type': 'text_delta', 'index': idx, 'text': text})
                        yield _ev({'type': 'block_stop', 'index': idx})
                        assistant_blocks[idx] = {'type': 'text', 'text': text}
                    elif btype == 'thinking':
                        text = block.get('thinking', '') or ''
                        yield _ev({'type': 'block_start', 'block_type': 'thinking', 'index': idx})
                        if text:
                            yield _ev({'type': 'thinking_delta', 'index': idx, 'text': text})
                        yield _ev({'type': 'block_stop', 'index': idx})
                        assistant_blocks[idx] = {'type': 'thinking', 'text': text}
                    elif btype == 'tool_use':
                        name = block.get('name', '')
                        tid = block.get('id', '')
                        input_obj = block.get('input', {}) or {}
                        yield _ev({'type': 'block_start', 'block_type': 'tool_use',
                                   'index': idx, 'name': name, 'id': tid})
                        # Emit the full input as a single delta so the UI's
                        # tool-use card populates without needing to assemble
                        # partial chunks.
                        try:
                            yield _ev({'type': 'tool_input_delta', 'index': idx,
                                       'partial_json': json.dumps(input_obj)})
                        except Exception:
                            yield _ev({'type': 'tool_input_delta', 'index': idx,
                                       'partial_json': '{}'})
                        yield _ev({'type': 'block_stop', 'index': idx})
                        assistant_blocks[idx] = {'type': 'tool_use', 'name': name,
                                                 'id': tid, 'input': input_obj}
                yield _ev({'type': 'message_stop'})
                continue

            if etype == 'stream_event':
                inner = event.get('event', {})
                itype = inner.get('type', '')

                if itype == 'message_start':
                    yield _ev({'type': 'message_start'})

                elif itype == 'content_block_start':
                    idx = inner.get('index', 0)
                    cb = inner.get('content_block', {}) or {}
                    btype = cb.get('type', 'text')
                    out = {'type': 'block_start', 'block_type': btype, 'index': idx}
                    if btype == 'tool_use':
                        out['name'] = cb.get('name', '')
                        out['id'] = cb.get('id', '')
                        assistant_blocks[idx] = {'type': 'tool_use',
                                                 'name': cb.get('name', ''),
                                                 'id': cb.get('id', ''),
                                                 'input': {}}
                        tool_partial[idx] = ''
                    elif btype == 'thinking':
                        assistant_blocks[idx] = {'type': 'thinking', 'text': ''}
                    else:  # text
                        assistant_blocks[idx] = {'type': 'text', 'text': ''}
                    yield _ev(out)

                elif itype == 'content_block_delta':
                    idx = inner.get('index', 0)
                    delta = inner.get('delta', {}) or {}
                    dtype = delta.get('type', '')
                    if dtype == 'text_delta':
                        txt = delta.get('text', '')
                        if idx in assistant_blocks:
                            assistant_blocks[idx]['text'] = assistant_blocks[idx].get('text', '') + txt
                        yield _ev({'type': 'text_delta', 'index': idx, 'text': txt})
                    elif dtype == 'thinking_delta':
                        txt = delta.get('thinking', '')
                        if idx in assistant_blocks:
                            assistant_blocks[idx]['text'] = assistant_blocks[idx].get('text', '') + txt
                        yield _ev({'type': 'thinking_delta', 'index': idx, 'text': txt})
                    elif dtype == 'input_json_delta':
                        partial = delta.get('partial_json', '')
                        tool_partial[idx] = tool_partial.get(idx, '') + partial
                        yield _ev({'type': 'tool_input_delta', 'index': idx,
                                   'partial_json': partial})
                    elif dtype == 'signature_delta':
                        # opaque signature for thinking — not displayed
                        pass

                elif itype == 'content_block_stop':
                    idx = inner.get('index', 0)
                    # Finalize tool_use input from accumulated partial JSON
                    if idx in tool_partial and idx in assistant_blocks and assistant_blocks[idx]['type'] == 'tool_use':
                        try:
                            assistant_blocks[idx]['input'] = json.loads(tool_partial[idx]) if tool_partial[idx] else {}
                        except json.JSONDecodeError:
                            assistant_blocks[idx]['input'] = {'_raw': tool_partial[idx]}
                    yield _ev({'type': 'block_stop', 'index': idx})

                elif itype == 'message_delta':
                    pass  # carries stop_reason, not needed for UI

                elif itype == 'message_stop':
                    yield _ev({'type': 'message_stop'})

            elif etype == 'user':
                # claude's wrapper "user" event surfaces tool_result blocks coming back from the host
                msg = event.get('message', {}) or {}
                content = msg.get('content', []) or []
                for c in content:
                    if isinstance(c, dict) and c.get('type') == 'tool_result':
                        out = {
                            'type': 'tool_result',
                            'tool_use_id': c.get('tool_use_id', ''),
                            'is_error': bool(c.get('is_error', False)),
                            'content': _flatten_tool_result_content(c.get('content', '')),
                        }
                        yield _ev(out)

            elif etype == 'result':
                # Final summary
                sid = event.get('session_id')
                if sid and not claude_sid:
                    claude_sid = sid
                # Append assistant turn to history (in order)
                ordered = [assistant_blocks[i] for i in sorted(assistant_blocks.keys())]
                if ordered:
                    history.append({'role': 'assistant', 'blocks': ordered,
                                    'ts': time.time()})
                # Persist updated history + claude session id so the next
                # /api/chat/<id>/send can --resume from where we left off.
                chat_store.update_history(chat_id, history, claude_session_id=claude_sid)
                yield _ev({
                    'type': 'turn_done',
                    'claude_session_id': claude_sid,
                    'duration_ms': int((time.time() - t0) * 1000),
                    'cost_usd': event.get('total_cost_usd'),
                    'num_turns': event.get('num_turns'),
                })

        proc.wait(timeout=5)
        if proc.returncode and proc.returncode != 0:
            err = proc.stderr.read()[:600] if proc.stderr else ''
            if err:
                yield _ev({'type': 'error', 'message': err})
        yield "data: [DONE]\n\n"

    finally:
        _active_procs.pop(chat_id, None)


def _flatten_tool_result_content(content) -> str:
    """tool_result content can be a string or a list of blocks. Coerce to str for UI."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get('type') == 'text':
                    parts.append(c.get('text', ''))
                elif c.get('type') == 'image':
                    parts.append('[image]')
                else:
                    parts.append(json.dumps(c)[:400])
            else:
                parts.append(str(c))
        return '\n'.join(parts)
    return str(content)


def _ev(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"
