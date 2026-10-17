"""The standalone Talker compiler and the demo send identical model inputs."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest

from backend.harness.context import ContextSelectionPolicy, ContextTurn
from backend.harness.contracts import PreparedRun, RunBudget, RunIdentity
from backend.harness import talker
from backend.persona import PERSONAS


NOW = '2026-10-17T12:00:00+00:00'


def identity(turn_id='current'):
    return RunIdentity(run_id='run', request_id='request', turn_id=turn_id, context_revision=7)


def minimal_snapshot():
    return {'vehicle': {}, 'background_tasks_reference_only': [],
            'speaker': {'seat': 'driver', 'display_name': '主驾'}}


class CapturingModel:
    def __init__(self):
        self.requests = []

    async def stream(self, messages, tools):
        self.requests.append({'messages': deepcopy(messages), 'tools': deepcopy(tools)})
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': '离线捕获'}}


@pytest.mark.parametrize('persona_id', list(PERSONAS))
@pytest.mark.parametrize('history_kind', ['ordinary', 'interrupted', 'pending_tools'])
async def test_prepared_run_equals_the_actual_actor_model_request(monkeypatch, persona_id, history_kind):
    from backend import session
    from backend.catalog import initial_vehicle_state
    from backend.cockpit import initial_cockpit_state
    from backend.environment_context import environment_context
    from backend.persona import PERSONAS, style_prompt
    from backend.speaker import speaker_context

    def forbidden_client(*args, **kwargs):
        raise AssertionError('Input compilation must not start clients or open a database')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden_client)
    monkeypatch.setattr(httpx, 'Client', forbidden_client)
    monkeypatch.setattr(sqlite3, 'connect', forbidden_client)
    monkeypatch.setattr(session, 'utcnow', lambda: NOW)

    actor = object.__new__(session.SessionActor)
    actor.id = 'input-equivalence'
    actor.persona = PERSONAS[persona_id].model_copy(deep=True)
    actor.speaker_seat = 'driver'
    actor.vehicle = initial_vehicle_state()
    actor.vehicle['_meta'].update(location='上海', location_country='CN', timezone='Asia/Shanghai')
    actor.recent_places = [{'name': '已查询地点', 'place_id': 'place-1'}]
    actor.cockpit = None
    actor.cockpit_state = initial_cockpit_state()
    actor.media_choices = {'known': {'content_id': 'known', 'title': '已查询媒体'}}
    actor.tasks = {str(index): {'id': str(index), 'goal': f'目标{index}', 'status': 'completed',
        'created_at': f'2026-10-17T00:00:{index:02d}+00:00', 'revision': index + 1,
        'result': {'summary': '完整资料', 'spoken_summary': f'结论{index}'}} for index in range(10)}
    actor.turns = []
    for index in range(6):
        turn = session.Turn(f'prior-{index}', f'用户历史{index}', actor.persona, 1)
        turn.speaker_seat = 'rear_left'
        turn.messages = [{'role': 'user', 'content': turn.text}, {'role': 'assistant', 'content': f'回答{index}'}]
        if index == 5:
            turn.proactive = True
            turn.messages = [{'role': 'system', 'content': '任务归因数据'}, {'role': 'assistant', 'content': '后台已完成的结论'}]
        if index == 4 and history_kind == 'interrupted':
            turn.playback_id = 'playback'
            turn.audio_cancelled.set()
            turn.played_text = '实际听到部分'
        if index == 4 and history_kind == 'pending_tools':
            call = {'id': 'pending', 'type': 'function', 'function': {'name': 'manage_task', 'arguments': '{"action":"get"}'}}
            turn.messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
            turn.group = [call]
        actor.turns.append(turn)
    current = session.Turn('current', '把主驾车窗关上', actor.persona, 1)
    current.speaker_seat = 'driver'
    current.messages = [{'role': 'user', 'content': current.text}]
    current.loaded = ['lights', 'seats', 'mirrors']
    actor.turns.append(current)
    actor.current = current
    actor.ark = CapturingModel()
    actor.trace = lambda *args, **kwargs: None
    actor.spawn = asyncio.create_task

    async def ignore_post(event):
        pass

    actor.post = ignore_post
    state = {
        'vehicle': actor.vehicle, 'background_tasks_reference_only': actor.task_list(),
        'speaker': speaker_context(current.speaker_seat), 'recent_places': actor.recent_places,
        'cockpit': actor.cockpit_state, 'media_choices': list(actor.media_choices.values()),
        'cockpit_capabilities': {'configured': False}, 'tool_capabilities': actor.tool_capabilities(),
        **environment_context(actor.vehicle, NOW),
        'capabilities': {'gps': False, 'browser_geolocation': False, 'task_priority_change': False},
    }
    records = [ContextTurn(turn.id, turn.messages, proactive=turn.proactive, speaker_seat=turn.speaker_seat,
                           pending_tool_group=bool(turn.group and len(turn.group_results) < len(turn.group)),
                           interrupted_playback=bool(turn.playback_id and turn.audio_cancelled.is_set()),
                           heard_text=turn.played_text) for turn in actor.turns]
    prepared = talker.prepare_talker_run(identity(), current_turn=records[-1], turns=records,
        snapshot=state, persona_prompt=style_prompt(actor.persona), release_id='test-release')
    actor.launch_model(current)
    await current.model_task
    assert actor.ark.requests == [{'messages': prepared.messages, 'tools': prepared.tools}]
    assert len(prepared.tools) == 26
    assert prepared.messages[2:2 + len(talker.TALKER_EXAMPLES)] == talker.TALKER_EXAMPLES
    assert prepared.model_options == {'role': 'talker'}


def test_tools_are_fixed_and_each_compilation_owns_its_input():
    current = ContextTurn('current', [{'role': 'user', 'content': '当前输入'}])
    state = minimal_snapshot()
    budget = RunBudget(max_model_calls=3, max_tool_calls=10)
    original_identity = identity()
    first = talker.prepare_talker_run(original_identity, current_turn=current, turns=[current], snapshot=state,
                                     persona_prompt='明确的人设配置', release_id='test-release', budget=budget)
    original = first.model_dump()
    first.messages[2]['content'] = 'only this run'
    first.tools[0]['function']['description'] = 'only this run'
    first.identity.context_revision = 99
    first.budget.max_model_calls = 1
    state['tool_capabilities'] = [{'name': f'provider_{index}'} for index in range(50)]
    second = talker.prepare_talker_run(original_identity, current_turn=current, turns=[current], snapshot=state,
                                      persona_prompt='明确的人设配置', release_id='test-release', budget=budget)
    assert second.tools == original['tools'] == talker.talker_tools()
    assert second.messages[2] == original['messages'][2]
    assert original_identity.context_revision == 7 and budget.max_model_calls == 3
    assert PreparedRun.model_validate_json(second.model_dump_json()) == second


def test_policy_is_opt_in_and_run_identity_must_match():
    current = ContextTurn('current', [{'role': 'user', 'content': '当前输入'}])
    older = ContextTurn('older', [{'role': 'user', 'content': '旧的用户约束'}])
    notices = [ContextTurn(f'notice-{index}', [{'role': 'assistant', 'content': f'通知{index}'}], proactive=True) for index in range(5)]
    options = dict(current_turn=current, turns=[older, *notices, current], snapshot=minimal_snapshot(),
                   persona_prompt='明确的人设配置', release_id='test-release')
    default = talker.prepare_talker_run(identity(), **options)
    preferred = talker.prepare_talker_run(identity(), policy=ContextSelectionPolicy(prefer_user_turns=True), **options)
    assert older.messages[0] not in default.messages
    assert older.messages[0] in preferred.messages
    assert default.tools == preferred.tools
    with pytest.raises(ValueError) as mismatch:
        talker.prepare_talker_run(identity('wrong-turn'), **options)
    assert str(mismatch.value) == 'Run identity must match the current context turn'
    with pytest.raises(ValueError):
        talker.prepare_talker_run(identity(), **{**options, 'release_id': ''})


def test_standalone_import_does_not_load_actor_or_start_clients():
    script = '''
import httpx, sqlite3, sys
def forbidden(*args, **kwargs):
    raise AssertionError('Unexpected client or database construction')
httpx.AsyncClient = forbidden
httpx.Client = forbidden
sqlite3.connect = forbidden
from backend.harness.talker import prepare_talker_run, talker_tools
assert len(talker_tools()) == 26
assert 'backend.session' not in sys.modules
assert 'backend.db' not in sys.modules
assert 'backend.asr' not in sys.modules
assert 'backend.tts' not in sys.modules
print('standalone import: no actor, database, audio or clients')
'''
    completed = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, check=True)
    assert 'standalone import:' in completed.stdout
