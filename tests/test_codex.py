import json


def test_codex_send_streams_claude_style_blocks(tmp_data, monkeypatch):
    from oh_my_skill.shared import codex_chat

    class FakeProc:
        def __init__(self, lines):
            self.stdout = [json.dumps(line) + '\n' for line in lines]
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    lines = [
        {'type': 'thread.started', 'thread_id': 'thread-1'},
        {
            'type': 'item.completed',
            'item': {
                'type': 'reasoning',
                'id': 'reasoning-1',
                'text': 'Inspecting card.md and planning edits',
            },
        },
        {
            'type': 'item.started',
            'item': {
                'type': 'command_execution',
                'id': 'command-1',
                'command': 'sed -n "1,80p" card.md',
                'aggregated_output': '',
                'status': 'in_progress',
            },
        },
        {
            'type': 'item.updated',
            'item': {
                'type': 'command_execution',
                'id': 'command-1',
                'command': 'sed -n "1,80p" card.md',
                'aggregated_output': '# Card\n',
                'status': 'in_progress',
            },
        },
        {
            'type': 'item.completed',
            'item': {
                'type': 'command_execution',
                'id': 'command-1',
                'command': 'sed -n "1,80p" card.md',
                'aggregated_output': '# Card\n',
                'exit_code': 0,
                'status': 'completed',
            },
        },
        {
            'type': 'item.started',
            'item': {
                'type': 'mcp_tool_call',
                'id': 'mcp-1',
                'server': 'cards',
                'tool': 'lookup',
                'arguments': {'id': 'card-1'},
                'status': 'in_progress',
            },
        },
        {
            'type': 'item.completed',
            'item': {
                'type': 'mcp_tool_call',
                'id': 'mcp-1',
                'server': 'cards',
                'tool': 'lookup',
                'arguments': {'id': 'card-1'},
                'result': {'content': [{'type': 'text', 'text': 'found card'}]},
                'status': 'completed',
            },
        },
        {
            'type': 'item.completed',
            'item': {
                'type': 'file_change',
                'id': 'change-1',
                'changes': [{'path': 'card.md', 'kind': 'update'}],
                'status': 'completed',
            },
        },
        {
            'type': 'item.started',
            'item': {
                'type': 'agent_message',
                'id': 'message-1',
                'text': 'Updated',
            },
        },
        {
            'type': 'item.updated',
            'item': {
                'type': 'agent_message',
                'id': 'message-1',
                'text': 'Updated the card',
            },
        },
        {
            'type': 'item.completed',
            'item': {
                'type': 'agent_message',
                'id': 'message-1',
                'text': 'Updated the card outline.',
            },
        },
        {
            'type': 'turn.completed',
            'usage': {'input_tokens': 12, 'output_tokens': 34},
        },
    ]

    monkeypatch.setattr(codex_chat.subprocess, 'Popen', lambda *args, **kwargs: FakeProc(lines))

    chat_id = codex_chat.new_session(str(tmp_data / 'workspace'), card_id='card-1')
    events = list(codex_chat.send(chat_id, 'check the card', mode='explain'))

    decoded = []
    for chunk in events:
        if not chunk.startswith('data: ') or chunk.strip() == 'data: [DONE]':
            continue
        decoded.append(json.loads(chunk[6:]))

    assert any(ev.get('type') == 'block_start' and ev.get('block_type') == 'thinking' for ev in decoded)
    thinking = [ev for ev in decoded if ev.get('type') == 'thinking_delta']
    assert [ev['text'] for ev in thinking] == ['Inspecting card.md and planning edits']

    tool_starts = [ev for ev in decoded if ev.get('type') == 'block_start' and ev.get('block_type') == 'tool_use']
    assert [(ev['name'], ev['id']) for ev in tool_starts] == [
        ('Bash', 'command-1'),
        ('cards.lookup', 'mcp-1'),
        ('Edit', 'change-1'),
    ]

    tool_inputs = [json.loads(ev['partial_json']) for ev in decoded if ev.get('type') == 'tool_input_delta']
    assert tool_inputs == [
        {'command': 'sed -n "1,80p" card.md'},
        {'id': 'card-1'},
        {'file_path': 'card.md', 'changes': [{'path': 'card.md', 'kind': 'update'}]},
    ]

    command_results = [
        ev for ev in decoded
        if ev.get('type') == 'tool_result' and ev.get('tool_use_id') == 'command-1'
    ]
    assert [(ev['content'], ev['pending']) for ev in command_results] == [
        ('# Card\n', True),
        ('# Card\n', False),
    ]
    assert command_results[-1]['is_error'] is False
    mcp_result = next(ev for ev in decoded if ev.get('type') == 'tool_result' and ev.get('tool_use_id') == 'mcp-1')
    assert mcp_result['content'] == 'found card'
    assert mcp_result['pending'] is False

    text_deltas = [ev['text'] for ev in decoded if ev.get('type') == 'text_delta']
    assert text_deltas == ['Updated', ' the card', ' outline.']

    turn_done = next(ev for ev in decoded if ev.get('type') == 'turn_done')
    assert turn_done['provider'] == 'codex'
    assert turn_done['provider_session_id'] == 'thread-1'
    assert turn_done['input_tokens'] == 12
    assert turn_done['output_tokens'] == 34

    session = codex_chat.get_session(chat_id)
    blocks = session['history'][-1]['blocks']
    assert blocks == [
        {'type': 'thinking', 'text': 'Inspecting card.md and planning edits'},
        {'type': 'tool_use', 'name': 'Bash', 'id': 'command-1', 'input': {'command': 'sed -n "1,80p" card.md'}},
        {'type': 'tool_use', 'name': 'cards.lookup', 'id': 'mcp-1', 'input': {'id': 'card-1'}},
        {'type': 'tool_use', 'name': 'Edit', 'id': 'change-1', 'input': {
            'file_path': 'card.md',
            'changes': [{'path': 'card.md', 'kind': 'update'}],
        }},
        {'type': 'text', 'text': 'Updated the card outline.'},
    ]
