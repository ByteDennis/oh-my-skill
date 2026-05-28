"""Conversational Codex support using `codex exec --json`.

The exec JSONL protocol emits lifecycle updates for typed items rather than
Claude-style content block deltas. This adapter maps those items into the
existing chat block contract; finer token/tool deltas require app-server.
"""
import json
import os
import subprocess
import threading
import time
import uuid

from . import chat_store
from .ai_providers import get_chat_model

CODEX_BIN = os.environ.get('CODEX_BIN') or 'codex'

_lock = threading.Lock()
_active_procs: dict = {}  # chat_id -> Popen


def new_session(cwd: str, card_id: str = '') -> str:
    chat_id = str(uuid.uuid4())
    chat_store.create(chat_id, cwd, card_id=card_id, provider='codex')
    return chat_id


def get_session(chat_id: str) -> dict | None:
    s = chat_store.get(chat_id)
    if not s or (s.get('provider') or 'claude') != 'codex':
        return None
    s['provider_session_id'] = s.get('provider_session_id') or None
    return s


def find_or_create_for_card(cwd: str, card_id: str) -> tuple[str, bool]:
    if card_id:
        existing = chat_store.find_active_for_card(card_id, provider='codex')
        if existing:
            return existing['id'], False
    return new_session(cwd, card_id=card_id), True


def list_sessions(limit: int = 30, card_id: str | None = None) -> list[dict]:
    rows = chat_store.list_recent(limit=limit, card_id=card_id, provider='codex')
    return [{
        'chat_id': s['id'], 'cwd': s['cwd'],
        'card_id': s.get('card_id') or '',
        'provider': 'codex',
        'provider_session_id': s.get('provider_session_id') or '',
        'message_count': len(s.get('history') or []),
        'created_at': s['created_at'], 'updated_at': s['updated_at'],
        'closed': bool(s.get('closed')),
        'preview': _first_user_text(s)[:120],
    } for s in rows]


def close_session(chat_id: str):
    chat_store.close(chat_id)


def reopen_session(chat_id: str):
    chat_store.reopen(chat_id)


def clear_session_context(chat_id: str):
    chat_store.clear_context(chat_id)


def delete_session(chat_id: str):
    chat_store.delete(chat_id)


def stop(chat_id: str) -> bool:
    proc = _active_procs.get(chat_id)
    if not proc:
        return False
    try:
        proc.kill()
        return True
    except Exception:
        return False


def _first_user_text(session: dict) -> str:
    for m in session.get('history') or []:
        if m.get('role') == 'user':
            for b in m.get('blocks', []):
                if b.get('type') == 'text':
                    return b.get('text', '')
    return ''


def _build_cmd(session: dict, prompt: str, mode: str = 'edit') -> tuple[list[str], str]:
    # Use danger-full-access inside Docker — bwrap namespace sandboxing
    # fails without CAP_SYS_ADMIN; the container itself is the sandbox.
    sandbox = 'danger-full-access'
    last_msg_path = os.path.join(
        os.environ.get('OMI_DATA_DIR', '/data'),
        'codex-last-message',
        f"{session['id']}.txt",
    )
    os.makedirs(os.path.dirname(last_msg_path), exist_ok=True)
    if os.path.exists(last_msg_path):
        try:
            os.remove(last_msg_path)
        except OSError:
            pass
    base = [
        CODEX_BIN,
        'exec',
        '--json',
        '--model',
        get_chat_model('codex'),
        '--skip-git-repo-check',
        '--sandbox',
        sandbox,
        '--output-last-message',
        last_msg_path,
    ]
    if session.get('provider_session_id'):
        base += ['resume', session['provider_session_id']]
    base.append(prompt)
    return base, last_msg_path


def _initial_prompt(sess: dict, user_message: str, mode: str) -> str:
    return user_message


_CODEX_DATA_HOME = os.path.join(os.environ.get('OMI_DATA_DIR', '/data'), '.codex')

