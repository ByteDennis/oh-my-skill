from flask import Blueprint, request, jsonify
import os
import json
import re
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime

from oh_my_skill.shared.config import SETTINGS_DB

skillcards_bp = Blueprint('skillcards', __name__)

SKILLCARDS_DB = os.environ.get('SKILLCARDS_DB', '/data/skillcards.db')
SKILLS_DIR = os.environ.get('SKILLS_DIR', '/skills')
SKILLS_REMOTE_DIR = '/data/skills_remote'
SKILLS_REMOTE_PRIVATE_DIR = '/data/skills_private'
IMAGES_DIR = os.path.join(os.path.dirname(SKILLCARDS_DB), 'images')

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
    conn.execute('PRAGMA foreign_keys=ON')  # lineage_items.card_id cascades on card delete
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


@skillcards_bp.route('/skill-cards/api/rss', methods=['GET'])
def rss_feed():
    """Lean feed for the external RSS service (pull model).

    Returns ONLY cards tagged `rss`, in a stable, minimal shape:
        [{id, title, content, tags, updated_at}, ...]

    The RSS service (configured to point at this skill app) polls this
    endpoint; unlike /skill-cards/api/cards it drops `metadata`,
    `created_at`, and sync bookkeeping columns, and never returns
    untagged cards.
    """
    conn = _db()
    rows = conn.execute('SELECT * FROM cards ORDER BY updated_at DESC').fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            tags = json.loads(r['tags'])
        except (json.JSONDecodeError, TypeError):
            tags = []
        if 'rss' not in tags:
            continue
        out.append({
            'id': r['id'],
            'title': r['title'],
            'content': r['content'],
            'tags': tags,
            'updated_at': r['updated_at'],
        })
    return jsonify(out)


# ─── Server-side markdown → self-contained HTML (for the mobile app) ───
# markdown-it-py + Pygments (code) + latex2mathml (math → MathML, rendered
# natively by WebKit). Output is a complete HTML doc with inline CSS and no
# external resources / JS, so the app just displays it (offline once cached).
import re as _re
import html as _html
from markdown_it import MarkdownIt
from pygments import highlight as _pyg_highlight
from pygments.lexers import get_lexer_by_name, guess_lexer, TextLexer
from pygments.formatters import HtmlFormatter

try:
    from latex2mathml.converter import convert as _tex2mathml
except Exception:  # pragma: no cover - optional dependency
    _tex2mathml = None


def _highlight_code(code, lang, attrs):
    try:
        lexer = get_lexer_by_name(lang, stripnl=False) if lang else guess_lexer(code)
    except Exception:
        lexer = TextLexer(stripnl=False)
    inner = _pyg_highlight(code, lexer, HtmlFormatter(nowrap=True))
    label = f'<span class="lang-label">{_html.escape(lang)}</span>' if lang else ''
    return f'<pre class="code">{label}<code>{inner}</code></pre>'


_md = MarkdownIt("commonmark", {
    "html": False, "breaks": True, "linkify": False, "highlight": _highlight_code,
}).enable(["table", "strikethrough"])

_PYGMENTS_CSS = HtmlFormatter(style="github-dark").get_style_defs(".code")

_CONTENT_CSS = (
    "body{font-family:-apple-system,system-ui,sans-serif;font-size:16px;color:#1c1c1e;"
    "margin:0;padding:16px 18px 48px;line-height:1.55;-webkit-text-size-adjust:100%}"
    "h1.title{font-size:22px;font-weight:700;margin:0 0 6px;line-height:1.25}"
    "#tags{margin:0 0 14px}"
    ".tag{display:inline-block;font-size:12px;color:#e8590f;background:rgba(242,107,29,.12);"
    "padding:3px 9px;border-radius:999px;margin:0 5px 5px 0}"
    "h1{font-size:19px;color:#0a84ff;margin:18px 0 8px;font-weight:700}"
    "h2{font-size:17px;color:#2da44e;margin:16px 0 6px;font-weight:700}"
    "h3{font-size:15.5px;color:#8250df;margin:13px 0 4px;font-weight:700}"
    "a{color:#0a84ff;text-decoration:none}ul,ol{padding-left:22px;margin:6px 0}li{margin:3px 0}"
    "strong{font-weight:700}del{color:#8e8e93;text-decoration:line-through}"
    "hr{border:none;height:1px;background:#d0d3d8;margin:18px 0}img{max-width:100%;border-radius:8px}"
    "table{width:100%;border-collapse:collapse;margin:10px 0;font-size:14px;display:block;overflow-x:auto}"
    "th,td{border:1px solid #d0d3d8;padding:6px 10px;text-align:left}th{background:#f6f8fa;font-weight:600}"
    ":not(pre)>code{background:#eef1f4;color:#1c1c1e;padding:2px 6px;border-radius:4px;font-size:.88em;"
    "font-family:ui-monospace,'SF Mono',Menlo,monospace}"
    "pre.code{background:#0d1117;color:#c9d1d9;border-radius:10px;padding:14px 16px;margin:12px 0;"
    "overflow-x:auto;font-size:13.5px;position:relative}"
    "pre.code code{font-family:ui-monospace,'SF Mono',Menlo,monospace;background:none;padding:0}"
    "pre.code .lang-label{position:absolute;right:12px;top:8px;font-size:10px;color:#8b949e;"
    "text-transform:uppercase;letter-spacing:1px}"
    "blockquote{border-left:3px solid #0a84ff;padding:8px 14px;margin:12px 0;"
    "background:rgba(10,132,255,.06);border-radius:0 8px 8px 0}"
    ".tldr{border-left:3px solid #e8590f;background:rgba(242,107,29,.10);padding:8px 14px;"
    "margin:12px 0;border-radius:0 8px 8px 0;line-height:1.5}"
    ".tldr-label{display:inline-block;font-size:11px;font-weight:800;color:#e8590f;"
    "letter-spacing:.05em;margin-right:8px}"
    "math{font-size:1.05em}.math-display{display:block;overflow-x:auto;margin:12px 0;text-align:center}"
    "@media(prefers-color-scheme:dark){body{color:#e6e6ea}h1.title{color:#fff}"
    ":not(pre)>code{background:#2c2c2e;color:#e6e6ea}th{background:#1c1c1e}"
    "th,td{border-color:#3a3a3c}hr{background:#3a3a3c}}"
)


