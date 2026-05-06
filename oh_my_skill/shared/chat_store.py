"""Persistent chat session store.

Replaces the in-memory `_sessions` dict in claude_chat.py with a SQLite
table so chats survive container restarts. One row per chat session.
Per-card resume: the latest non-closed chat for a card_id is reused
when the user clicks 💬 on the same card again.

Retention: a sweeper deletes chats older than `skill.chat_retention_days`
(default 7). Runs at boot + every hour.
"""
import json
import os
import sqlite3
import threading
import time

from .config import get_setting

DATA_DIR = os.environ.get('OMI_DATA_DIR', '/data')
CHATS_DB = os.environ.get('OMI_CHATS_DB') or os.path.join(DATA_DIR, 'chats.db')

_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(CHATS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    os.makedirs(os.path.dirname(CHATS_DB) or '.', exist_ok=True)
    conn = _conn()
    conn.execute('''CREATE TABLE IF NOT EXISTS chats (
        id            TEXT PRIMARY KEY,
        cwd           TEXT NOT NULL,
        card_id       TEXT DEFAULT '',
        claude_session_id TEXT DEFAULT '',
        history_json  TEXT DEFAULT '[]',
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL,
        closed        INTEGER DEFAULT 0
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS chats_card_id ON chats(card_id, closed, updated_at DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS chats_updated ON chats(updated_at DESC)')
    conn.commit()
    conn.close()


init()


def get_retention_days() -> int:
    val = get_setting('skill', 'chat_retention_days', '')
    try:
        n = int(val)
        return max(0, n)
    except (TypeError, ValueError):
        return 7  # default


def create(chat_id: str, cwd: str, card_id: str = '') -> dict:
    now = time.time()
    row = {
        'id': chat_id, 'cwd': cwd, 'card_id': card_id or '',
        'claude_session_id': '', 'history_json': '[]',
        'created_at': now, 'updated_at': now, 'closed': 0,
    }
    with _lock:
        conn = _conn()
        conn.execute(
            '''INSERT INTO chats (id, cwd, card_id, claude_session_id,
               history_json, created_at, updated_at, closed)
               VALUES (?,?,?,?,?,?,?,?)''',
            tuple(row.values()),
        )
        conn.commit()
        conn.close()
    return _hydrate(row)


def get(chat_id: str) -> dict | None:
    conn = _conn()
    r = conn.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone()
    conn.close()
    return _hydrate(dict(r)) if r else None


def find_active_for_card(card_id: str) -> dict | None:
    """Latest non-closed chat for this card (for resume-on-click)."""
    if not card_id:
        return None
    conn = _conn()
    r = conn.execute(
        'SELECT * FROM chats WHERE card_id=? AND closed=0 '
        'ORDER BY updated_at DESC LIMIT 1', (card_id,)
    ).fetchone()
    conn.close()
    return _hydrate(dict(r)) if r else None


def list_recent(limit: int = 30, card_id: str | None = None) -> list[dict]:
    sql = 'SELECT * FROM chats WHERE 1=1'
    params: list = []
    if card_id is not None:
        sql += ' AND card_id=?'
        params.append(card_id)
    sql += ' ORDER BY updated_at DESC LIMIT ?'
    params.append(limit)
    conn = _conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_hydrate(dict(r)) for r in rows]


def update_history(chat_id: str, history: list, claude_session_id: str | None = None):
    with _lock:
        conn = _conn()
        if claude_session_id:
            conn.execute(
                'UPDATE chats SET history_json=?, claude_session_id=?, updated_at=? WHERE id=?',
                (json.dumps(history, default=str), claude_session_id, time.time(), chat_id),
            )
        else:
            conn.execute(
                'UPDATE chats SET history_json=?, updated_at=? WHERE id=?',
                (json.dumps(history, default=str), time.time(), chat_id),
            )
        conn.commit()
        conn.close()


def close(chat_id: str):
    with _lock:
        conn = _conn()
        conn.execute('UPDATE chats SET closed=1, updated_at=? WHERE id=?',
                     (time.time(), chat_id))
        conn.commit()
        conn.close()


def delete(chat_id: str):
    with _lock:
        conn = _conn()
        conn.execute('DELETE FROM chats WHERE id=?', (chat_id,))
        conn.commit()
        conn.close()


def sweep() -> int:
    """Delete chats whose updated_at is older than retention. Returns count."""
    days = get_retention_days()
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    with _lock:
        conn = _conn()
        cur = conn.execute('DELETE FROM chats WHERE updated_at < ?', (cutoff,))
        conn.commit()
        n = cur.rowcount
        conn.close()
    return n


def _hydrate(row: dict) -> dict:
    try:
        row['history'] = json.loads(row.get('history_json') or '[]')
    except json.JSONDecodeError:
        row['history'] = []
    return row


# ── Background sweeper ─────────────────────────────────────────────
_sweeper_thread = None


def start_sweeper(interval_sec: int = 3600):
    """One sweep at boot, then periodic. Idempotent."""
    global _sweeper_thread
    if _sweeper_thread and _sweeper_thread.is_alive():
        return
    def _loop():
        while True:
            try:
                sweep()
            except Exception:
                pass
            time.sleep(interval_sec)
    _sweeper_thread = threading.Thread(target=_loop, daemon=True)
    _sweeper_thread.start()
