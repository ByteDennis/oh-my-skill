"""Settings DB + global key resolution.

Lifted from devhub/shared/config.py with one addition: a per-user 'image' namespace.
Resolution order for API keys: per-user (image.X) > global (global.X) > env > ''.
"""
import os
import sqlite3

SETTINGS_DB = os.environ.get('SETTINGS_DB', '/data/oh-my-image.db')

ENV_MAP = {
    'claude_code_oauth_token': 'CLAUDE_CODE_OAUTH_TOKEN',
    'openai_api_key': 'OPENAI_API_KEY',
    'gemini_api_key': 'GEMINI_API_KEY',
}


def _conn():
    conn = sqlite3.connect(SETTINGS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_settings_db():
    os.makedirs(os.path.dirname(SETTINGS_DB), exist_ok=True)
    conn = sqlite3.connect(SETTINGS_DB)
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()


def seed_global_settings_from_env():
    init_settings_db()
    conn = _conn()
    for key, env_key in ENV_MAP.items():
        val = os.environ.get(env_key, '')
        if not val:
            continue
        existing = conn.execute(
            'SELECT value FROM settings WHERE key = ?', (f'global.{key}',)
        ).fetchone()
        if not existing or not existing['value']:
            conn.execute(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                (f'global.{key}', val),
            )
    conn.commit()
    conn.close()


def get_setting(namespace: str, key: str, default: str = '') -> str:
    try:
        conn = _conn()
        row = conn.execute(
            'SELECT value FROM settings WHERE key = ?', (f'{namespace}.{key}',)
        ).fetchone()
        conn.close()
        if row and row['value']:
            return row['value']
    except Exception:
        pass
    return default


def put_setting(namespace: str, key: str, value: str):
    conn = _conn()
    conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (f'{namespace}.{key}', value or ''),
    )
    conn.commit()
    conn.close()


def get_all(namespace: str) -> dict:
    prefix = namespace + '.'
    try:
        conn = _conn()
        rows = conn.execute(
            'SELECT key, value FROM settings WHERE key LIKE ?', (prefix + '%',)
        ).fetchall()
        conn.close()
        return {r['key'][len(prefix):]: r['value'] for r in rows}
    except Exception:
        return {}


def resolve_api_key(provider: str) -> tuple[str, str]:
    """Return (key, source) where source ∈ {'user', 'global', 'env', ''}.

    provider: 'openai' or 'gemini'.
    """
    settings_key = f'{provider}_api_key'
    env_key = ENV_MAP.get(settings_key, '')

    val = get_setting('image', settings_key)
    if val:
        return val, 'user'

    val = get_setting('global', settings_key)
    if val:
        return val, 'global'

    if env_key:
        val = os.environ.get(env_key, '')
        if val:
            return val, 'env'

    return '', ''
