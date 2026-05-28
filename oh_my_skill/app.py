"""oh-my-skill — Claude-powered skill cards manager (Flask app factory).

Runtime env defaults are bootstrapped in oh_my_skill/__init__.py BEFORE any
submodule reads OMI_DATA_DIR / SETTINGS_DB / etc.
"""
import os
import sqlite3
from importlib.resources import files as _pkg_files

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

from oh_my_skill.routes.skillcards import skillcards_bp
from oh_my_skill.routes.chat import chat_bp
from oh_my_skill.routes.sync import sync_bp
from oh_my_skill.routes.lineage import lineage_bp
from oh_my_skill.shared.ai_providers import (
    DEFAULT_CHAT_MODELS, active_chat_status, claude_status, codex_status,
    get_chat_model, get_chat_provider, get_model_options,
)
from oh_my_skill.shared.config import (
    SETTINGS_DB, get_setting, get_all, put_setting, seed_global_settings_from_env,
)
from oh_my_skill.shared import chat_store, logger
from oh_my_skill.shared.system_prompt import render_to_disk as render_skill_brain
from oh_my_skill.shared.themes import list_colors, list_fonts


_DATA_DIR = os.environ['OMI_DATA_DIR']  # set in oh_my_skill/__init__.py
_PKG_STATIC = str(_pkg_files('oh_my_skill').joinpath('static'))


