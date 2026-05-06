from flask import Blueprint, request, render_template, jsonify
import os
import json
import re
import sqlite3
import subprocess
import threading
from datetime import datetime

from oh_my_skill.shared.config import SETTINGS_DB

skillcards_bp = Blueprint('skillcards', __name__)

SKILLCARDS_DB = os.environ.get('SKILLCARDS_DB', '/data/skillcards.db')
SKILLS_DIR = os.environ.get('SKILLS_DIR', '/skills')
SKILLS_REMOTE_DIR = '/data/skills_remote'
SKILLS_REMOTE_PRIVATE_DIR = '/data/skills_private'

def _get_app_setting(key, default=''):
    """Read a setting from the shared app settings DB."""
    try:
        conn = sqlite3.connect(SETTINGS_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (f'skillcards.{key}',)).fetchone()
        conn.close()
        return row['value'] if row and row['value'] else default
    except Exception:
        return default


def _db():
    conn = sqlite3.connect(SKILLCARDS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _init_db():
    conn = _db()
    conn.execute('''CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        metadata TEXT DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''')
    # Add metadata column if missing (migration)
    try:
        conn.execute("SELECT metadata FROM cards LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE cards ADD COLUMN metadata TEXT DEFAULT '{}'")
    conn.commit()
    conn.close()


_init_db()


@skillcards_bp.route('/skill-cards')
def gallery_page():
    return render_template('skillcards.html')


@skillcards_bp.route('/skill-cards/api/cards', methods=['GET'])
def list_cards():
    q = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    conn = _db()
    rows = conn.execute('SELECT * FROM cards ORDER BY updated_at DESC').fetchall()
    conn.close()
    cards = []
    for r in rows:
        c = dict(r)
        c['tags'] = json.loads(c['tags'])
        try:
            c['metadata'] = json.loads(c.get('metadata') or '{}')
        except (json.JSONDecodeError, TypeError):
            c['metadata'] = {}
        if q and q not in c['title'].lower() and q not in c['content'].lower() and not any(q in t for t in c['tags']):
            continue
        if tag and tag not in c['tags']:
            continue
        cards.append(c)
    return jsonify(cards)


@skillcards_bp.route('/skill-cards/api/cards', methods=['POST'])
def create_card():
    data = request.get_json()
    if not data or not data.get('title', '').strip():
        return jsonify({'error': 'title required'}), 400
    now = datetime.utcnow().isoformat() + 'Z'
    card_id = data.get('id') or _gen_id()
    tags = json.dumps(data.get('tags', []))
    conn = _db()
    conn.execute(
        'INSERT INTO cards (id, title, content, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
        (card_id, data['title'].strip(), data.get('content', ''), tags, now, now)
    )
    conn.commit()
    conn.close()
    return jsonify({'id': card_id, 'title': data['title'].strip(), 'content': data.get('content', ''), 'tags': data.get('tags', []), 'created_at': now, 'updated_at': now}), 201


@skillcards_bp.route('/skill-cards/api/cards/<card_id>', methods=['PUT'])
def update_card(card_id):
    data = request.get_json()
    if not data or not data.get('title', '').strip():
        return jsonify({'error': 'title required'}), 400
    now = datetime.utcnow().isoformat() + 'Z'
    tags = json.dumps(data.get('tags', []))
    conn = _db()
    conn.execute(
        'UPDATE cards SET title=?, content=?, tags=?, updated_at=? WHERE id=?',
        (data['title'].strip(), data.get('content', ''), tags, now, card_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'id': card_id, 'title': data['title'].strip(), 'content': data.get('content', ''), 'tags': data.get('tags', []), 'updated_at': now})


@skillcards_bp.route('/skill-cards/api/cards/<card_id>', methods=['DELETE'])
def delete_card(card_id):
    conn = _db()
    conn.execute('DELETE FROM cards WHERE id=?', (card_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def _scan_skills_dir(skills_dir, source_tag=None):
    """Scan a directory for SKILL.md files and upsert into the cards DB.
    Returns (imported, errors) counts."""
    if not os.path.isdir(skills_dir):
        return 0, [f'Directory not found: {skills_dir}']

    imported = 0
    errors = []
    conn = _db()

    for entry in sorted(os.listdir(skills_dir)):
        skill_path = os.path.join(skills_dir, entry, 'SKILL.md')
        if not os.path.isfile(skill_path):
            continue

        try:
            with open(skill_path, 'r') as f:
                raw = f.read()
        except Exception as e:
            errors.append(f'{entry}: {e}')
            continue

        # Parse YAML frontmatter — capture ALL fields
        name = entry
        tags = []
        body = raw
        metadata = {}

        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', raw, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            body = fm_match.group(2).strip()

            for line in fm_text.split('\n'):
                if ':' not in line:
                    continue
                key, val = line.split(':', 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                metadata[key] = val

                if key == 'name':
                    name = val
                elif key == 'tags':
                    tag_str = val.strip('[]')
                    tags = [t.strip().strip('"').strip("'") for t in tag_str.split(',') if t.strip()]

        # Add source tag if provided
        if source_tag and source_tag not in tags:
            tags.append(source_tag)

        card_id = f'skill-{entry}'
        title = name or entry
        metadata_json = json.dumps(metadata)

        existing = conn.execute('SELECT id FROM cards WHERE id = ?', (card_id,)).fetchone()
        now = datetime.utcnow().isoformat() + 'Z'
        tags_json = json.dumps(tags)

        if existing:
            conn.execute('UPDATE cards SET content=?, metadata=?, updated_at=? WHERE id=?',
                         (body, metadata_json, now, card_id))
        else:
            conn.execute('INSERT INTO cards (id, title, content, tags, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (card_id, title, body, tags_json, metadata_json, now, now))
        imported += 1

    conn.commit()
    conn.close()
    return imported, errors


@skillcards_bp.route('/skill-cards/api/sync-skills', methods=['POST'])
def sync_skills_from_disk():
    """Scan skills directory for SKILL.md files and upsert into cards DB.
    Accepts optional JSON body: { "path": "/custom/path" }"""
    data = request.get_json(silent=True) or {}

    # Priority: request body > app setting > env var default
    skills_path = data.get('path', '').strip()
    if not skills_path:
        skills_path = _get_app_setting('skillsDir', SKILLS_DIR)

    imported, errors = _scan_skills_dir(skills_path, source_tag='local')
    if errors and imported == 0:
        return jsonify({'error': errors[0], 'imported': 0}), 404
    return jsonify({'imported': imported, 'errors': errors, 'path': skills_path})


def _git_env(ssh_key=''):
    """Build env dict with SSH key for private repos."""
    env = os.environ.copy()
    if ssh_key and os.path.isfile(ssh_key):
        env['GIT_SSH_COMMAND'] = f'ssh -i {ssh_key} -F /dev/null -o StrictHostKeyChecking=no -o IdentitiesOnly=yes'
    else:
        env['GIT_SSH_COMMAND'] = 'ssh -o StrictHostKeyChecking=no'
    return env


def _git_clone_or_pull(repo, branch, ssh_key='', target_dir=None):
    """Clone or pull the remote repo into target_dir. Returns git env."""
    import shutil as _shutil
    if target_dir is None:
        target_dir = SKILLS_REMOTE_DIR
    env = _git_env(ssh_key)
    git_dir = os.path.join(target_dir, '.git')
    os.makedirs(target_dir, exist_ok=True)

    if os.path.isdir(git_dir):
        subprocess.run(['git', '-C', target_dir, 'fetch', '--depth', '1', 'origin', branch],
                       capture_output=True, timeout=30, check=True, env=env)
        subprocess.run(['git', '-C', target_dir, 'reset', '--hard', f'origin/{branch}'],
                       capture_output=True, timeout=10, check=True)
    else:
        if os.path.exists(target_dir):
            _shutil.rmtree(target_dir)
        result = subprocess.run(['git', 'clone', '--depth', '1', '--branch', branch, repo, target_dir],
                       capture_output=True, timeout=60, env=env)
        if result.returncode != 0:
            os.makedirs(target_dir, exist_ok=True)
            subprocess.run(['git', 'init', target_dir], check=True, capture_output=True)
            subprocess.run(['git', '-C', target_dir, 'remote', 'add', 'origin', repo],
                         check=True, capture_output=True)
            subprocess.run(['git', '-C', target_dir, 'checkout', '-b', branch],
                         check=True, capture_output=True)
    return env


def _card_has_tag(card_dict, tag):
    """Check if a card has a specific tag."""
    try:
        tags = json.loads(card_dict.get('tags') or '[]')
    except (json.JSONDecodeError, TypeError):
        tags = []
    return tag in tags


def _export_cards_to_dir(cards, target_dir):
    """Export a list of card dicts as SKILL.md files into target_dir. Returns count."""
    import shutil as _shutil
    # Clean push: wipe target (keep .git)
    if os.path.isdir(target_dir):
        for entry in os.listdir(target_dir):
            if entry == '.git':
                continue
            entry_path = os.path.join(target_dir, entry)
            if os.path.isdir(entry_path):
                _shutil.rmtree(entry_path)
            else:
                os.remove(entry_path)
    os.makedirs(target_dir, exist_ok=True)

    exported = 0
    for c in cards:
        dir_name = c['id'].replace('skill-', '') if c['id'].startswith('skill-') else c['id']
        skill_dir = os.path.join(target_dir, dir_name)
        os.makedirs(skill_dir, exist_ok=True)

        try:
            meta = json.loads(c.get('metadata') or '{}')
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta['name'] = c['title']
        try:
            tags = json.loads(c.get('tags') or '[]')
        except (json.JSONDecodeError, TypeError):
            tags = []
        if tags:
            meta['tags'] = '[' + ', '.join(tags) + ']'

        fm_lines = ['---']
        for k, v in meta.items():
            fm_lines.append(f'{k}: {v}')
        fm_lines.append('---')
        content = '\n'.join(fm_lines) + '\n\n' + (c.get('content') or '')

        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as f:
            f.write(content)
        exported += 1
    return exported


def _git_add_commit_push(repo_dir, message, env):
    """Git add, commit, push. Returns True if pushed."""
    subprocess.run(['git', '-C', repo_dir, 'add', '.'], check=True, capture_output=True)
    result = subprocess.run(['git', '-C', repo_dir, 'status', '--porcelain'],
                          capture_output=True, text=True, check=True)
    if not result.stdout.strip():
        return False
    subprocess.run(['git', '-C', repo_dir, 'config', 'user.name', 'Skill Cards'],
                 check=True, capture_output=True)
    subprocess.run(['git', '-C', repo_dir, 'config', 'user.email', 'skills@local'],
                 check=True, capture_output=True)
    subprocess.run(['git', '-C', repo_dir, 'commit', '-m', message],
                 check=True, capture_output=True)
    subprocess.run(['git', '-C', repo_dir, 'push', '-u', 'origin',
                   subprocess.run(['git', '-C', repo_dir, 'branch', '--show-current'],
                                 capture_output=True, text=True).stdout.strip() or 'main'],
                 check=True, capture_output=True, env=env)
    return True


@skillcards_bp.route('/skill-cards/api/sync-remote', methods=['POST'])
def sync_remote():
    """Sync cards with remote git repos.
    Public repo: all cards WITHOUT 'private' tag.
    Private repo: only cards WITH 'private' tag.
    JSON body: { "direction": "pull"|"push", "repo": "...", "privateRepo": "...", "branch": "main", "sshKey": "" }"""
    data = request.get_json(silent=True) or {}

    direction = data.get('direction', 'pull').strip()
    public_repo = data.get('repo', '').strip() or _get_app_setting('remoteRepo', os.environ.get('SKILL_GITHUB_REPO', ''))
    private_repo = data.get('privateRepo', '').strip() or _get_app_setting('privateRepo', os.environ.get('SKILL_PRIVATE_GITHUB_REPO', ''))
    branch = data.get('branch', '').strip() or _get_app_setting('remoteBranch', 'main')
    subdir = data.get('subdir', '').strip() or _get_app_setting('remoteSubdir', '')
    ssh_key = data.get('sshKey', '').strip() or _get_app_setting('remoteSshKey', os.environ.get('SKILL_SSH_KEY', ''))

    if not public_repo and not private_repo:
        return jsonify({'error': 'No remote repo configured'}), 400

    results = {}
    now = datetime.utcnow().isoformat()

    # --- Public repo ---
    if public_repo:
        try:
            pub_env = _git_clone_or_pull(public_repo, branch, ssh_key, SKILLS_REMOTE_DIR)
            pub_path = os.path.join(SKILLS_REMOTE_DIR, subdir) if subdir else SKILLS_REMOTE_DIR

            if direction == 'push':
                conn = _db()
                all_cards = [dict(r) for r in conn.execute('SELECT * FROM cards ORDER BY title').fetchall()]
                conn.close()
                public_cards = [c for c in all_cards if not _card_has_tag(c, 'private')]
                exported = _export_cards_to_dir(public_cards, pub_path)
                pushed = _git_add_commit_push(SKILLS_REMOTE_DIR, f'Sync {exported} public cards at {now}', pub_env)
                results['public'] = {'exported': exported, 'pushed': pushed, 'repo': public_repo}
            else:
                imported, errors = _scan_skills_dir(pub_path, source_tag='remote')
                results['public'] = {'imported': imported, 'errors': errors, 'repo': public_repo}
        except subprocess.CalledProcessError as e:
            results['public'] = {'error': f'Git failed: {(e.stderr or b"").decode().strip()}'}
        except subprocess.TimeoutExpired:
            results['public'] = {'error': 'Git timed out'}

    # --- Private repo ---
    if private_repo:
        try:
            priv_env = _git_clone_or_pull(private_repo, branch, ssh_key, SKILLS_REMOTE_PRIVATE_DIR)
            priv_path = os.path.join(SKILLS_REMOTE_PRIVATE_DIR, subdir) if subdir else SKILLS_REMOTE_PRIVATE_DIR

            if direction == 'push':
                conn = _db()
                all_cards = [dict(r) for r in conn.execute('SELECT * FROM cards ORDER BY title').fetchall()]
                conn.close()
                private_cards = [c for c in all_cards if _card_has_tag(c, 'private')]
                exported = _export_cards_to_dir(private_cards, priv_path)
                pushed = _git_add_commit_push(SKILLS_REMOTE_PRIVATE_DIR, f'Sync {exported} private cards at {now}', priv_env)
                results['private'] = {'exported': exported, 'pushed': pushed, 'repo': private_repo}
            else:
                imported, errors = _scan_skills_dir(priv_path, source_tag='private')
                results['private'] = {'imported': imported, 'errors': errors, 'repo': private_repo}
        except subprocess.CalledProcessError as e:
            results['private'] = {'error': f'Git failed: {(e.stderr or b"").decode().strip()}'}
        except subprocess.TimeoutExpired:
            results['private'] = {'error': 'Git timed out'}

    return jsonify(results)


def _gen_id():
    import time
    import random
    return f"{int(time.time()):x}{random.randint(0, 0xfffff):05x}"
