"""Stream Claude Code completions via subprocess as SSE.

Lifted from devhub/routes/slider.py:_stream_claude. Single-purpose: feed a system
prompt + user message to `claude -p` and yield text deltas.
"""
import glob
import json
import os
import subprocess
import threading
import time

from .config import get_setting

CLAUDE_BIN = os.environ.get('CLAUDE_BIN') or (
    sorted(glob.glob('/opt/claude/versions/*'), key=os.path.getmtime)[-1]
    if glob.glob('/opt/claude/versions/*') else 'claude'
)

_active_procs: dict[str, subprocess.Popen] = {}
_active_procs_lock = threading.Lock()


def cancel(job_id: str) -> bool:
    with _active_procs_lock:
        proc = _active_procs.get(job_id)
    if not proc:
        return False
    try:
        proc.kill()
        return True
    except Exception:
        return False


def stream(job_id: str, system_prompt: str, user_msg: str, model: str = 'sonnet'):
    """Yield SSE-formatted strings for a single Claude completion."""
    t_start = time.time()
    cmd = [
        CLAUDE_BIN, '-p',
        '--output-format', 'stream-json',
        '--verbose',
        '--system-prompt', system_prompt,
        '--model', model,
    ]
    env = os.environ.copy()
    env['HOME'] = env.get('HOME', '/root')
    token = get_setting('global', 'claude_code_oauth_token')
    if token:
        env['CLAUDE_CODE_OAUTH_TOKEN'] = token

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
    except FileNotFoundError as e:
        yield f"data: {json.dumps({'error': f'claude binary not found: {e}'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    with _active_procs_lock:
        _active_procs[job_id] = proc

    try:
        proc.stdin.write(user_msg)
        proc.stdin.close()

        result_text = ''
        got_output = False

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get('type', '')
            if etype == 'stream_event':
                inner = event.get('event', {})
                if inner.get('type') == 'content_block_delta':
                    delta = inner.get('delta', {})
                    if delta.get('type') == 'text_delta':
                        got_output = True
                        txt = delta['text']
                        result_text += txt
                        yield f"data: {json.dumps({'text': txt})}\n\n"
            elif etype == 'result':
                if not got_output:
                    text = event.get('result', '')
                    if text:
                        result_text = text
                        yield f"data: {json.dumps({'text': text})}\n\n"

        proc.wait(timeout=5)
        if proc.returncode and proc.returncode != 0:
            err = proc.stderr.read()[:400] if proc.stderr else ''
            yield f"data: {json.dumps({'error': err or f'claude exited {proc.returncode}'})}\n\n"
        duration = int((time.time() - t_start) * 1000)
        yield f"data: {json.dumps({'done': True, 'duration_ms': duration, 'full': result_text})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        with _active_procs_lock:
            _active_procs.pop(job_id, None)