def _ensure_codex_home():
    """Create /data/.codex and sync auth.json from the host's ~/.codex."""
    os.makedirs(_CODEX_DATA_HOME, exist_ok=True)
    host_auth = os.path.join(os.path.expanduser('~'), '.codex', 'auth.json')
    data_auth = os.path.join(_CODEX_DATA_HOME, 'auth.json')
    try:
        if os.path.isfile(host_auth):
            import shutil
            # Only copy if source is newer or dest doesn't exist
            if not os.path.isfile(data_auth) or \
               os.path.getmtime(host_auth) > os.path.getmtime(data_auth):
                shutil.copy2(host_auth, data_auth)
    except OSError:
        pass

def _build_env(card_id: str = '') -> dict:
    env = os.environ.copy()
    env.pop('OPENAI_API_KEY', None)
    _ensure_codex_home()
    env['CODEX_HOME'] = _CODEX_DATA_HOME
    env['TMPDIR'] = os.path.join(_CODEX_DATA_HOME, 'tmp')
    os.makedirs(env['TMPDIR'], exist_ok=True)
    if card_id:
        env['OMI_CARD_ID'] = card_id
    return env


def send(chat_id: str, user_message: str, mode: str = 'edit'):
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
               'provider': 'codex',
               'provider_session_id': sess.get('provider_session_id') or None})
    yield _ev({'type': 'user_message', 'text': user_message})

    prompt = _initial_prompt(sess, user_message, mode)
    cmd, last_msg_path = _build_cmd(sess, prompt, mode=mode)
    t0 = time.time()
    assistant_text = ''
    text_started = False
    thread_id = sess.get('provider_session_id') or None
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    assistant_blocks: dict[int, dict] = {}
    block_state: dict[str, dict] = {}
    tool_inputs_sent: set[int] = set()
    cumulative_text: dict[str, str] = {}
    next_idx = 0
    message_open = False

    def ensure_message_start():
        nonlocal message_open
        if not message_open:
            message_open = True
            return [_ev({'type': 'message_start'})]
        return []

    def ensure_block(key: str, block_type: str, **extra) -> tuple[int, list[str]]:
        nonlocal next_idx
        state = block_state.get(key)
        if state:
            return state['index'], []
        idx = next_idx
        next_idx += 1
        payload = {'type': 'block_start', 'block_type': block_type, 'index': idx, **extra}
        events = ensure_message_start()
        events.append(_ev(payload))
        state = {'index': idx, 'type': block_type, 'closed': False, **extra}
        block_state[key] = state
        if block_type == 'text':
            assistant_blocks[idx] = {'type': 'text', 'text': ''}
        elif block_type == 'thinking':
            assistant_blocks[idx] = {'type': 'thinking', 'text': ''}
        elif block_type == 'tool_use':
            assistant_blocks[idx] = {
                'type': 'tool_use',
                'name': extra.get('name', ''),
                'id': extra.get('id', ''),
                'input': {},
            }
        return idx, events

    def stop_block(key: str) -> list[str]:
        state = block_state.get(key)
        if not state or state.get('closed'):
            return []
        state['closed'] = True
        return [_ev({'type': 'block_stop', 'index': state['index']})]

    def ensure_text_block() -> tuple[int, list[str]]:
        return ensure_block('text', 'text')

    def ensure_thinking_block(item: dict | None = None) -> tuple[int, list[str]]:
        item = item or {}
        key = f"thinking:{item.get('id') or item.get('item_id') or 'main'}"
        return ensure_block(key, 'thinking')

    def ensure_tool_block(item: dict | None = None) -> tuple[int | None, list[str]]:
        item = item or {}
        tool_name = _tool_name(item)
        tool_id = _tool_id(item)
        if not tool_name and not tool_id:
            return None, []
        key = f"tool:{tool_id or tool_name}:{item.get('id') or item.get('item_id') or ''}"
        return ensure_block(key, 'tool_use', name=tool_name or 'tool', id=tool_id)

    def append_text(text: str) -> list[str]:
        nonlocal assistant_text, text_started
        if not text:
            return []
        idx, events = ensure_text_block()
        assistant_text += text
        assistant_blocks[idx]['text'] = assistant_blocks[idx].get('text', '') + text
        events.append(_ev({'type': 'text_delta', 'index': idx, 'text': text}))
        text_started = True
        return events

    def append_thinking(text: str, item: dict | None = None) -> list[str]:
        if not text:
            return []
        idx, events = ensure_thinking_block(item)
        assistant_blocks[idx]['text'] = assistant_blocks[idx].get('text', '') + text
        events.append(_ev({'type': 'thinking_delta', 'index': idx, 'text': text}))
        return events

    def append_tool_input(item: dict | None = None, partial_json: str = '') -> list[str]:
        idx, events = ensure_tool_block(item)
        if idx is None:
            return []
        if partial_json and idx not in tool_inputs_sent:
            events.append(_ev({'type': 'tool_input_delta', 'index': idx, 'partial_json': partial_json}))
            tool_inputs_sent.add(idx)
            try:
                assistant_blocks[idx]['input'] = json.loads(partial_json)
            except json.JSONDecodeError:
                assistant_blocks[idx]['input'] = {'_raw': partial_json}
        return events

    def append_cumulative_text(key: str, text: str, item: dict | None = None) -> list[str]:
        previous = cumulative_text.get(key, '')
        if not text or text == previous:
            return []
        delta = text[len(previous):] if text.startswith(previous) else text
        cumulative_text[key] = text
        return append_thinking(delta, item)

    def append_cumulative_message(key: str, text: str) -> list[str]:
        previous = cumulative_text.get(key, '')
        if not text or text == previous:
            return []
        delta = text[len(previous):] if text.startswith(previous) else text
        cumulative_text[key] = text
        return append_text(delta)

    def emit_tool_result(item: dict, pending: bool) -> list[str]:
        content = _extract_tool_result_text(item)
        if not content and pending:
            return []
        return [_ev({
            'type': 'tool_result',
            'tool_use_id': _tool_id(item),
            'content': content or _tool_completion_summary(item),
            'is_error': _tool_result_is_error(item),
            'pending': pending,
        })]

    def close_all_blocks() -> list[str]:
        events: list[str] = []
        for key in list(block_state.keys()):
            events.extend(stop_block(key))
        if message_open:
            events.append(_ev({'type': 'message_stop'}))
        return events

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=sess['cwd'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_build_env(card_id=sess.get('card_id') or ''),
        )
    except FileNotFoundError as e:
        yield _ev({'type': 'error', 'message': f'codex binary not found: {e}'})
        yield "data: [DONE]\n\n"
        return

    _active_procs[chat_id] = proc
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get('type', '')
            if etype == 'thread.started':
                thread_id = event.get('thread_id') or thread_id
                continue
            if etype == 'turn.started':
                continue
            if etype == 'turn.completed':
                usage = event.get('usage') or {}
                input_tokens += usage.get('input_tokens', 0) or 0
                output_tokens += usage.get('output_tokens', 0) or 0
                cached_tokens = usage.get('cached_input_tokens', 0) or 0
                continue
            if etype == 'item.started':
                item = event.get('item') or {}
                if _is_tool_item(item):
                    for out in append_tool_input(item, _tool_input_json(item)):
                        yield out
                elif _is_reasoning_item(item):
                    _, pending = ensure_thinking_block(item)
                    for out in pending:
                        yield out
                elif _is_agent_message_item(item):
                    key = f"message:{item.get('id') or item.get('item_id') or 'main'}"
                    for out in append_cumulative_message(key, _extract_text(item)):
                        yield out
                continue
            if etype == 'item.updated':
                item = event.get('item') or {}
                if _is_tool_item(item):
                    for out in append_tool_input(item, _tool_input_json(item)):
                        yield out
                    for out in emit_tool_result(item, pending=True):
                        yield out
                elif _is_reasoning_item(item):
                    key = f"thinking:{item.get('id') or item.get('item_id') or 'main'}"
                    for out in append_cumulative_text(key, _extract_reasoning_text(item), item):
                        yield out
                elif _is_agent_message_item(item):
                    key = f"message:{item.get('id') or item.get('item_id') or 'main'}"
                    for out in append_cumulative_message(key, _extract_text(item)):
                        yield out
                continue
            if etype == 'item.completed':
                item = event.get('item') or {}
                if item.get('type') == 'error':
                    yield _ev({'type': 'error', 'message': item.get('message') or 'codex item error'})
                    continue
                if _is_reasoning_item(item):
                    key = f"thinking:{item.get('id') or item.get('item_id') or 'main'}"
                    for out in append_cumulative_text(key, _extract_reasoning_text(item), item):
                        yield out
                    for out in stop_block(key):
                        yield out
                    continue
                if _is_tool_item(item):
                    for out in append_tool_input(item, _tool_input_json(item)):
                        yield out
                    tool_key = f"tool:{_tool_id(item) or _tool_name(item)}:{item.get('id') or item.get('item_id') or ''}"
                    for out in stop_block(tool_key):
                        yield out
                    for out in emit_tool_result(item, pending=False):
                        yield out
                    continue
                key = f"message:{item.get('id') or item.get('item_id') or 'main'}"
                for out in append_cumulative_message(key, _extract_text(item)):
                    yield out
                continue
            if etype in {'error', 'turn.failed'}:
                error = event.get('error') or {}
                message = error.get('message') if isinstance(error, dict) else str(error)
                yield _ev({'type': 'error', 'message': event.get('message') or message or 'codex error'})

        proc.wait(timeout=10)
        if os.path.isfile(last_msg_path):
            try:
                final_text = open(last_msg_path).read().strip()
            except Exception:
                final_text = ''
            if final_text and not assistant_text.strip():
                for out in append_text(final_text):
                    yield out

        for out in close_all_blocks():
            yield out

        if proc.returncode and proc.returncode != 0 and not text_started:
            yield _ev({'type': 'error', 'message': f'codex exited {proc.returncode}'})

        ordered = [assistant_blocks[i] for i in sorted(assistant_blocks.keys())]
        if ordered:
            history.append({'role': 'assistant', 'blocks': ordered, 'ts': time.time()})
        chat_store.update_history(chat_id, history, provider_session_id=thread_id)
        yield _ev({
            'type': 'turn_done',
            'provider': 'codex',
            'provider_session_id': thread_id,
            'duration_ms': int((time.time() - t0) * 1000),
            'input_tokens': input_tokens or None,
            'output_tokens': output_tokens or None,
            'cache_read_tokens': cached_tokens or None,
        })
        yield "data: [DONE]\n\n"
    finally:
        _active_procs.pop(chat_id, None)


