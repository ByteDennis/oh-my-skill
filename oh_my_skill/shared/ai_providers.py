"""Provider selection + lightweight availability checks."""
import glob
import os
import shutil
import subprocess

from .config import get_setting

CHAT_MODELS = {
    'claude': [
        {'id': 'haiku', 'label': 'Haiku'},
        {'id': 'sonnet', 'label': 'Sonnet'},
        {'id': 'opus', 'label': 'Opus'},
    ],
    'codex': [
        {'id': 'gpt-5.4-mini', 'label': 'GPT-5.4 Mini'},
        {'id': 'gpt-5.4', 'label': 'GPT-5.4'},
        {'id': 'gpt-5.5', 'label': 'GPT-5.5'},
    ],
}

DEFAULT_CHAT_MODELS = {
    'claude': 'sonnet',
    'codex': 'gpt-5.4',
}


def get_chat_provider() -> str:
    provider = (get_setting('skill', 'chat_provider', 'claude') or 'claude').strip().lower()
    return provider if provider in {'claude', 'codex'} else 'claude'


def get_model_options(provider: str) -> list[dict]:
    provider = provider if provider in CHAT_MODELS else 'claude'
    return CHAT_MODELS[provider]


def get_chat_model(provider: str | None = None) -> str:
    provider = provider or get_chat_provider()
    provider = provider if provider in CHAT_MODELS else 'claude'
    key = f'{provider}_chat_model'
    model = (get_setting('skill', key, DEFAULT_CHAT_MODELS[provider]) or '').strip()
    valid = {m['id'] for m in CHAT_MODELS[provider]}
    return model if model in valid else DEFAULT_CHAT_MODELS[provider]


def _find_claude_bin() -> str:
    bin_path = os.environ.get('CLAUDE_BIN') or shutil.which('claude')
    if bin_path:
        return bin_path
    cands = sorted(glob.glob('/opt/claude/versions/*'), key=os.path.getmtime)
    return cands[-1] if cands else ''


def _find_codex_bin() -> str:
    return os.environ.get('CODEX_BIN') or shutil.which('codex') or ''


def claude_status() -> dict:
    has_token = bool(get_setting('global', 'claude_code_oauth_token'))
    bin_path = _find_claude_bin()
    configured = bool(bin_path and has_token)
    reason = ''
    if not bin_path:
        reason = 'claude binary not found'
    elif not has_token:
        reason = 'no Claude OAuth token (Settings → Claude OAuth token)'
    return {
        'provider': 'claude',
        'configured': configured,
        'has_binary': bool(bin_path),
        'has_token': has_token,
        'binary_path': bin_path,
        'reason': reason,
    }


def codex_status() -> dict:
    bin_path = _find_codex_bin()
    login_ok = False
    login_detail = ''
    if bin_path:
        try:
            proc = subprocess.run(
                [bin_path, 'login', 'status'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=3,
                env=os.environ.copy(),
            )
            login_detail = (proc.stdout or '').strip()
            login_ok = proc.returncode == 0 and ('Logged in' in login_detail or 'API key' in login_detail)
        except Exception as e:
            login_detail = str(e)
    configured = bool(bin_path and login_ok)
    reason = ''
    if not bin_path:
        reason = 'codex binary not found'
    elif not login_ok:
        reason = 'Codex is not logged in'
    return {
        'provider': 'codex',
        'configured': configured,
        'has_binary': bool(bin_path),
        'logged_in': login_ok,
        'binary_path': bin_path,
        'login_detail': login_detail,
        'reason': reason,
    }


def active_chat_status() -> dict:
    provider = get_chat_provider()
    status = codex_status() if provider == 'codex' else claude_status()
    status['selected'] = True
    return status
