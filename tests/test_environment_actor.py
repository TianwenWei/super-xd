import json
from copy import deepcopy

import pytest

from backend.db import Database
from backend.session import SessionActor, Turn
from backend.harness.contracts import RunCheckpoint, ToolRequest
from backend.harness.product_policy import ProductPolicyError, prepare_product_action


class NoModelRequests:
    async def stream(self, messages, tools, **options):
        raise AssertionError('Context construction must not invoke a model')
        yield


@pytest.fixture
async def actor(tmp_path):
    instance = SessionActor('environment-test', Database(tmp_path / 'environment.sqlite3'), NoModelRequests())
    yield instance
    await instance.close()


def snapshot(messages):
    return next(json.loads(message['content'].splitlines()[-1]) for message in messages
                if message['role'] == 'system' and message['content'].startswith('当前会话快照'))


async def test_environment_refreshes_each_request_without_changing_prompt_prefix(actor, monkeypatch):
    monkeypatch.setattr('backend.demo.session_nlp.utcnow', lambda: '2026-10-15T21:59:59+00:00')
    await actor.post({'type': 'vehicle.configure', 'settings': {'location': 'Paris', 'location_country': 'France', 'timezone': 'Europe/Paris'}})
    await actor.queue.join()
    turn = Turn('environment-turn', '你好', actor.persona, 1)
    before = deepcopy(actor.vehicle)
    first_start = actor.nlp_start_request(turn)
    first = actor.nlp.compile_start(first_start).messages
    monkeypatch.setattr('backend.demo.session_nlp.utcnow', lambda: '2026-10-15T22:00:01+00:00')
    second_start = actor.nlp_start_request(Turn('next-environment-turn', '你好', actor.persona, 1))
    second = actor.nlp.compile_start(second_start).messages
    assert actor.nlp.compile_start(first_start).messages == first  # Resumes keep the original clock.
    assert snapshot(first)['current_local'] == '2026-10-15T23:59:59+02:00'
    assert snapshot(second)['current_local'] == '2026-10-16T00:00:01+02:00'
    assert snapshot(second)['current_date'] == '2026-10-16'
    assert snapshot(second)['current_location'] == {'name': 'Paris', 'country': 'France', 'source': 'user_setting'}
    index = next(i for i, m in enumerate(first) if m['role'] == 'system' and m['content'].startswith('当前会话快照'))
    assert first[:index] == second[:index]
    assert actor.vehicle == before


async def test_only_user_configured_location_can_ground_nearby_search(actor):
    await actor.post({'type': 'vehicle.configure', 'settings': {'location': 'Lyon', 'location_country': 'France'}})
    await actor.queue.join()
    turn = Turn('nearby-turn', '帮我找附近的咖啡馆', actor.persona, 1)
    start = actor.nlp_start_request(turn)
    prepared = actor.nlp.compile_start(start)
    checkpoint = RunCheckpoint(prepared=prepared, messages=prepared.messages)
    policy_input = {'start': start.model_dump(), 'checkpoint': checkpoint.model_dump()}
    def search(area):
        return ToolRequest(invocation_id='nearby', tool_call_id='nearby', idempotency_key='nearby',
            name='search_places', arguments={'query': '咖啡馆', 'area': area})
    action = prepare_product_action(policy_input, search('Lyon'))
    assert action['execution_arguments']['near'] == 'Lyon'
    with pytest.raises(ProductPolicyError) as rejected:
        prepare_product_action(policy_input, search('Madrid'))
    assert rejected.value.code == 'area_not_grounded'
    assert start.context.environment.location.model_dump() == {
        'name': 'Lyon', 'country': 'France', 'source': 'user_setting'}
    assert start.context.vehicle.state['navigation']['destination'] is None
