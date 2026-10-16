import json
from copy import deepcopy

import pytest

from backend.db import Database
from backend.session import SessionActor, Turn


class NoModelRequests:
    async def stream(self, messages, tools):
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
    monkeypatch.setattr('backend.session.utcnow', lambda: '2026-10-15T21:59:59+00:00')
    await actor.post({'type': 'vehicle.configure', 'settings': {'location': 'Paris', 'location_country': 'France', 'timezone': 'Europe/Paris'}})
    await actor.queue.join()
    turn = Turn('environment-turn', '你好', actor.persona, 1)
    before = deepcopy(actor.vehicle)
    first = actor.messages_for(turn)
    monkeypatch.setattr('backend.session.utcnow', lambda: '2026-10-15T22:00:01+00:00')
    second = actor.messages_for(turn)
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
    def search(area):
        return {'id': 'nearby', 'function': {'name': 'search_places', 'arguments': json.dumps({'query': '咖啡馆', 'area': area})}}
    name, arguments, error = actor.precheck(turn, search('Lyon'))
    assert name == 'search_places' and arguments['near'] == 'Lyon' and error is None
    assert actor.precheck(turn, search('Madrid'))[2]['code'] == 'area_not_from_user'
    context = actor.task_context()
    assert context['current_location'] == {'name': 'Lyon', 'country': 'France', 'source': 'user_setting'}
    assert context['vehicle']['navigation']['destination'] is None
