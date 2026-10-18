"""The standalone Talker compiler is the production model request boundary."""
from tests.support.paths import REPO_ROOT
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest

from super_xd.nlp.prompts.context import ContextSelectionPolicy, ContextTurn
from super_xd.nlp.contracts.run import PreparedRun, RunBudget, RunIdentity
import super_xd.nlp.orchestration.talker as talker
from super_xd.nlp.prompts.persona import PERSONAS
from super_xd.nlp.prompts.catalog import TOOLS


NOW = '2026-10-17T12:00:00+00:00'


def identity(turn_id='current'):
    return RunIdentity(run_id='run', request_id='request', turn_id=turn_id, context_revision=7)


def minimal_snapshot():
    return {'vehicle': {}, 'background_tasks_reference_only': [],
            'speaker': {'seat': 'driver', 'display_name': '主驾'}}


class CapturingModel:
    def __init__(self):
        self.requests = []

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        self.requests.append({'messages': deepcopy(messages), 'tools': deepcopy(tools)})
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': '离线捕获'}}


@pytest.mark.parametrize('persona_id', list(PERSONAS))
@pytest.mark.parametrize('history_kind', ['ordinary', 'interrupted', 'pending_tools'])
async def test_compiled_run_equals_the_actual_production_model_request(monkeypatch, persona_id, history_kind):
    from scripts.validate_structured_prompts import import_runtime, capture_start_request
    runtime = import_runtime()

    def forbidden_client(*args, **kwargs):
        raise AssertionError('Input compilation must not start clients or open a database')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden_client)
    monkeypatch.setattr(httpx, 'Client', forbidden_client)
    monkeypatch.setattr(sqlite3, 'connect', forbidden_client)
    history = [{'user': f'用户历史{index}', 'assistant': f'回答{index}'} for index in range(6)]
    if history_kind == 'interrupted':
        history[-1].update(interrupted_playback=True, heard_text='实际听到部分', playback_status='partial')
    if history_kind == 'pending_tools':
        history[-1]['messages'] = [
            {'role': 'user', 'content': '继续查询'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'pending', 'type': 'function',
                'function': {'name': 'manage_task', 'arguments': '{"action":"get"}'}}]},
        ]
    request, bindings = capture_start_request(runtime, {
        'id': 'current', 'text': '把主驾车窗关上', 'role': 'talker', 'persona': persona_id,
        'history': history, 'vehicle_overrides': {'_meta.location': '上海', '_meta.timezone': 'Asia/Shanghai'}}, NOW)
    model = CapturingModel()
    service = runtime.gateway.NLPService(model, model_policy_id='offline-model', release_id='test-release',
        semantic_policy=runtime.policy.ProductTalkerPolicy(bindings,
            enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management']))
    prepared = service.compile_start(request)
    events = [event async for event in service.start(request).events()]
    assert events[-1].type == 'run.completed'
    assert model.requests == [{'messages': prepared.messages, 'tools': prepared.tools}]
    assert len(prepared.tools) == 26
    assert prepared.messages[2:2 + len(talker.TALKER_EXAMPLES)] == talker.TALKER_EXAMPLES
    assert prepared.model_options == {'role': 'talker'}
    if history_kind == 'interrupted':
        assert {'role': 'assistant', 'content': '回答5'} in prepared.messages
    if history_kind == 'pending_tools':
        assert not any(call['id'] == 'pending' for message in prepared.messages for call in message.get('tool_calls', []))


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


def test_all_cold_semantics_are_cached_without_expanding_or_granting_tools(monkeypatch):
    names = {tool['function']['name'] for tool in talker.talker_tools()}
    catalog = {name: description for group in talker.COLD_CAPABILITY_CATALOG.values()
               for name, description in group.items()}
    assert len(catalog) == 85
    assert catalog == {name: spec.description for name, spec in TOOLS.items() if name not in names}
    encoded = json.dumps(talker.COLD_CAPABILITY_CATALOG, ensure_ascii=False, separators=(',', ':'))
    assert encoded in talker.CORE_PROMPT

    def forbidden_read(*args, **kwargs):
        pytest.fail('Compiled role semantics must not read files on a request')

    monkeypatch.setattr(Path, 'read_text', forbidden_read)
    original = minimal_snapshot()
    original['capabilities'] = {name: False for name in catalog}
    original['tool_capabilities'] = []
    before = deepcopy(original)
    for text in ('车况查询', '不同的控制请求'):
        current = ContextTurn('current', [{'role': 'user', 'content': text}])
        result = talker.prepare_talker_run(identity(), current_turn=current, turns=[], snapshot=original,
            persona_prompt='人设', release_id='test-release')
        assert result.messages[0]['content'] == talker.CORE_PROMPT
        assert {tool['function']['name'] for tool in result.tools} == names
        dynamic = next(message['content'] for message in result.messages
                       if message['content'].startswith('当前会话快照'))
        state = json.loads(dynamic[dynamic.index('{'):])
        assert all(group['available_count'] == 0 for group in state['vehicle_skill_capabilities'].values())
        assert {name for group in state['vehicle_skill_capabilities'].values()
                for name in group['unavailable']} == catalog.keys()
    assert original == before


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
from super_xd.nlp.orchestration.talker import prepare_talker_run, talker_tools
assert len(talker_tools()) == 26
assert 'super_xd.demo.session.actor' not in sys.modules
assert 'super_xd.demo.tasks.database' not in sys.modules
assert 'super_xd.demo.audio.volcengine_asr' not in sys.modules
assert 'super_xd.demo.audio.volcengine_tts' not in sys.modules
print('standalone import: no actor, database, audio or clients')
'''
    completed = subprocess.run([sys.executable, '-c', script], cwd=REPO_ROOT,
                               capture_output=True, text=True, check=True)
    assert 'standalone import:' in completed.stdout
