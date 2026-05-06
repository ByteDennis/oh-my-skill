"""CRUD round-trip via devhub's skillcards blueprint (mounted by oh-my-skill)."""


def test_create_list_get_update_delete(client, make_card):
    # Empty
    assert client.get('/skill-cards/api/cards').get_json() == []

    # Create
    cid = make_card('Hello', '# Hello\n\nbody', tags=['demo', 'test'])

    # List
    cards = client.get('/skill-cards/api/cards').get_json()
    assert len(cards) == 1
    assert cards[0]['id'] == cid
    assert cards[0]['tags'] == ['demo', 'test']

    # Update
    r = client.put(f'/skill-cards/api/cards/{cid}',
                   json={'title': 'Renamed', 'content': '# Renamed\n\nv2',
                         'tags': ['demo']})
    assert r.status_code == 200
    assert r.get_json()['title'] == 'Renamed'

    # Confirm via list
    cards = client.get('/skill-cards/api/cards').get_json()
    assert cards[0]['title'] == 'Renamed'
    assert cards[0]['tags'] == ['demo']

    # Delete
    client.delete(f'/skill-cards/api/cards/{cid}')
    assert client.get('/skill-cards/api/cards').get_json() == []


def test_search_filter(client, make_card):
    make_card('Python tips', 'list comprehensions', tags=['python'])
    make_card('Bash tricks', 'parameter expansion', tags=['bash'])
    make_card('Docker note', 'multi-stage builds', tags=['docker'])

    # search by content
    r = client.get('/skill-cards/api/cards?q=parameter')
    titles = [c['title'] for c in r.get_json()]
    assert titles == ['Bash tricks']

    # filter by tag
    r = client.get('/skill-cards/api/cards?tag=python')
    titles = [c['title'] for c in r.get_json()]
    assert titles == ['Python tips']
