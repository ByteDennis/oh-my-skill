"""Sync Center — diff-based local↔git sync, per-card direction control.

Replaces the bulk push/pull buttons with a two-step flow:
  1. POST /skill-cards/api/sync-diff   → returns rows {id, title, status, suggested, ...}
  2. POST /skill-cards/api/sync-apply  → executes per-row directions, single commit per repo

Routing rule (matches devhub): cards with the `private` tag go to the private
repo; everything else goes to the public repo. Conflict policy: local vs remote
`updated_at` decides the *suggested* direction; the UI lets the user override.
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sqlite3
from datetime import datetime

from flask import Blueprint, jsonify, request

# Reuse helpers from skillcards.py
from oh_my_skill.routes.skillcards import (
    _db, _get_app_setting,
    _git_env, _git_clone_or_pull,
    SKILLS_REMOTE_DIR, SKILLS_REMOTE_PRIVATE_DIR,
)

sync_bp = Blueprint('sync', __name__)


# ── DB migration: add last_synced_hash + remote_updated_at columns ──
def _migrate():
    try:
        conn = _db()
        try:
            conn.execute('ALTER TABLE cards ADD COLUMN last_synced_hash TEXT DEFAULT \'\'')
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
    except Exception:
        pass


_migrate()


# ── Card hashing ──────────────────────────────────────────────────
def _card_hash(title: str, content: str, tags: list) -> str:
    """Stable hash over the synced fields. Order tags so reordering doesn't
    look like a change."""
    h = hashlib.sha256()
    h.update((title or '').encode('utf-8'))
    h.update(b'\x1f')
    h.update((content or '').encode('utf-8'))
    h.update(b'\x1f')
    for t in sorted(tags or []):
        h.update(t.encode('utf-8'))
        h.update(b',')
    return h.hexdigest()[:16]


def _has_private(tags) -> bool:
    return 'private' in (tags or [])


def _parse_ignore_patterns(raw: str) -> list[str]:
    """Split on newlines or commas. Strip blanks."""
    if not raw:
        return []
    parts = re.split(r'[,\n]', raw)
    return [p.strip() for p in parts if p.strip()]


def _is_ignored(card_id: str, patterns: list[str]) -> bool:
    """Match patterns against both the full id and the id without the
    devhub `skill-` prefix, so a user writing `sibyl-*` matches both
    `sibyl-foo` and `skill-sibyl-foo`."""
    if not patterns:
        return False
    stripped = card_id[6:] if card_id.startswith('skill-') else card_id
    candidates = (card_id, stripped)
    return any(fnmatch.fnmatch(c, p) for c in candidates for p in patterns)


# ── Resolve config ────────────────────────────────────────────────
def _config(data: dict) -> dict:
    g = lambda k, default='': (data.get(k) or '').strip() or _get_app_setting(k, default)
    return {
        'public_repo':  g('remoteRepo', os.environ.get('SKILL_GITHUB_REPO', '')),
        'private_repo': g('privateRepo', os.environ.get('SKILL_PRIVATE_GITHUB_REPO', '')),
        'branch':       g('remoteBranch', 'main'),
        'subdir':       g('remoteSubdir', ''),
        'ssh_key':      g('remoteSshKey', os.environ.get('SKILL_SSH_KEY', '')),
    }


# ── SKILL.md serializer (round-trippable: includes id + updated_at) ──
def _entry_dir_for_card(card: dict) -> str:
    """Directory name for a card under the repo. Matches devhub's convention:
    strip leading 'skill-' so a card with id 'skill-abc' lives under 'abc/'."""
    cid = card['id']
    return cid[6:] if cid.startswith('skill-') else cid


def _write_skill_md(card: dict, target_dir: str):
    """Write one card to <target_dir>/<entry>/SKILL.md with frontmatter
    that's enough to round-trip (id, name, tags, updated_at + any pre-existing
    metadata)."""
    entry = _entry_dir_for_card(card)
    skill_dir = os.path.join(target_dir, entry)
    os.makedirs(skill_dir, exist_ok=True)
    try:
        meta = json.loads(card.get('metadata') or '{}')
    except (json.JSONDecodeError, TypeError):
        meta = {}
    tags = card.get('tags') or []
    fm = {
        'id': card['id'],
        'name': card['title'],
        **{k: v for k, v in meta.items() if k not in ('id', 'name', 'tags', 'updated_at')},
        'tags': '[' + ', '.join(tags) + ']',
        'updated_at': card.get('updated_at') or '',
    }
    fm_lines = ['---'] + [f'{k}: {v}' for k, v in fm.items()] + ['---']
    body = (card.get('content') or '').rstrip() + '\n'
    with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as f:
        f.write('\n'.join(fm_lines) + '\n\n' + body)


def _parse_skill_md(skill_path: str, entry: str) -> dict | None:
    """Parse a SKILL.md back into a card dict. Returns None on failure."""
    try:
        with open(skill_path, 'r') as f:
            raw = f.read()
    except Exception:
        return None
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', raw, re.DOTALL)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2).lstrip('\n').rstrip() + '\n'
    meta = {}
    for line in fm_text.split('\n'):
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    cid = meta.get('id') or f'skill-{entry}'
    title = meta.get('name') or entry
    tag_str = (meta.get('tags') or '').strip('[]')
    tags = [t.strip().strip('"').strip("'") for t in tag_str.split(',') if t.strip()]
    updated_at = meta.get('updated_at') or ''
    # Strip the synthetic frontmatter keys; everything else stays in metadata
    meta_extra = {k: v for k, v in meta.items()
                  if k not in ('id', 'name', 'tags', 'updated_at')}
    return {
        'id': cid, 'title': title, 'content': body, 'tags': tags,
        'metadata': meta_extra, 'updated_at': updated_at,
    }


def _scan_repo(target_dir: str) -> dict[str, dict]:
    """Returns {card_id: parsed_card_dict}. Skips entries without SKILL.md."""
    out = {}
    if not os.path.isdir(target_dir):
        return out
    for entry in sorted(os.listdir(target_dir)):
        if entry == '.git':
            continue
        skill_path = os.path.join(target_dir, entry, 'SKILL.md')
        if not os.path.isfile(skill_path):
            continue
        c = _parse_skill_md(skill_path, entry)
        if c:
            out[c['id']] = c
    return out


def _load_local_cards() -> list[dict]:
    conn = _db()
    rows = [dict(r) for r in conn.execute('SELECT * FROM cards').fetchall()]
    conn.close()
    out = []
    for r in rows:
        try:
            r['tags'] = json.loads(r.get('tags') or '[]')
        except (json.JSONDecodeError, TypeError):
            r['tags'] = []
        try:
            r['metadata'] = json.loads(r.get('metadata') or '{}')
        except (json.JSONDecodeError, TypeError):
            r['metadata'] = {}
        out.append(r)
    return out


# ── /sync-diff ────────────────────────────────────────────────────
@sync_bp.route('/skill-cards/api/sync-diff', methods=['POST'])
def sync_diff():
    """Clone/fetch both repos and compute per-card diff rows."""
    data = request.get_json(silent=True) or {}
    cfg = _config(data)
    if not cfg['public_repo'] and not cfg['private_repo']:
        return jsonify({'error': 'No remote repo configured. Set Public and/or Private repo in Settings.'}), 400

    locals_ = _load_local_cards()
    locals_by_id = {c['id']: c for c in locals_}
    ignore_patterns = _parse_ignore_patterns(_get_app_setting('ignorePatterns', ''))

    rows = []  # diff rows
    ignored_count = 0
    repo_status = {}  # repo -> {ok, error}

    for repo_kind, repo_url, target_dir, want_private in (
        ('public', cfg['public_repo'], SKILLS_REMOTE_DIR, False),
        ('private', cfg['private_repo'], SKILLS_REMOTE_PRIVATE_DIR, True),
    ):
        if not repo_url:
            repo_status[repo_kind] = {'ok': False, 'error': 'not configured'}
            continue
        try:
            _git_clone_or_pull(repo_url, cfg['branch'], cfg['ssh_key'], target_dir)
        except subprocess.CalledProcessError as e:
            repo_status[repo_kind] = {'ok': False, 'error': (e.stderr or b'').decode()[:200] or str(e)}
            continue
        except subprocess.TimeoutExpired:
            repo_status[repo_kind] = {'ok': False, 'error': 'git timed out'}
            continue
        repo_status[repo_kind] = {'ok': True, 'repo': repo_url}

        scan_dir = os.path.join(target_dir, cfg['subdir']) if cfg['subdir'] else target_dir
        remotes_by_id = _scan_repo(scan_dir)

        # Cards from local that belong to this repo
        local_for_repo = {
            cid: c for cid, c in locals_by_id.items()
            if _has_private(c['tags']) == want_private
        }

        # Build the set of all ids relevant to this repo
        all_ids = set(local_for_repo) | set(remotes_by_id)
        for cid in sorted(all_ids):
            if _is_ignored(cid, ignore_patterns):
                ignored_count += 1
                continue
            lc = local_for_repo.get(cid)
            rc = remotes_by_id.get(cid)
            row = {
                'id': cid, 'repo': repo_kind,
                'title': (lc or rc)['title'],
                'tags': (lc or rc).get('tags') or [],
                'local_updated_at': (lc or {}).get('updated_at') or '',
                'remote_updated_at': (rc or {}).get('updated_at') or '',
            }
            if lc and not rc:
                row['status'] = 'local-only'
                row['suggested'] = 'push'
            elif rc and not lc:
                row['status'] = 'remote-only'
                row['suggested'] = 'pull'
            else:
                lh = _card_hash(lc['title'], lc['content'], lc['tags'])
                rh = _card_hash(rc['title'], rc['content'], rc['tags'])
                if lh == rh:
                    row['status'] = 'synced'
                    row['suggested'] = 'skip'
                else:
                    row['status'] = 'modified'
                    # Conflict resolution by timestamp: newer wins
                    lu = lc.get('updated_at') or ''
                    ru = rc.get('updated_at') or ''
                    row['suggested'] = 'push' if lu >= ru else 'pull'
                    row['conflict'] = bool(lu and ru and lu != ru)
            rows.append(row)

    return jsonify({
        'config': {k: v for k, v in cfg.items() if k != 'ssh_key'},
        'repos': repo_status,
        'rows': rows,
        'ignored_count': ignored_count,
        'ignore_patterns': ignore_patterns,
    })


# ── /sync-apply ───────────────────────────────────────────────────
@sync_bp.route('/skill-cards/api/sync-apply', methods=['POST'])
def sync_apply():
    """Execute per-card directions. Body: {actions: [{id, repo, direction}]}.

    direction is one of: 'push', 'pull', 'skip'. Single git commit per repo.
    Returns per-row results + final commit/push status.
    """
    data = request.get_json(silent=True) or {}
    cfg = _config(data)
    actions = data.get('actions') or []
    if not actions:
        return jsonify({'error': 'no actions'}), 400

    locals_ = _load_local_cards()
    locals_by_id = {c['id']: c for c in locals_}

    # Group by repo for batched commit
    VALID_DIRS = ('push', 'pull', 'delete-remote', 'delete-local')
    by_repo = {'public': [], 'private': []}
    for a in actions:
        if a.get('repo') in by_repo and a.get('direction') in VALID_DIRS:
            by_repo[a['repo']].append(a)

    results = {'rows': [], 'commits': {}}
    conn = _db()
    now_ts = datetime.utcnow().isoformat() + 'Z'

    for repo_kind, items in by_repo.items():
        if not items:
            continue
        repo_url = cfg['public_repo'] if repo_kind == 'public' else cfg['private_repo']
        target_dir = SKILLS_REMOTE_DIR if repo_kind == 'public' else SKILLS_REMOTE_PRIVATE_DIR
        if not repo_url:
            for a in items:
                results['rows'].append({**a, 'ok': False, 'error': f'{repo_kind} repo not configured'})
            continue

        # Clone/fetch fresh so we have an up-to-date base
        try:
            env = _git_clone_or_pull(repo_url, cfg['branch'], cfg['ssh_key'], target_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            err = getattr(e, 'stderr', None)
            err = (err or b'').decode()[:200] if isinstance(err, (bytes, bytearray)) else str(e)
            results['commits'][repo_kind] = {'ok': False, 'error': err}
            for a in items:
                results['rows'].append({**a, 'ok': False, 'error': 'fetch failed'})
            continue
        scan_dir = os.path.join(target_dir, cfg['subdir']) if cfg['subdir'] else target_dir
        os.makedirs(scan_dir, exist_ok=True)
        remotes = _scan_repo(scan_dir)

        # Apply each action
        pushed_count = 0
        pulled_count = 0
        deleted_remote_count = 0
        deleted_local_count = 0
        for a in items:
            cid = a['id']
            try:
                if a['direction'] == 'push':
                    lc = locals_by_id.get(cid)
                    if not lc:
                        results['rows'].append({**a, 'ok': False, 'error': 'local card not found'})
                        continue
                    _write_skill_md(lc, scan_dir)
                    new_hash = _card_hash(lc['title'], lc['content'], lc['tags'])
                    conn.execute('UPDATE cards SET last_synced_hash=? WHERE id=?', (new_hash, cid))
                    pushed_count += 1
                    results['rows'].append({**a, 'ok': True})

                elif a['direction'] == 'pull':
                    rc = remotes.get(cid)
                    if not rc:
                        results['rows'].append({**a, 'ok': False, 'error': 'remote card not found'})
                        continue
                    tags_json = json.dumps(rc['tags'])
                    meta_json = json.dumps(rc['metadata'])
                    upd = rc.get('updated_at') or now_ts
                    new_hash = _card_hash(rc['title'], rc['content'], rc['tags'])
                    if cid in locals_by_id:
                        conn.execute(
                            '''UPDATE cards SET title=?, content=?, tags=?, metadata=?,
                               updated_at=?, last_synced_hash=? WHERE id=?''',
                            (rc['title'], rc['content'], tags_json, meta_json, upd, new_hash, cid),
                        )
                    else:
                        conn.execute(
                            '''INSERT INTO cards
                               (id, title, content, tags, metadata, created_at, updated_at, last_synced_hash)
                               VALUES (?,?,?,?,?,?,?,?)''',
                            (cid, rc['title'], rc['content'], tags_json, meta_json,
                             upd, upd, new_hash),
                        )
                    pulled_count += 1
                    results['rows'].append({**a, 'ok': True})

                elif a['direction'] == 'delete-remote':
                    # Reconstruct the entry dir from id (matches _entry_dir_for_card)
                    entry = cid[6:] if cid.startswith('skill-') else cid
                    skill_dir = os.path.join(scan_dir, entry)
                    if os.path.isdir(skill_dir):
                        shutil.rmtree(skill_dir)
                        deleted_remote_count += 1
                        results['rows'].append({**a, 'ok': True})
                    else:
                        results['rows'].append({**a, 'ok': False, 'error': 'remote card not found'})

                elif a['direction'] == 'delete-local':
                    if cid in locals_by_id:
                        conn.execute('DELETE FROM cards WHERE id=?', (cid,))
                        deleted_local_count += 1
                        results['rows'].append({**a, 'ok': True})
                    else:
                        results['rows'].append({**a, 'ok': False, 'error': 'local card not found'})

            except Exception as e:
                results['rows'].append({**a, 'ok': False, 'error': str(e)[:200]})

        # Commit + push if anything mutated the working tree
        mod_count = pushed_count + deleted_remote_count
        if mod_count:
            try:
                # -A captures both additions/modifications AND deletions
                subprocess.run(['git', '-C', target_dir, 'add', '-A'],
                               check=True, capture_output=True)
                status = subprocess.run(['git', '-C', target_dir, 'status', '--porcelain'],
                                        capture_output=True, text=True, check=True).stdout.strip()
                if status:
                    subprocess.run(['git', '-C', target_dir, 'config', 'user.name', 'Skill Cards'],
                                   check=True, capture_output=True)
                    subprocess.run(['git', '-C', target_dir, 'config', 'user.email', 'skills@local'],
                                   check=True, capture_output=True)
                    parts = []
                    if pushed_count: parts.append(f'{pushed_count} push')
                    if deleted_remote_count: parts.append(f'{deleted_remote_count} delete')
                    msg = f"Sync · {', '.join(parts)} · {now_ts}"
                    subprocess.run(['git', '-C', target_dir, 'commit', '-m', msg],
                                   check=True, capture_output=True)
                    branch = cfg['branch'] or 'main'
                    subprocess.run(['git', '-C', target_dir, 'push', '-u', 'origin', branch],
                                   check=True, capture_output=True, env=env)
                    results['commits'][repo_kind] = {
                        'ok': True, 'pushed': pushed_count, 'pulled': pulled_count,
                        'deleted_remote': deleted_remote_count, 'deleted_local': deleted_local_count,
                        'commit_msg': msg,
                    }
                else:
                    results['commits'][repo_kind] = {
                        'ok': True, 'pushed': 0, 'pulled': pulled_count,
                        'deleted_remote': 0, 'deleted_local': deleted_local_count,
                        'note': 'nothing to commit',
                    }
            except subprocess.CalledProcessError as e:
                results['commits'][repo_kind] = {
                    'ok': False, 'error': (e.stderr or b'').decode()[:300] or str(e),
                }
        else:
            results['commits'][repo_kind] = {
                'ok': True, 'pushed': 0, 'pulled': pulled_count,
                'deleted_remote': 0, 'deleted_local': deleted_local_count,
            }

    conn.commit()
    conn.close()
    return jsonify(results)


# ── Settings GET/PUT for the sync repos ───────────────────────────
@sync_bp.route('/skill-cards/api/sync-settings', methods=['GET'])
def sync_settings_get():
    return jsonify({
        'remoteRepo':     _get_app_setting('remoteRepo', ''),
        'privateRepo':    _get_app_setting('privateRepo', ''),
        'remoteBranch':   _get_app_setting('remoteBranch', 'main'),
        'remoteSubdir':   _get_app_setting('remoteSubdir', ''),
        'remoteSshKey':   _get_app_setting('remoteSshKey', ''),
        'ignorePatterns': _get_app_setting('ignorePatterns', ''),
    })


@sync_bp.route('/skill-cards/api/sync-settings', methods=['PUT'])
def sync_settings_put():
    data = request.get_json(silent=True) or {}
    from oh_my_skill.shared.config import put_setting
    for k in ('remoteRepo', 'privateRepo', 'remoteBranch', 'remoteSubdir', 'remoteSshKey',
              'ignorePatterns'):
        if k in data and isinstance(data[k], str):
            # devhub stores under 'skillcards.*'; reuse that prefix so the
            # vendored devhub endpoints (sync-skills, etc.) see the same config
            put_setting('skillcards', k, data[k].strip())
    return jsonify({'ok': True})
