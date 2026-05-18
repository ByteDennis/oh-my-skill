"""Lineage — Topic → (optional) Subtopic → Card outline.

A single global lineage stored in two SQLite tables alongside the existing
`cards` table. Cards are referenced, not copied: deleting a card from the
gallery cascades; removing a card from the lineage (drag to trash) only
detaches that instance.

Data shape:
    lineage_topics(id, title, position)
    lineage_items (id, topic_id, position, kind, label, card_id)
        kind = 'subtopic' → use `label`
        kind = 'card'     → use `card_id`

The "subtopic" a card belongs to is implicit: it's the nearest
kind='subtopic' row above the card in the same topic (or None).
"""
import json
import sqlite3

from flask import Blueprint, jsonify, render_template, request

from oh_my_skill.routes.skillcards import SKILLCARDS_DB

lineage_bp = Blueprint('lineage', __name__)


def _db():
    conn = sqlite3.connect(SKILLCARDS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _init_db():
    conn = _db()
    conn.execute('''CREATE TABLE IF NOT EXISTS lineage_topics (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        title    TEXT NOT NULL,
        position INTEGER NOT NULL,
        card_id  TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS lineage_items (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id  INTEGER NOT NULL REFERENCES lineage_topics(id) ON DELETE CASCADE,
        position  INTEGER NOT NULL,
        kind      TEXT NOT NULL CHECK (kind IN ('subtopic','card')),
        label     TEXT,
        card_id   TEXT REFERENCES cards(id) ON DELETE CASCADE
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_lineage_items_topic_pos '
                 'ON lineage_items(topic_id, position)')
    # Migrate: add card_id to lineage_topics if column doesn't exist yet
    try:
        conn.execute('ALTER TABLE lineage_topics ADD COLUMN card_id TEXT')
    except Exception:
        pass
    conn.commit()
    conn.close()


_init_db()


def _resolve_subtopic_for(conn, topic_id, position, strict=False):
    """Return the label of the nearest kind='subtopic' divider above a row at
    `position`. `strict=True` is for insert/move *targets* (the new row lands
    BEFORE anything currently at `position`, which gets shifted down), so a
    divider sitting AT `position` is below the new row and must be excluded.
    `strict=False` is for resolving the subtopic of an existing row."""
    op = '<' if strict else '<='
    row = conn.execute(
        f"SELECT label FROM lineage_items "
        f"WHERE topic_id=? AND position{op}? AND kind='subtopic' "
        f"ORDER BY position DESC LIMIT 1",
        (topic_id, position),
    ).fetchone()
    return row['label'] if row else None


def _card_already_in_slot(conn, topic_id, position, card_id, exclude_item_id=None):
    """Check whether `card_id` already exists under the same (topic, subtopic)
    that `position` would land in. `exclude_item_id` lets a move skip itself."""
    target_sub = _resolve_subtopic_for(conn, topic_id, position, strict=True)
    # Walk all card rows in this topic and compare their resolved subtopic.
    rows = conn.execute(
        "SELECT id, position, card_id FROM lineage_items "
        "WHERE topic_id=? AND kind='card' AND card_id=?",
        (topic_id, card_id),
    ).fetchall()
    for r in rows:
        if exclude_item_id is not None and r['id'] == exclude_item_id:
            continue
        if _resolve_subtopic_for(conn, topic_id, r['position'], strict=False) == target_sub:
            return True
    return False


def _shift_positions(conn, topic_id, start_pos):
    """Make room at `start_pos` by shifting everything at or after it down by 1."""
    conn.execute(
        "UPDATE lineage_items SET position = position + 1 "
        "WHERE topic_id=? AND position >= ?",
        (topic_id, start_pos),
    )


def _normalize_positions(conn, topic_id):
    """Renumber positions in `topic_id` to 0..N-1 in their current order."""
    rows = conn.execute(
        "SELECT id FROM lineage_items WHERE topic_id=? ORDER BY position, id",
        (topic_id,),
    ).fetchall()
    for i, r in enumerate(rows):
        conn.execute("UPDATE lineage_items SET position=? WHERE id=?", (i, r['id']))


# ── State ────────────────────────────────────────────────────────────────

@lineage_bp.route('/lineage')
def page():
    return render_template('lineage.html')


@lineage_bp.route('/lineage/linear')
def linear_view():
    conn = _db()
    topics = [dict(r) for r in conn.execute(
        "SELECT id, title, position FROM lineage_topics ORDER BY position, id"
    ).fetchall()]
    rows = conn.execute("""
        SELECT li.id, li.topic_id, li.position, li.kind, li.label,
               li.card_id, c.title AS card_title, c.tags AS card_tags
        FROM lineage_items li
        LEFT JOIN cards c ON c.id = li.card_id
        ORDER BY li.topic_id, li.position
    """).fetchall()
    conn.close()

    by_topic = {t['id']: [] for t in topics}
    for r in rows:
        item = dict(r)
        try:
            item['card_tags'] = json.loads(item['card_tags'] or '[]')
        except Exception:
            item['card_tags'] = []
        by_topic.setdefault(item['topic_id'], []).append(item)

    for t in topics:
        groups = []
        current_sub, current_cards = None, []
        for item in by_topic.get(t['id'], []):
            if item['kind'] == 'subtopic':
                if current_cards or current_sub is not None:
                    groups.append({'subtopic': current_sub, 'cards': current_cards})
                current_sub, current_cards = item['label'], []
            else:
                current_cards.append(item)
        groups.append({'subtopic': current_sub, 'cards': current_cards})
        t['groups'] = groups

    return render_template('lineage_linear.html', topics=topics)



@lineage_bp.route('/lineage/api/state', methods=['GET'])
def get_state():
    conn = _db()
    topics = [dict(r) for r in conn.execute(
        "SELECT id, title, position, card_id FROM lineage_topics ORDER BY position, id"
    ).fetchall()]
    items = [dict(r) for r in conn.execute(
        "SELECT id, topic_id, position, kind, label, card_id "
        "FROM lineage_items ORDER BY topic_id, position"
    ).fetchall()]
    conn.close()
    by_topic = {t['id']: [] for t in topics}
    for it in items:
        by_topic.setdefault(it['topic_id'], []).append(it)
    for t in topics:
        t['items'] = by_topic.get(t['id'], [])
    return jsonify({'topics': topics})


# ── Topics ───────────────────────────────────────────────────────────────

@lineage_bp.route('/lineage/api/topics', methods=['POST'])
def create_topic():
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    card_id = (data.get('card_id') or '').strip() or None
    conn = _db()
    row = conn.execute("SELECT COALESCE(MAX(position), -1) AS m FROM lineage_topics").fetchone()
    pos = (row['m'] if row else -1) + 1
    cur = conn.execute(
        "INSERT INTO lineage_topics (title, position, card_id) VALUES (?, ?, ?)",
        (title, pos, card_id),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': tid, 'title': title, 'position': pos, 'card_id': card_id}), 201


@lineage_bp.route('/lineage/api/topics/<int:topic_id>', methods=['PATCH'])
def update_topic(topic_id):
    data = request.get_json() or {}
    conn = _db()
    if not conn.execute("SELECT 1 FROM lineage_topics WHERE id=?", (topic_id,)).fetchone():
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if 'title' in data:
        title = (data.get('title') or '').strip()
        if not title:
            conn.close()
            return jsonify({'error': 'title required'}), 400
        conn.execute("UPDATE lineage_topics SET title=? WHERE id=?", (title, topic_id))
    if 'position' in data:
        try:
            new_pos = int(data['position'])
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'error': 'bad position'}), 400
        # Renumber: pull the topic out, then insert at new_pos among the rest.
        topics = [dict(r) for r in conn.execute(
            "SELECT id FROM lineage_topics ORDER BY position, id"
        ).fetchall()]
        topics = [t for t in topics if t['id'] != topic_id]
        new_pos = max(0, min(new_pos, len(topics)))
        topics.insert(new_pos, {'id': topic_id})
        for i, t in enumerate(topics):
            conn.execute("UPDATE lineage_topics SET position=? WHERE id=?", (i, t['id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@lineage_bp.route('/lineage/api/topics/<int:topic_id>', methods=['DELETE'])
def delete_topic(topic_id):
    force = request.args.get('force', '').lower() in ('1', 'true', 'yes')
    conn = _db()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM lineage_items WHERE topic_id=?", (topic_id,)
    ).fetchone()['n']
    if count and not force:
        conn.close()
        return jsonify({'error': 'topic not empty', 'item_count': count}), 409
    conn.execute("DELETE FROM lineage_topics WHERE id=?", (topic_id,))
    # CASCADE drops the items.
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'removed_items': count})


# ── Items (cards & subtopic dividers) ────────────────────────────────────

@lineage_bp.route('/lineage/api/items', methods=['POST'])
def create_item():
    data = request.get_json() or {}
    try:
        topic_id = int(data.get('topic_id'))
        position = int(data.get('position'))
    except (TypeError, ValueError):
        return jsonify({'error': 'topic_id and position required'}), 400
    kind = data.get('kind')
    if kind not in ('subtopic', 'card'):
        return jsonify({'error': "kind must be 'subtopic' or 'card'"}), 400
    label = (data.get('label') or '').strip() or None
    card_id = (data.get('card_id') or '').strip() or None

    conn = _db()
    if not conn.execute("SELECT 1 FROM lineage_topics WHERE id=?", (topic_id,)).fetchone():
        conn.close()
        return jsonify({'error': 'topic not found'}), 404

    n_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM lineage_items WHERE topic_id=?", (topic_id,)
    ).fetchone()['n']
    position = max(0, min(position, n_rows))

    if kind == 'card':
        if not card_id:
            conn.close()
            return jsonify({'error': 'card_id required'}), 400
        if not conn.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone():
            conn.close()
            return jsonify({'error': 'card not found'}), 404
        if _card_already_in_slot(conn, topic_id, position, card_id):
            conn.close()
            return jsonify({'error': 'duplicate', 'message':
                            'card is already in this subtopic'}), 409
    else:
        if not label:
            label = 'Subtopic'

    _shift_positions(conn, topic_id, position)
    cur = conn.execute(
        "INSERT INTO lineage_items (topic_id, position, kind, label, card_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (topic_id, position, kind, label, card_id),
    )
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': iid, 'topic_id': topic_id, 'position': position,
                    'kind': kind, 'label': label, 'card_id': card_id}), 201


@lineage_bp.route('/lineage/api/items/<int:item_id>', methods=['PATCH'])
def update_item(item_id):
    data = request.get_json() or {}
    conn = _db()
    row = conn.execute(
        "SELECT * FROM lineage_items WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    item = dict(row)

    # Rename a subtopic.
    if 'label' in data and item['kind'] == 'subtopic':
        new_label = (data.get('label') or '').strip()
        if not new_label:
            conn.close()
            return jsonify({'error': 'label required'}), 400
        conn.execute("UPDATE lineage_items SET label=? WHERE id=?", (new_label, item_id))

    # Move (topic_id and/or position).
    if 'topic_id' in data or 'position' in data:
        try:
            new_topic = int(data.get('topic_id', item['topic_id']))
            new_pos_req = int(data.get('position', item['position']))
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'error': 'bad topic_id/position'}), 400
        if not conn.execute("SELECT 1 FROM lineage_topics WHERE id=?", (new_topic,)).fetchone():
            conn.close()
            return jsonify({'error': 'target topic not found'}), 404

        # Remove from old slot and renumber.
        conn.execute("DELETE FROM lineage_items WHERE id=?", (item_id,))
        _normalize_positions(conn, item['topic_id'])

        n_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM lineage_items WHERE topic_id=?", (new_topic,)
        ).fetchone()['n']
        new_pos = max(0, min(new_pos_req, n_rows))

        # Re-validate uniqueness for cards crossing boundaries.
        if item['kind'] == 'card' and _card_already_in_slot(
            conn, new_topic, new_pos, item['card_id'], exclude_item_id=None
        ):
            # Put it back so the user doesn't lose state.
            _shift_positions(conn, item['topic_id'], item['position'])
            conn.execute(
                "INSERT INTO lineage_items (id, topic_id, position, kind, label, card_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, item['topic_id'], item['position'], item['kind'],
                 item['label'], item['card_id']),
            )
            conn.commit()
            conn.close()
            return jsonify({'error': 'duplicate', 'message':
                            'card is already in this subtopic'}), 409

        _shift_positions(conn, new_topic, new_pos)
        conn.execute(
            "INSERT INTO lineage_items (id, topic_id, position, kind, label, card_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, new_topic, new_pos, item['kind'], item['label'], item['card_id']),
        )

    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@lineage_bp.route('/lineage/api/items/<int:item_id>', methods=['DELETE'])
def delete_item(item_id):
    conn = _db()
    row = conn.execute("SELECT topic_id FROM lineage_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    topic_id = row['topic_id']
    conn.execute("DELETE FROM lineage_items WHERE id=?", (item_id,))
    _normalize_positions(conn, topic_id)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Candidates (L0) ─────────────────────────────────────────────────────

@lineage_bp.route('/lineage/api/candidates', methods=['GET'])
def list_candidates():
    q = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    unused = request.args.get('unused', '').lower() in ('1', 'true', 'yes')

    conn = _db()
    rows = conn.execute('SELECT * FROM cards ORDER BY updated_at DESC').fetchall()
    used = set()
    if unused:
        used = {r['card_id'] for r in conn.execute(
            "SELECT DISTINCT card_id FROM lineage_items WHERE kind='card' AND card_id IS NOT NULL"
        ).fetchall()}
    conn.close()

    out = []
    for r in rows:
        c = dict(r)
        try:
            c['tags'] = json.loads(c.get('tags') or '[]')
        except (json.JSONDecodeError, TypeError):
            c['tags'] = []
        try:
            c['metadata'] = json.loads(c.get('metadata') or '{}')
        except (json.JSONDecodeError, TypeError):
            c['metadata'] = {}
        if q and q not in c['title'].lower() and q not in (c['content'] or '').lower() \
                and not any(q in t.lower() for t in c['tags']):
            continue
        if tag and tag not in [t.lower() for t in c['tags']]:
            continue
        if unused and c['id'] in used:
            continue
        out.append(c)
    return jsonify(out)