def _extract_text(item: dict) -> str:
    parts: list[str] = []
    if not isinstance(item, dict):
        return ''
    if isinstance(item.get('text'), str):
        parts.append(item['text'])
    output = item.get('output')
    if isinstance(output, list):
        for part in output:
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                parts.append(part['text'])
    content = item.get('content')
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                parts.append(part['text'])
    return ''.join(parts)


def _extract_reasoning_text(item: dict) -> str:
    parts: list[str] = []
    if not isinstance(item, dict):
        return ''
    if isinstance(item.get('text'), str):
        parts.append(item['text'])
    summary = item.get('summary')
    if isinstance(summary, str):
        parts.append(summary)
    elif isinstance(summary, list):
        for part in summary:
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                parts.append(part['text'])
            elif isinstance(part, str):
                parts.append(part)
    content = item.get('content')
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                parts.append(part['text'])
    return ''.join(parts)


def _tool_name(item: dict) -> str:
    if not isinstance(item, dict):
        return ''
    item_type = item.get('type')
    if item_type == 'command_execution':
        return 'Bash'
    if item_type == 'file_change':
        return 'Edit'
    if item_type == 'mcp_tool_call':
        server = item.get('server') or 'mcp'
        tool = item.get('tool') or 'tool'
        return f'{server}.{tool}'
    if item_type == 'web_search':
        return 'WebSearch'
    return (
        item.get('name')
        or item.get('tool_name')
        or item.get('function_name')
        or item.get('action')
        or ''
    )


