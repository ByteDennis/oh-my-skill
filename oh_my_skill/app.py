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
        """Whether Claude is reachable. UI uses this to gracefully degrade
        Extract / Chat features when Claude isn't installed/configured."""
        import glob as _glob, shutil as _shutil
        has_token = bool(get_setting('global', 'claude_code_oauth_token'))
        bin_path = os.environ.get('CLAUDE_BIN') or _shutil.which('claude')
        if not bin_path:
            cands = sorted(_glob.glob('/opt/claude/versions/*'), key=os.path.getmtime)
            if cands:
                bin_path = cands[-1]
        return jsonify({
            'configured': bool(has_token and bin_path),
            'has_token': has_token,
            'has_binary': bool(bin_path),
            'binary_path': bin_path or '',
        })

    @app.route('/api/themes')
    def themes_list():
        return jsonify({'colors': list_colors(), 'fonts': list_fonts()})

    # ── Settings (per-service) ─────────────────────────────────────────
    @app.route('/api/settings', methods=['GET'])
    def settings_get():
        cur = get_all('skill')
        return jsonify({
            'color_theme': cur.get('color_theme', 'classic-blue'),
            'font_theme': cur.get('font_theme', 'modern'),
            'chat_retention_days': int(cur.get('chat_retention_days') or 7),
            'claude_token_set': bool(get_setting('global', 'claude_code_oauth_token')),
        })

    @app.route('/api/settings', methods=['PUT'])
    def settings_put():
        data = request.get_json() or {}
        for k in ('color_theme', 'font_theme'):
            if k in data:
                put_setting('skill', k, data[k] or '')
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
        return jsonify({'ok': True})

    # ── Helper for `oms-save` — the CLI in the chat workspace calls this
    @app.route('/api/cards/<card_id>/sync-from-disk', methods=['POST'])
    def card_sync_from_disk(card_id):
        """Read ./card.md from a project workspace and update the card in DB."""
        skillcards_db = os.environ.get('SKILLCARDS_DB',
                                       os.path.join(_DATA_DIR, 'skillcards.db'))
        data = request.get_json() or {}
        cwd = data.get('cwd', '')
        md_path = os.path.join(cwd, 'card.md') if cwd else ''
        if not md_path or not os.path.exists(md_path):
            return jsonify({'error': 'card.md not found'}), 404
        raw = open(md_path).read()
        lines = raw.splitlines()
        title = ''
        content_start = 0
        for i, ln in enumerate(lines):
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
        cur = conn.execute('SELECT id FROM cards WHERE id=?', (card_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({'error': 'card not found in DB'}), 404
        conn.execute(
            'UPDATE cards SET title=?, content=?, updated_at=? WHERE id=?',
            (title, content, now, card_id),
        )
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': card_id, 'title': title, 'updated_at': now})

    # ── API logs (lightweight passthrough to logger) ───────────────────
    @app.route('/api/logs', methods=['GET'])
    def list_logs():
        limit = min(int(request.args.get('limit', 100)), 500)
        return jsonify({
            'logs': logger.list_logs(limit, 0),
            'log_file': logger.LOG_FILE,
        })

    @app.route('/api/logs', methods=['DELETE'])
    def clear_logs():
        logger.clear(older_than_days=0)
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
