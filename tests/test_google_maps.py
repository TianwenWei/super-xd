import asyncio
import copy
from datetime import datetime, timezone
import json

import httpx
import pytest

from backend.google_maps import GoogleMapsClient, MapsError
from backend.db import Database, _trace_data
from backend.session import SessionActor
from test_session import FakeArk, call, wait_for


NOW = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
PLACE = {'id': 'real-place', 'displayName': {'text': '巴黎'}, 'formattedAddress': 'Paris, France',
         'location': {'latitude': 48.85, 'longitude': 2.35},
         'addressComponents': [{'shortText': 'FR', 'types': ['country']}]}


def forecast_day(day):
    part = {'weatherCondition': {'description': {'text': '晴'}}, 'precipitation': {'probability': {'percent': 10}}}
    return {'displayDate': {'year': 2026, 'month': 10, 'day': day},
            'minTemperature': {'unit': 'CELSIUS', 'degrees': day},
            'maxTemperature': {'unit': 'CELSIUS', 'degrees': day + 5},
            'daytimeForecast': part, 'nighttimeForecast': copy.deepcopy(part)}


@pytest.mark.parametrize('target,expected', [('today', 14), ('tomorrow', 15), ('2026-10-15', 15)])
async def test_weather_matches_civil_date_not_first_record(target, expected):
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={'places': [PLACE]} if req.url.host == 'places.googleapis.com' else
                              {'timeZone': {'id': 'Europe/Paris'}, 'forecastDays': [forecast_day(d) for d in (13, 14, 15)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        maps = GoogleMapsClient(http, 'fake-secret', now=lambda: NOW)
        result = await maps.get_weather('巴黎 法国', target)
        assert result['date'] == f'2026-10-{expected}' and result['summary']['min_c'] == expected
        assert result['provider'] == 'Google Weather'
        assert all('fake-secret' not in str(req.url) for req in requests)
        assert requests[0].headers['X-Goog-Api-Key'] == 'fake-secret'


@pytest.mark.parametrize('status,code', [(403, 'permission_denied'), (429, 'upstream_error')])
async def test_permission_failure_does_not_silently_fallback_or_expose_key(status, code):
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={'places': [PLACE]}) if len(requests) == 1 else httpx.Response(status, json={'error': {'message': 'fake-secret'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MapsError) as caught:
            await GoogleMapsClient(http, 'fake-secret').get_weather('巴黎 法国', 'today')
        assert caught.value.code == code and 'fake-secret' not in str(caught.value)
        assert len(requests) == 2


async def test_unsupported_location_falls_back_and_never_sends_google_key_to_open_meteo():
    requests = []
    def handler(req):
        requests.append(req)
        if req.url.host == 'places.googleapis.com':
            return httpx.Response(200, json={'places': [PLACE]})
        if req.url.host == 'weather.googleapis.com':
            return httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})
        assert 'X-Goog-Api-Key' not in req.headers
        return httpx.Response(200, json={'timezone': 'Asia/Shanghai',
            'daily_units': {'temperature_2m_min': '°C', 'temperature_2m_max': '°C'},
            'daily': {'time': ['2026-10-15'], 'temperature_2m_min': [22], 'temperature_2m_max': [29],
                      'weather_code': [1], 'precipitation_probability_max': [10]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GoogleMapsClient(http, 'fake-secret', now=lambda: NOW).get_weather('深圳 中国', 'tomorrow')
        assert result['ok'] and result['provider'] == 'Open-Meteo'
        assert result['fallback_reason'] == 'google_unsupported_location'
        assert result['date'] == '2026-10-15' and len(requests) == 3
        assert result['summary']['condition'] == '大部晴朗'


async def test_city_ambiguity_does_not_choose_or_query_weather():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'places': [PLACE, {**PLACE, 'id': 'other'}]}))) as http:
        result = await GoogleMapsClient(http, 'key').get_weather('Paris', 'today')
        assert result['needs_clarification'] and len(result['candidates']) == 2