def _tool_id(item: dict) -> str:
    if not isinstance(item, dict):
        return ''
    return (
        item.get('call_id')
        or item.get('tool_call_id')
        or item.get('id')
        or item.get('item_id')
        or ''
    )


def _tool_input_json(item: dict) -> str:
    if not isinstance(item, dict):
        return ''
    item_type = item.get('type')
    if item_type == 'command_execution':
        return json.dumps({'command': item.get('command', '')})
    if item_type == 'file_change':
        changes = item.get('changes') or []
        first_path = changes[0].get('path', '') if changes and isinstance(changes[0], dict) else ''
        return json.dumps({'file_path': first_path, 'changes': changes})
    if item_type == 'mcp_tool_call':
        return json.dumps(item.get('arguments') or {})
    if item_type == 'web_search':
        return json.dumps({'query': item.get('query', '')})
    if isinstance(item.get('arguments'), str):
        return item['arguments']
    if isinstance(item.get('input'), str):
        return item['input']
    for key in ('arguments', 'input', 'parsed_arguments'):
        value = item.get(key)
        if isinstance(value, dict):
            return json.dumps(value)
    return ''


def _is_reasoning_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    itype = str(item.get('type') or '').lower()
    return 'reason' in itype or 'summary' in item


def _is_agent_message_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return item.get('type') in {'agent_message', 'message'}