def create_app() -> Flask:
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

    seed_global_settings_from_env()

    # Render the SKILL_SYSTEM_PROMPT brain to disk so per-card chat sessions
    # can --append-system-prompt-file it.
    try:
        render_skill_brain(os.path.join(_DATA_DIR, '.skill-system-prompt.md'))
    except Exception:
        pass

    # Retention sweeper for stale chat sessions (default 7 days)
    chat_store.start_sweeper()

    app.register_blueprint(skillcards_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(lineage_bp)

    # ── Static @oh-my/ui (packaged, with optional override via env) ────
    ui_dir = os.environ.get('OH_MY_UI_DIR') or _PKG_STATIC
    if not os.path.isfile(os.path.join(ui_dir, 'css', 'omi.css')):
        for cand in ('/opt/oh-my-ui',):
            if os.path.isfile(os.path.join(cand, 'css', 'omi.css')):
                ui_dir = cand
                break

    # Bundled cli_helpers (oms-save, oms-tag, …) on the PATH so the chat
    # workspace can call them. Done lazily and idempotently on app start.
    try:
        cli_helpers = str(_pkg_files('oh_my_skill').joinpath('cli_helpers'))
        if os.path.isdir(cli_helpers):
            os.environ['PATH'] = cli_helpers + os.pathsep + os.environ.get('PATH', '')
    except Exception:
        pass

    @app.route('/omi/ui/<path:fname>')
    def omi_ui(fname):
        return send_from_directory(ui_dir, fname)

    # ── Pages ──────────────────────────────────────────────────────────
    @app.route('/')
    def home():
        return render_template('skill.html')

    @app.route('/healthz')
    def healthz():
        return {'ok': True}

    @app.route('/api/ai/status')
    def ai_status():
        """Per-feature AI availability used by the UI."""
        chat = active_chat_status()
        extract = claude_status()
        return jsonify({
            'configured': bool(chat.get('configured') or extract.get('configured')),
            'chat_provider': get_chat_provider(),
            'chat': chat,
            'extract': extract,
            'claude': extract,
            'codex': codex_status(),
        })

    @app.route('/api/themes')
    def themes_list():
        return jsonify({'colors': list_colors(), 'fonts': list_fonts()})

    @app.route('/api/runtime')
    def runtime_info():
        """Container/host paths the UI uses to surface real filesystem
        locations to the user (card workspace, DBs, etc.)."""
        host_home = os.environ.get('HOST_HOME') or os.path.expanduser('~')
        # The DB lives under /data inside the container; for the host path
        # we assume the conventional bind-mount: $HOST_HOME/.local/files/oh-my-skill/data
        host_data = os.path.join(host_home, '.local/files/oh-my-skill/data')
        return jsonify({
            'host_home': host_home,
            'host_data_dir': host_data,
            'host_db_path': os.path.join(host_data, 'skillcards.db'),
            'host_workspace_root': os.path.join(host_data, 'projects'),
            'container_data_dir': _DATA_DIR,
        })

    # ── Settings (per-service) ─────────────────────────────────────────
    @app.route('/api/settings', methods=['GET'])
    def settings_get():
        cur = get_all('skill')
        return jsonify({
            'color_theme': cur.get('color_theme', 'classic-blue'),
            'font_theme': cur.get('font_theme', 'modern'),
            'chat_retention_days': int(cur.get('chat_retention_days') or 7),
            'chat_provider': cur.get('chat_provider', 'claude'),
            'chat_model': get_chat_model(),
            'claude_chat_model': get_chat_model('claude'),
            'codex_chat_model': get_chat_model('codex'),
            'chat_model_options': {
                'claude': get_model_options('claude'),
                'codex': get_model_options('codex'),
            },
            'claude_token_set': bool(get_setting('global', 'claude_code_oauth_token')),
            'img_paste_enabled': cur.get('img_paste_enabled', 'true') != 'false',
        })

    @app.route('/api/settings', methods=['PUT'])
    def settings_put():
        data = request.get_json() or {}
        valid_models = {
            'claude_chat_model': {m['id'] for m in get_model_options('claude')},
            'codex_chat_model': {m['id'] for m in get_model_options('codex')},
        }
        for k in ('color_theme', 'font_theme', 'chat_provider',
                  'claude_chat_model', 'codex_chat_model'):
            if k in data:
                v = (data[k] or '').strip() if isinstance(data[k], str) else data[k]
                if k == 'chat_provider' and v not in ('claude', 'codex'):
                    continue
                if k in valid_models and v not in valid_models[k]:
                    v = DEFAULT_CHAT_MODELS['claude' if k.startswith('claude_') else 'codex']
                put_setting('skill', k, v or '')
        if 'chat_retention_days' in data:
            try:
                n = int(data['chat_retention_days'])
                if n >= 0:
                    put_setting('skill', 'chat_retention_days', str(n))
            except (TypeError, ValueError):
                pass
        if 'claude_code_oauth_token' in data and isinstance(data['claude_code_oauth_token'], str):
            v = data['claude_code_oauth_token'].strip()
            if v:
                put_setting('global', 'claude_code_oauth_token', v)
        if 'img_paste_enabled' in data:
            put_setting('skill', 'img_paste_enabled', 'true' if data['img_paste_enabled'] else 'false')
        return jsonify({'ok': True})

    # ── Serve local image files for inline preview ─────────────────────
    # Card markdown can reference absolute paths like
    # `![](/home/dalab2/.local/files/images/foo.png)`. The browser asks the
    # Flask server for that URL, which it doesn't own; we serve it here,
    # whitelisting image MIME types and limiting to a few safe roots.
    _IMG_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.bmp'}
    _MIME = {
        '.png':  'image/png',  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.gif': 'image/gif',  '.svg':  'image/svg+xml',
        '.bmp':  'image/bmp',
    }
    _ALLOWED_ROOTS = [
        os.path.realpath(os.path.expanduser('~')),
        os.path.realpath(_DATA_DIR),
        '/home', '/tmp', '/data',
    ]

    @app.route('/file')
    def serve_local_file():
        p = request.args.get('path', '')
        if not p:
            return 'path required', 400
        try:
            real = os.path.realpath(os.path.expanduser(p))
        except Exception:
            return 'bad path', 400
        if not os.path.isfile(real):
            return 'not found', 404
        if not any(real == r or real.startswith(r + os.sep) for r in _ALLOWED_ROOTS):
            return 'forbidden', 403
        ext = os.path.splitext(real)[1].lower()
        if ext not in _IMG_EXTS:
            return 'not an image', 415
        return send_file(real, mimetype=_MIME.get(ext, 'application/octet-stream'))

    # ── Serve uploaded card images ─────────────────────────────────────
    _IMAGES_DIR = os.path.join(_DATA_DIR, 'images')

    @app.route('/omi/images/<card_id>/<fname>')
    def serve_uploaded_image(card_id, fname):
        if '..' in card_id or '..' in fname:
            return 'forbidden', 403
        folder = os.path.join(_IMAGES_DIR, card_id)
        return send_from_directory(folder, fname)

    # ── Helper for `oms-save` — the CLI in the chat workspace calls this
    @app.route('/api/cards/<card_id>/sync-from-disk', methods=['POST'])
    def card_sync_from_disk(card_id):
        """Read ./card.md from a project workspace and update the card in DB.

        Parses optional YAML-ish frontmatter for `parent:` and `links:` so
        cascade linkage round-trips through git/disk:

            ---
            parent: <card-id>
            links: [<card-id>, <card-id>]
            ---
            # Title
            …content…
        """
        import json as _json
        skillcards_db = os.environ.get('SKILLCARDS_DB',
                                       os.path.join(_DATA_DIR, 'skillcards.db'))
        data = request.get_json() or {}
        cwd = data.get('cwd', '')
        md_path = os.path.join(cwd, 'card.md') if cwd else ''
        if not md_path or not os.path.exists(md_path):
            return jsonify({'error': 'card.md not found'}), 404
        raw = open(md_path).read()
        lines = raw.splitlines()

        # ── Frontmatter parse ────────────────────────────────────────
        parent_id, links = None, None
        body_start = 0
        if lines and lines[0].strip() == '---':
            for i in range(1, min(len(lines), 30)):
                if lines[i].strip() == '---':
                    body_start = i + 1
                    for fm in lines[1:i]:
                        m = fm.strip()
                        if m.startswith('parent:'):
                            parent_id = m.split(':', 1)[1].strip().strip('"\'') or ''
                        elif m.startswith('links:'):
                            v = m.split(':', 1)[1].strip()
                            if v.startswith('['):
                                try:
                                    links = [str(x).strip().strip('"\'') for x in _json.loads(v.replace("'", '"'))]
                                except Exception:
                                    links = []
                            else:
                                links = [s.strip() for s in v.split(',') if s.strip()]
                    break

        # ── Title + content extract ──────────────────────────────────
        title = ''
        content_start = body_start
        for i, ln in enumerate(lines[body_start:], start=body_start):
            if ln.startswith('# '):
                title = ln[2:].strip()
                content_start = i + 1
                break
        if not title:
            return jsonify({'error': 'no H1 title found in card.md'}), 400
        content = '\n'.join(lines[content_start:]).lstrip('\n')

        from datetime import datetime
        now = datetime.utcnow().isoformat() + 'Z'
        conn = sqlite3.connect(skillcards_db)
        conn.row_factory = sqlite3.Row
        cur = conn.execute('SELECT metadata FROM cards WHERE id=?', (card_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'card not found in DB'}), 404
        # Merge parent_id / links into existing metadata if frontmatter set them
        try:
            meta = _json.loads(row['metadata'] or '{}')
        except Exception:
            meta = {}
        if parent_id is not None:
            if parent_id == '' or parent_id == card_id:
                meta.pop('parent_id', None)
            else:
                meta['parent_id'] = parent_id
        if links is not None:
            cleaned = sorted({x for x in links if x and x != card_id})
            if cleaned:
                meta['links'] = list(cleaned)
            else:
                meta.pop('links', None)
        conn.execute(
            'UPDATE cards SET title=?, content=?, metadata=?, updated_at=? WHERE id=?',
            (title, content, _json.dumps(meta), now, card_id),
        )
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': card_id, 'title': title,
                        'parent_id': meta.get('parent_id'),
                        'links': meta.get('links') or [],
                        'updated_at': now})

    # ── API logs (lightweight passthrough to logger) ───────────────────
    @app.route('/api/logs', methods=['GET'])
    def list_logs():
        limit = min(int(request.args.get('limit', 100)), 500)
        return jsonify({
            'logs': logger.list_logs(limit, 0),
            'log_file': logger.LOG_FILE,
        })

    @app.route('/api/logs/<int:log_id>', methods=['GET'])
    def get_log_detail(log_id):
        logs = logger.list_logs(limit=1)
        conn = logger._conn()
        row = conn.execute('SELECT * FROM api_logs WHERE id=?', (log_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'not found'}), 404
        import json as _j
        d = dict(row)
        d['meta'] = _j.loads(d.pop('meta_json', '{}') or '{}')
        return jsonify(d)

    @app.route('/api/logs', methods=['DELETE'])
    def clear_logs():
        logger.clear(older_than_days=0)
        return jsonify({'ok': True})

    # ── Chat preamble (user-editable first-turn context) ──────────────
    from oh_my_skill.routes.chat import _PREAMBLE_PATH, _get_preamble

    @app.route('/api/chat-preamble', methods=['GET'])
    def get_preamble():
        return jsonify({'content': _get_preamble(), 'path': _PREAMBLE_PATH})

    @app.route('/api/chat-preamble', methods=['PUT'])
    def put_preamble():
        data = request.get_json() or {}
        content = data.get('content', '')
        with open(_PREAMBLE_PATH, 'w') as f:
            f.write(content)
        return jsonify({'ok': True})

    @app.route('/favicon.ico')
    def favicon():
        p = os.path.join(os.path.dirname(__file__), 'templates', 'favicon.svg')
        if os.path.exists(p):
            return send_file(p, mimetype='image/svg+xml')
        return '', 404

    return app


# Module-level app for WSGI servers (gunicorn, etc.)
app = create_app()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '80'))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
