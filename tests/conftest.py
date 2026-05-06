"""oh-my-skill test fixtures — fully mocked, $0 to run."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_data(monkeypatch, tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    monkeypatch.setenv('OMI_DATA_DIR', str(d))
    monkeypatch.setenv('SETTINGS_DB', str(d / 'oh-my-skill.db'))
    monkeypatch.setenv('SKILLCARDS_DB', str(d / 'skillcards.db'))
    monkeypatch.setenv('OMI_LOG_DB', str(d / 'api_logs.db'))
    monkeypatch.setenv('OMI_LOG_FILE', str(d / 'api.log'))
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'fake-claude')
    # Force-clear cached oh_my_skill modules so paths re-resolve from env
    for mod in list(sys.modules):
        if mod.startswith('oh_my_skill'):
            sys.modules.pop(mod, None)
    return d


@pytest.fixture
def app(tmp_data):
    from oh_my_skill.app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_card(client):
    """Helper: create a card via the real API and return its id."""
    def _create(title='Test card', content='# Test card\n\nbody', tags=None):
        r = client.post('/skill-cards/api/cards', json={
            'title': title, 'content': content, 'tags': tags or [],
        })
        assert r.status_code == 201, r.get_json()
        return r.get_json()['id']
    return _create