def _is_tool_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    itype = str(item.get('type') or '').lower()
    if itype in {'command_execution', 'file_change', 'mcp_tool_call', 'web_search'}:
        return True
    return bool(_tool_name(item) or _tool_input_json(item)) and (
        'call' in itype or 'tool' in itype or 'function' in itype or not itype
    )


def _tool_result_is_error(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    status = item.get('status')
    exit_code = item.get('exit_code')
    return bool(
        item.get('is_error')
        or item.get('error')
        or status == 'failed'
        or isinstance(exit_code, int) and exit_code != 0
    )


def _extract_tool_result_text(item: dict) -> str:
    if not isinstance(item, dict):
        return ''
    if item.get('type') == 'command_execution':
        return item.get('aggregated_output') or ''
    if item.get('type') == 'mcp_tool_call':
        result = item.get('result') or {}
        if isinstance(result, dict):
            return _flatten_content(result.get('content') or result.get('structured_content') or '')
        return _flatten_content(result)
    for key in ('output', 'result', 'content'):
        value = item.get(key)
        if value:
            return _flatten_content(value)
    return ''


def _tool_completion_summary(item: dict) -> str:
    item_type = item.get('type')
    if item_type == 'file_change':
        changes = item.get('changes') or []
        rendered = [
            f"{change.get('kind', 'update')} {change.get('path', '')}"
            for change in changes
            if isinstance(change, dict)
        ]
        return '\n'.join(rendered) or str(item.get('status') or 'completed')
    if item_type == 'web_search':
        return item.get('query') or 'search completed'
    if item_type == 'mcp_tool_call' and item.get('error'):
        error = item['error']
        return error.get('message', '') if isinstance(error, dict) else str(error)
    return str(item.get('status') or 'completed')


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get('text'), str):
                    parts.append(c['text'])
                else:
                    parts.append(json.dumps(c)[:400])
            else:
                parts.append(str(c))
        return '\n'.join(parts)
    if isinstance(content, dict):
        if isinstance(content.get('text'), str):
            return content['text']
        return json.dumps(content)
    return str(content)


def _ev(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"
