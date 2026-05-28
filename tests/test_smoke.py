"""Smoke tests — page renders, themes load, settings round-trip."""


def test_healthz(client):
    assert client.get('/healthz').get_json() == {'ok': True}


def test_home_renders_new_skill_template(client):
    r = client.get('/')
    assert r.status_code == 200
    body = r.data.decode()
    # New template signals (must be the rewrite, not devhub's skillcards.html)
    assert 'oh-my-skill' in body
    assert '/omi/ui/css/omi.css' in body


def test_themes_endpoint(client):
    j = client.get('/api/themes').get_json()
    assert len(j['colors']) == 9
    assert len(j['fonts']) == 7


def test_settings_get(client):
    j = client.get('/api/settings').get_json()
    assert 'color_theme' in j
    assert 'font_theme' in j
    assert 'chat_provider' in j
    assert 'claude_token_set' in j


def test_settings_put_round_trip(client):
    client.put('/api/settings',
               json={'color_theme': 'oxford-burgundy', 'font_theme': 'tech',
                     'chat_provider': 'codex'})
    j = client.get('/api/settings').get_json()
    assert j['color_theme'] == 'oxford-burgundy'
    assert j['font_theme'] == 'tech'
    assert j['chat_provider'] == 'codex'


def test_ai_status_has_chat_and_extract_sections(client):
    j = client.get('/api/ai/status').get_json()
    assert 'chat' in j
    assert 'extract' in j
    assert 'chat_provider' in j


def test_omi_ui_static_served(client):
    r = client.get('/omi/ui/css/omi.css')
    assert r.status_code == 200
    assert b'@oh-my/ui' in r.data
