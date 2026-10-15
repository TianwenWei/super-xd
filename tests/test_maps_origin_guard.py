"""Location-free airport navigation cannot acquire an invented origin via POI."""
import asyncio
from copy import deepcopy

import pytest

from backend.conditions import validate_required_origin
from backend.db import Database
from backend.session import SessionActor
from test_session import FakeArk, call, wait_for
from vehicle_fakes import FakeVehiclePlanner


PLACE = {'place_id': 'actual-airport-id', 'name': '巴黎戴高乐机场', 'address': '法国巴黎',
         'location': {'latitude': 49.0097, 'longitude': 2.5479}}


class FakeMaps:
    def __init__(self):
        self.requests = []

    async def search_places(self, **arguments):
        self.requests.append(deepcopy(arguments))
        return {'ok': True, 'query': arguments['query'], 'places': [deepcopy(PLACE)]}


@pytest.fixture
async def make_maps_actor(tmp_path):
    actors = []

    async def make(responses):
        maps = FakeMaps()
        actor = SessionActor(f'origin-{len(actors)}', Database(tmp_path / f'{len(actors)}.sqlite3'),
                             FakeArk(responses), maps=maps, vehicle_planner=FakeVehiclePlanner())
        actors.append(actor)
        queue = await actor.attach()
        await queue.get()
        return actor, queue, maps

    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


@pytest.mark.parametrize('tool', ['search_places', 'start_poi_navigation'])
def test_guard_ignores_model_invented_location_in_maps_arguments(tool):
    result = validate_required_origin('导航去最近的机场。', tool, {'query': '巴黎 最近机场', 'place_id': 'Newark'})
    assert result['kind'] == 'missing_required_origin' and result['executed'] is False
    assert result['spoken_reason'] == '你现在在哪个城市或地址？'
    assert validate_required_origin('导航去最近的机场。', tool, {}, ['我在巴黎。']) is None
    assert validate_required_origin('帮我找巴黎的机场。', tool, {}) is None


@pytest.mark.parametrize('tool,arguments', [
    ('search_places', {'query': '最近机场 Newark'}),
    ('start_poi_navigation', {'place_id': PLACE['place_id']}),
])
async def test_no_origin_stops_network_and_cached_poi_navigation_before_dispatch(make_maps_actor, tool, arguments):
    actor, queue, maps = await make_maps_actor([
        {'tool_calls': [call(tool, arguments, 'bad-origin')]},
        {'content': '你现在在哪个城市或地址？'},
    ])
    actor.places[PLACE['place_id']] = deepcopy(PLACE)
    before = deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '导航去最近的机场。'})
    done, events = await wait_for(queue, 'stream.done')
    refusal = actor.current.tool_results['bad-origin']
    assert refusal['authorization_guard']['kind'] == 'missing_required_origin'
    assert refusal['executed'] is False and refusal['retry_model'] is True
    assert maps.requests == [] and actor.vehicle == before
    assert not any(e['type'] == 'vehicle.command' for e in events)
    assert done['text'] == '你现在在哪个城市或地址？'


async def test_explicit_paris_poi_query_stays_read_only_and_returns_actual_place(make_maps_actor):
    actor, queue, maps = await make_maps_actor([
        {'tool_calls': [call('search_places', {'query': '巴黎 法国 机场'}, 'paris')]},
        {'content': '找到了巴黎戴高乐机场。'},
    ])
    await actor.post({'type': 'input', 'text': '帮我找巴黎的机场。'})
    _, events = await wait_for(queue, 'stream.done')
    assert maps.requests == [{'query': '巴黎 法国 机场'}]
    assert actor.current.tool_results['paris']['places'][0]['place_id'] == PLACE['place_id']
    assert actor.vehicle['navigation']['destination'] is None
    assert not any(e['type'] == 'vehicle.command' for e in events)


async def test_previous_explicit_user_origin_allows_actual_poi_navigation(make_maps_actor):
    actor, queue, maps = await make_maps_actor([
        {'content': '好的。'},
        {'tool_calls': [call('search_places', {'query': '巴黎 法国 机场'}, 'paris')]},
        {'tool_calls': [call('start_poi_navigation', {'place_id': PLACE['place_id']}, 'navigate')]},
    ])
    await actor.post({'type': 'input', 'text': '我在巴黎。'})
    await wait_for(queue, 'stream.done')
    await actor.post({'type': 'input', 'text': '导航去最近的机场。'})
    _, events = await wait_for(queue, 'stream.done', actor)
    assert maps.requests == [{'query': '巴黎 法国 机场'}]
    assert actor.vehicle['navigation']['destination'] == PLACE['name']
    commands = [e for e in events if e['type'] == 'vehicle.command']
    assert len(commands) == 1 and commands[0]['arguments']['destination'] == PLACE['name']


async def test_intervening_user_context_prevents_reuse_of_old_origin(make_maps_actor):
    actor, queue, maps = await make_maps_actor([
        {'content': '好的。'}, {'content': '明白。'},
        {'tool_calls': [call('search_places', {'query': '巴黎 法国 机场'}, 'stale-origin')]},
        {'content': '你现在在哪个城市或地址？'},
    ])
    for text in ['我在巴黎。', '现在位置不确定。']:
        await actor.post({'type': 'input', 'text': text})
        await wait_for(queue, 'stream.done')
    await actor.post({'type': 'input', 'text': '导航去最近的机场。'})
    _, events = await wait_for(queue, 'stream.done')
    assert maps.requests == []
    assert actor.current.tool_results['stale-origin']['executed'] is False
    assert not any(e['type'] == 'vehicle.command' for e in events)