@pytest.mark.parametrize('change,code', [('units', 'invalid_response'), ('date', 'date_unavailable')])
async def test_bad_weather_does_not_become_a_success(change, code):
    day = forecast_day(14)
    if change == 'units': day['minTemperature']['unit'] = 'FAHRENHEIT'
    else: day['displayDate']['day'] = 13
    def handler(req):
        return httpx.Response(200, json={'places': [PLACE]} if req.url.host == 'places.googleapis.com' else
            {'timeZone': {'id': 'Europe/Paris'}, 'forecastDays': [day]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MapsError) as error:
            await GoogleMapsClient(http, 'key', now=lambda: NOW).get_weather('巴黎', 'today')
        assert error.value.code == code


POI = {'place_id': 'real-place', 'name': '卢浮宫', 'address': 'Paris', 'location': {'latitude': 48.86, 'longitude': 2.33}}


class FakeMaps:
    def __init__(self):
        self.started = asyncio.Event()
        self.wait = False
        self.cancelled = False
    async def search_places(self, **args):
        self.started.set()
        if self.wait:
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return {'ok': True, 'kind': 'places', 'provider': 'Google Maps', 'places': [copy.deepcopy(POI)]}
    async def get_weather(self, **args):
        return {'ok': True, 'kind': 'weather', 'provider': 'Google Weather', 'date': '2026-10-14', 'summary': {'min_c': 8, 'max_c': 20}}


@pytest.fixture
async def actor_factory(tmp_path):
    actors = []
    async def make(responses):
        maps, ark = FakeMaps(), FakeArk(responses)
        actor = SessionActor(str(len(actors)), Database(tmp_path / f'{len(actors)}.sqlite'), ark, maps=maps)
        actors.append(actor)
        queue = await actor.attach()
        await queue.get()
        return actor, queue, ark, maps
    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


async def test_foreground_weather_continues_same_model_without_task(actor_factory):
    actor, queue, ark, _ = await actor_factory([
        {'tool_calls': [call('get_weather', {'location': '巴黎 法国', 'date': 'today'})]}, {'content': '巴黎今天8到20度。'}])
    await actor.post({'type': 'input', 'text': '巴黎今天天气'})
    done, events = await wait_for(queue, 'stream.done')
    assert done['text'] == '巴黎今天8到20度。' and len(ark.requests) == 2
    assert actor.db.list_tasks(actor.id) == [] and any(e['type'] == 'lookup.result' for e in events)
    assert actor.snapshot()['turns'][0]['lookup_results'][0]['result']['provider'] == 'Google Weather'


async def test_poi_query_then_selection_uses_real_identity_and_receipt(actor_factory):
    actor, q, _, _ = await actor_factory([
        {'tool_calls': [call('search_places', {'query': '巴黎卢浮宫', 'area': '巴黎'})]}, {'content': '找到了卢浮宫。'},
        {'tool_calls': [call('start_poi_navigation', {'place_id': POI['place_id']}, id='navigate')]}])
    await actor.post({'type': 'input', 'text': '查巴黎卢浮宫'})
    await wait_for(q, 'stream.done')
    assert not actor.vehicle['navigation']['active']
    await actor.post({'type': 'input', 'text': '导航去第一个'})
    command, _ = await wait_for(q, 'vehicle.command')
    assert not actor.vehicle['navigation']['active']
    await actor.post({'type': 'vehicle.execute', 'command_id': command['command_id']})
    done, _ = await wait_for(q, 'stream.done')
    assert actor.vehicle['navigation']['destination_place'] == POI
    assert actor.vehicle['navigation']['destination'] == POI['name'] and '模拟导航' in done['text']


async def test_fabricated_poi_id_never_dispatches(actor_factory):
    actor, q, _, _ = await actor_factory([{'tool_calls': [call('start_poi_navigation', {'place_id': 'invented'})]}, {'content': '请先选择地点。'}])
    await actor.post({'type': 'input', 'text': '导航到第一个'})
    _, events = await wait_for(q, 'stream.done')
    assert not actor.vehicle['navigation']['active'] and not any(e['type'] == 'vehicle.command' for e in events)


async def test_same_turn_search_result_is_not_a_user_choice(actor_factory):
    actor, q, ark, _ = await actor_factory([
        {'tool_calls': [call('search_places', {'query': '巴黎卢浮宫', 'area': '巴黎'}, id='search')]},
        {'tool_calls': [call('start_poi_navigation', {'place_id': POI['place_id']}, id='navigate')]},
        {'content': '找到卢浮宫，要导航过去吗？'}])
    await actor.post({'type': 'input', 'text': '导航到巴黎卢浮宫'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert len(ark.requests) == 3 and not any(e['type'] == 'vehicle.command' for e in events)
    assert actor.current.tool_results['navigate']['code'] == 'place_not_confirmed'
    assert done['text'] == '找到卢浮宫，要导航过去吗？'


async def test_spoken_place_ordinals_match_cards_even_if_model_reorders(actor_factory):
    actor, q, _, maps = await actor_factory([
        {'tool_calls': [call('search_places', {'query': '巴黎咖啡馆', 'area': '巴黎'})]},
        {'content': '第一个是乙店，第二个是甲店。'}])
    places = [{**POI, 'place_id': 'a', 'name': '甲店'}, {**POI, 'place_id': 'b', 'name': '乙店'}]
    async def search(**args):
        return {'ok': True, 'kind': 'places', 'places': places, 'provider': 'Google Maps'}
    maps.search_places = search
    await actor.post({'type': 'input', 'text': '找巴黎咖啡馆'})
    done, _ = await wait_for(q, 'stream.done')
    assert '1，甲店、2，乙店' in done['text']
    assert actor.current.messages[-1]['content'] == done['text']
    assert [p['place_id'] for p in actor.recent_places] == ['a', 'b']


async def test_lookup_timeout_returns_error_and_no_background_task(actor_factory):
    actor, q, _, maps = await actor_factory([
        {'tool_calls': [call('search_places', {'query': '巴黎餐厅', 'area': '巴黎'})]}, {'content': '地点查询超时。'}])
    maps.wait, actor.lookup_timeout = True, 0.01
    await actor.post({'type': 'input', 'text': '找巴黎餐厅'})
    _, events = await wait_for(q, 'stream.done')
    result = next(e['result'] for e in events if e['type'] == 'lookup.result')
    assert result['ok'] is False and result['code'] == 'timeout'
    assert actor.db.list_tasks(actor.id) == [] and maps.cancelled


async def test_new_input_cancels_lookup_and_late_result_cannot_register_poi(actor_factory):
    actor, q, _, maps = await actor_factory([{'tool_calls': [call('search_places', {'query': '巴黎', 'area': '巴黎'})]}, {'content': '你好。'}])
    maps.wait = True
    await actor.post({'type': 'input', 'text': '找巴黎餐厅'})
    await asyncio.wait_for(maps.started.wait(), 1)
    old = actor.current
    await actor.post({'type': 'input', 'text': '你好'})
    await wait_for(q, 'stream.done')
    assert maps.cancelled
    actor.accept_lookup(old, old.group[0], {'ok': True, 'places': [POI]})
    assert actor.places == {} and actor.current.output == '你好。'


def test_google_secret_redaction(monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'secret-key')
    assert 'secret-key' not in json.dumps(_trace_data({'X-Goog-Api-Key': 'secret-key', 'message': 'secret-key'}))


async def test_search_area_must_come_from_user_words(actor_factory):
    actor, q, ark, maps = await actor_factory([
        {'tool_calls': [call('search_places', {'query': '最近的机场', 'area': '北京'}, id='guess')]},
        {'content': '你现在在哪个城市或者哪条路附近？'}])
    await actor.post({'type': 'input', 'text': '导航去最近的机场'})
    done, _ = await wait_for(q, 'stream.done')
    assert actor.current.tool_results['guess']['code'] == 'area_not_from_user'
    assert not maps.started.is_set() and len(ark.requests) == 2
    assert done['text'] == '你现在在哪个城市或者哪条路附近？'