def _to_mathml(tex, block):
    try:
        return _tex2mathml(tex, display="block" if block else "inline")
    except TypeError:
        return _tex2mathml(tex)


def _protect_math(text):
    blocks = []

    def repl(m, block):
        idx = len(blocks)
        raw = m.group(0)
        tex = m.group(1).strip()
        if _tex2mathml:
            try:
                ml = _to_mathml(tex, block)
                blocks.append(f'<div class="math-display">{ml}</div>' if block else ml)
            except Exception:
                blocks.append(f'<code>{_html.escape(raw)}</code>')
        else:
            blocks.append(f'<code>{_html.escape(raw)}</code>')
        return f'%%MATH{idx}%%'

    text = _re.sub(r'\$\$([\s\S]+?)\$\$', lambda m: repl(m, True), text)
    text = _re.sub(r'\$([^$\n]+?)\$', lambda m: repl(m, False), text)
    return text, blocks


# >>> a line like `TL;DR > xxx <` or `> xxx <` → a clean highlighted callout (strips the < > markers) <<< #
_TLDR_RE = _re.compile(r'(?m)^[ \t]*(TL;DR[ \t]*)?>[ \t]*(.+?)[ \t]*<[ \t]*$')


def _protect_tldr(text):
    blocks = []

    def repl(m):
        idx = len(blocks)
        lab = '<span class="tldr-label">TL;DR</span>' if m.group(1) else ''
        blocks.append(f'<div class="tldr">{lab}{_html.escape(m.group(2).strip())}</div>')
        return f'%%TLDR{idx}%%'

    return _TLDR_RE.sub(repl, text), blocks


def _render_card_html(row):
    try:
        tags = json.loads(row['tags'])
    except (json.JSONDecodeError, TypeError):
        tags = []
    protected, blocks = _protect_math(row['content'] or '')
    protected, tldrs = _protect_tldr(protected)
    body = _md.render(protected)
    for i, b in enumerate(tldrs):
        body = body.replace(f'<p>%%TLDR{i}%%</p>', b).replace(f'%%TLDR{i}%%', b)
    for i, b in enumerate(blocks):
        body = body.replace(f'%%MATH{i}%%', b)
    title = _html.escape(row['title'] or '(untitled)')
    tag_html = ''.join(f'<span class="tag">{_html.escape(t)}</span>' for t in tags)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">'
        f'<style>{_CONTENT_CSS}{_PYGMENTS_CSS}</style></head><body>'
        f'<h1 class="title">{title}</h1><div id="tags">{tag_html}</div>'
        f'{body}</body></html>'
    )


@skillcards_bp.route('/skill-cards/api/cards/<card_id>/html', methods=['GET'])
def card_html(card_id):
    """Server-rendered, self-contained HTML for one card (mobile app)."""
    conn = _db()
    row = conn.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'html': _render_card_html(row)})


def _merge_link_meta(existing_meta_json: str, parent_id, links) -> str:
    """Merge parent_id + links into the existing metadata JSON blob.
    Pass `None` to leave a field untouched; pass '' / [] to clear it."""
    try:
        meta = json.loads(existing_meta_json or '{}')
    except (json.JSONDecodeError, TypeError):
        meta = {}
    if parent_id is not None:
        if parent_id == '' or parent_id is False:
            meta.pop('parent_id', None)
        else:
            meta['parent_id'] = str(parent_id)
    if links is not None:
        if isinstance(links, list):
            # De-dup, drop empties, drop self-refs handled at write time
            cleaned = sorted({str(x) for x in links if x})
            if cleaned:
                meta['links'] = cleaned
            else:
                meta.pop('links', None)
    return json.dumps(meta)


