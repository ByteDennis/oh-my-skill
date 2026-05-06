"""API call logging — DB + JSONL file.

Mirrors the _log_api / _log_api_update pattern from devhub/routes/slider.py
but writes to its own table so it doesn't pollute slider's log.

Each call produces two rows: one at start (status='running') and one update
at end (status='done' or 'error'). The same log_id is reused.

Errors include the full Python traceback so the UI panel can show why
something failed without needing access to the container's stderr.
"""
import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime

from .config import SETTINGS_DB

DATA_DIR = os.environ.get('OMI_DATA_DIR', '/data')
LOG_DB = os.environ.get('OMI_LOG_DB') or os.path.join(DATA_DIR, 'api_logs.db')
LOG_FILE = os.environ.get('OMI_LOG_FILE') or os.path.join(DATA_DIR, 'api.log')

_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(LOG_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    os.makedirs(os.path.dirname(LOG_DB), exist_ok=True)
    conn = _conn()
    conn.execute('''CREATE TABLE IF NOT EXISTS api_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        action TEXT NOT NULL,
        model TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        detail TEXT DEFAULT '',
        error TEXT DEFAULT '',
        traceback TEXT DEFAULT '',
        duration_ms INTEGER DEFAULT 0,
        meta_json TEXT DEFAULT '{}'
    )''')
    conn.commit()
    conn.close()


init()


def _file_append(row: dict):
    try:
        with _lock, open(LOG_FILE, 'a') as f:
            f.write(json.dumps(row, default=str) + '\n')
    except Exception:
        pass


def start(action: str, model: str = '', detail: str = '', **meta) -> int:
    """Insert a 'running' log row, return its id."""
    ts = datetime.utcnow().isoformat()
    conn = _conn()
    cur = conn.execute(
        'INSERT INTO api_logs (ts, action, model, detail, status, meta_json) '
        'VALUES (?,?,?,?,?,?)',
        (ts, action, model, detail, 'running', json.dumps(meta) if meta else '{}'),
    )
    log_id = cur.lastrowid
    conn.commit()
    conn.close()
    _file_append({'event': 'start', 'id': log_id, 'ts': ts, 'action': action,
                  'model': model, 'detail': detail, **meta})
    return log_id


def finish(log_id: int, status: str = 'done', error: str = '',
           tb: str = '', duration_ms: int = 0, **meta):
    ts = datetime.utcnow().isoformat()
    conn = _conn()
    # Merge into existing meta_json
    row = conn.execute('SELECT meta_json FROM api_logs WHERE id=?', (log_id,)).fetchone()
    base = json.loads(row['meta_json']) if row and row['meta_json'] else {}
    base.update(meta)
    conn.execute(
        'UPDATE api_logs SET status=?, error=?, traceback=?, duration_ms=?, meta_json=? '
        'WHERE id=?',
        (status, error[:2000], tb[:8000], duration_ms, json.dumps(base, default=str), log_id),
    )
    conn.commit()
    conn.close()
    _file_append({'event': 'end', 'id': log_id, 'ts': ts, 'status': status,
                  'error': error[:500], 'duration_ms': duration_ms, **meta})


class Span:
    """Context manager that logs start + auto-logs end with status/error."""

    def __init__(self, action: str, model: str = '', detail: str = '', **meta):
        self.action = action
        self.model = model
        self.detail = detail
        self.meta = meta
        self.log_id = None
        self.t0 = 0.0

    def __enter__(self):
        self.log_id = start(self.action, self.model, self.detail, **self.meta)
        self.t0 = time.time()
        return self

    def update(self, **meta):
        self.meta.update(meta)

    def finish_ok(self, **meta):
        finish(self.log_id, status='done',
               duration_ms=int((time.time() - self.t0) * 1000),
               **{**self.meta, **meta})

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            # Caller is expected to call finish_ok explicitly so they can
            # attach response_summary etc. If they didn't, we still close it.
            conn = _conn()
            row = conn.execute(
                'SELECT status FROM api_logs WHERE id=?', (self.log_id,)
            ).fetchone()
            conn.close()
            if row and row['status'] == 'running':
                self.finish_ok()
            return False
        # Exception path
        finish(self.log_id, status='error',
               error=f'{exc_type.__name__}: {exc}',
               tb=''.join(traceback.format_exception(exc_type, exc, tb)),
               duration_ms=int((time.time() - self.t0) * 1000),
               **self.meta)
        return False  # re-raise


def list_logs(limit: int = 100, offset: int = 0,
              status: str | None = None, action: str | None = None) -> list[dict]:
    sql = 'SELECT * FROM api_logs WHERE 1=1'
    params: list = []
    if status:
        sql += ' AND status=?'
        params.append(status)
    if action:
        sql += ' AND action=?'
        params.append(action)
    sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])
    conn = _conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['meta'] = json.loads(d.pop('meta_json', '{}') or '{}')
        except Exception:
            d['meta'] = {}
        out.append(d)
    return out


def clear(older_than_days: int = 0):
    conn = _conn()
    if older_than_days > 0:
        cutoff = datetime.utcnow().isoformat()
        # naive date compare on iso string works for our uses
        conn.execute("DELETE FROM api_logs WHERE substr(ts,1,10) < date(?, ?)",
                     (cutoff, f'-{older_than_days} days'))
    else:
        conn.execute('DELETE FROM api_logs')
    conn.commit()
    conn.close()