@skillcards_bp.route('/skill-cards/api/cards', methods=['POST'])
def create_card():
    data = request.get_json()
    if not data or not data.get('title', '').strip():
        return jsonify({'error': 'title required'}), 400
    now = datetime.utcnow().isoformat() + 'Z'
    card_id = data.get('id') or _gen_id()
    tags = json.dumps(data.get('tags', []))
    meta_json = _merge_link_meta('{}', data.get('parent_id'), data.get('links'))
    category = (data.get('category') or '').strip()
    if category:
        meta = json.loads(meta_json)
        meta['category'] = category
        meta_json = json.dumps(meta)
    conn = _db()
    conn.execute(
        'INSERT INTO cards (id, title, content, tags, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (card_id, data['title'].strip(), data.get('content', ''), tags, meta_json, now, now)
    )
    conn.commit()
    conn.close()
    out = {
        'id': card_id, 'title': data['title'].strip(),
        'content': data.get('content', ''), 'tags': data.get('tags', []),
        'metadata': json.loads(meta_json),
        'created_at': now, 'updated_at': now,
    }
    return jsonify(out), 201


@skillcards_bp.route('/skill-cards/api/cards/<card_id>', methods=['PUT'])
def update_card(card_id):
    data = request.get_json()
    if not data or not data.get('title', '').strip():
        return jsonify({'error': 'title required'}), 400
    # Self-link guard
    if data.get('parent_id') == card_id:
        return jsonify({'error': 'card cannot be its own parent'}), 400
    if isinstance(data.get('links'), list) and card_id in data['links']:
        data['links'] = [x for x in data['links'] if x != card_id]
    now = datetime.utcnow().isoformat() + 'Z'
    tags = json.dumps(data.get('tags', []))
    conn = _db()
    row = conn.execute('SELECT metadata FROM cards WHERE id=?', (card_id,)).fetchone()
    cur_meta = row['metadata'] if row else '{}'
    meta_json = _merge_link_meta(cur_meta, data.get('parent_id'), data.get('links'))
    conn.execute(
        'UPDATE cards SET title=?, content=?, tags=?, metadata=?, updated_at=? WHERE id=?',
        (data['title'].strip(), data.get('content', ''), tags, meta_json, now, card_id)
    )
    conn.commit()
    conn.close()
    return jsonify({
        'id': card_id, 'title': data['title'].strip(),
        'content': data.get('content', ''), 'tags': data.get('tags', []),
        'metadata': json.loads(meta_json),
        'updated_at': now,
    })


@skillcards_bp.route('/skill-cards/api/cards/<card_id>', methods=['DELETE'])
def delete_card(card_id):
    conn = _db()
    conn.execute('DELETE FROM cards WHERE id=?', (card_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# >>> set/clear a card's category in metadata (empty string = uncategorized) <<< #
@skillcards_bp.route('/skill-cards/api/cards/<card_id>/category', methods=['POST'])
def set_card_category(card_id):
    data = request.get_json(silent=True) or {}
    category = (data.get('category') or '').strip()
    conn = _db()
    row = conn.execute('SELECT metadata FROM cards WHERE id=?', (card_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    try:
        meta = json.loads(row['metadata'] or '{}')
    except (json.JSONDecodeError, TypeError):
        meta = {}
    if category:
        meta['category'] = category
    else:
        meta.pop('category', None)
    conn.execute('UPDATE cards SET metadata=? WHERE id=?', (json.dumps(meta), card_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'category': category})


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
                # Only cards explicitly tagged `public` go to the public repo;
                # untagged cards are excluded (and wiped from the remote by the
                # clean re-export below), matching the Sync Center's routing.
                public_cards = [c for c in all_cards if _card_has_tag(c, 'public')]
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


# ── Image upload ──────────────────────────────────────────────────────────────

_ALLOWED_IMG_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'}

@skillcards_bp.route('/skill-cards/api/upload-image', methods=['POST'])
def upload_image():
    """Accept a pasted image, save it under /data/images/{card_id}/, return its URL."""
    f = request.files.get('image')
    if not f:
        return jsonify({'error': 'no image file'}), 400

    card_id = (request.form.get('card_id') or '_unlinked').strip()
    card_id = re.sub(r'[^\w\-]', '_', card_id)[:64] or '_unlinked'

    # Determine extension — use Content-Type when filename has none
    ext = os.path.splitext(f.filename or '')[1].lower()
    if ext not in _ALLOWED_IMG_EXTS:
        mime = (f.content_type or '').split(';')[0].strip()
        ext = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
               'image/webp': '.webp', 'image/svg+xml': '.svg', 'image/bmp': '.bmp'}.get(mime, '.png')

    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    fname = f'{ts}_{uuid.uuid4().hex[:8]}{ext}'

    folder = os.path.join(IMAGES_DIR, card_id)
    os.makedirs(folder, exist_ok=True)
    f.save(os.path.join(folder, fname))

    return jsonify({'url': f'/omi/images/{card_id}/{fname}'}), 201
