import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from backend.cockpit import CockpitServices, LocalMediaCatalog, TencentMusicGateway, initial_cockpit_state, CockpitError
from backend.cockpit.controller import prepare_media, prepare_navigation, scoped_place, validated_receipt
from backend.db import Database
from backend.session import SessionActor
from backend.services.contracts import ProviderError
from test_session import FakeArk, call, wait_for

ITEMS = [
    {'content_id': 'local:a', 'kind': 'music', 'title': '测试乐曲一', 'source_url': 'https://media.example/a.mp3'},
    {'content_id': 'local:b', 'kind': 'music', 'title': '测试乐曲二', 'source_url': 'https://media.example/b.mp3'},
    {'content_id': 'local:v', 'kind': 'video', 'title': '测试视频', 'source_url': '/api/media/local-v'},
    {'content_id': 'local:r', 'kind': 'radio', 'title': '测试电台', 'source_url': 'https://media.example/radio'},
]
PLACE = {'place_id': 'same-id', 'name': '地点一', 'location': {'latitude': 48.8, 'longitude': 2.3}, 'coordinate_system': 'wgs84'}


async def test_local_catalog_is_explicit_and_never_invents_playable_content():
    catalog = LocalMediaCatalog(ITEMS)
    result = await catalog.search(kind='music', query='测试乐曲')
    assert [item['content_id'] for item in result['items']] == ['local:a', 'local:b']
    assert all('source_url' not in item for item in result['items'])
    assert (await catalog.resolve('local:a'))['source_url'] == ITEMS[0]['source_url']
    with pytest.raises(CockpitError): await catalog.resolve('invented')
    assert (await catalog.search(kind='music', query='没有的内容'))['items'] == []


async def test_tencent_gateway_requires_configuration_and_validates_resolved_identity():
    requests = []
    def request(req):
        requests.append(req)
        return httpx.Response(200, json={'ok': True, 'item': {**ITEMS[0], 'content_id': 'wrong'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        disabled = TencentMusicGateway(client=client)
        with pytest.raises(ProviderError) as error: await disabled.search(kind='music', query='歌')
        assert error.value.code == 'not_configured' and not requests
        gateway = TencentMusicGateway(base_url='https://gateway.example', api_key='private', client=client)
        with pytest.raises(ProviderError): await gateway.resolve('local:a')
        assert requests[0].url.path == '/v1/media/resolve'
        assert requests[0].headers['Authorization'] == 'Bearer private'


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///private/example.mp3', 'https://user:secret@example.com/a', '//example.com/a'])
def test_catalog_rejects_non_playable_or_credential_urls(url):
    with pytest.raises(CockpitError): LocalMediaCatalog([{**ITEMS[0], 'source_url': url}])


async def test_pause_resume_stop_exit_seek_volume_and_queue_commands_are_distinct():
    services = CockpitServices(LocalMediaCatalog(ITEMS))
    choices = {item['content_id']: item for item in (await services.media.search(kind='music', query='测试乐曲'))['items']}
    state = initial_cockpit_state()['media']
    prepared = await prepare_media(services, 'play', {'content_id': 'local:a'}, state, choices)
    assert prepared['arguments']['source_url'] == ITEMS[0]['source_url']
    state = prepared['expected']
    for action,arguments,status in [('pause', {}, 'paused'), ('resume', {}, 'playing'), ('seek', {'position_seconds': 12}, 'playing'),
                                    ('volume', {'volume': 45}, 'playing'), ('next', {}, 'playing'), ('previous', {}, 'playing'),
                                    ('stop', {}, 'stopped'), ('exit', {}, 'closed')]:
        command = await prepare_media(services, action, arguments, state, choices)
        assert command['expected']['status'] == status
        if action == 'next': assert command['expected']['content_id'] == 'local:b'
        if action == 'stop': assert command['expected']['content_id'] == 'local:a'
        if action == 'exit': assert command['expected']['content_id'] is None
        state = command['expected']


async def test_media_queue_is_explicit_device_payload_and_must_be_actually_confirmed():
    from backend.demo.cockpit_device import CockpitDevice
    services = CockpitServices(LocalMediaCatalog(ITEMS))
    choices = {item['content_id']: item for item in (await services.media.search(kind='music', query='测试乐曲'))['items']}
    prepared = await prepare_media(services, 'play', {'content_id': 'local:a'}, initial_cockpit_state()['media'], choices)
    wire = {'command_id': 'play', 'request_id': 'play', 'session_id': 'session', 'turn_id': 'turn',
            'target_device': 'player', 'kind': 'control', 'domain': 'media', 'action': 'play',
            'arguments': prepared['arguments'], 'expected_state_version': 0}
    prepared['wire'] = wire
    device = CockpitDevice()
    receipt = device.execute(wire)
    actual = validated_receipt(prepared, receipt)
    assert [item['content_id'] for item in actual['queue']] == ['local:a', 'local:b']
    assert actual['queue'] == device.state['media']['queue'] == wire['arguments']['queue']
    next_track = await prepare_media(services, 'next', {}, actual, {})
    assert next_track['arguments']['content_id'] == 'local:b'
    for queue in ([], [choices['local:b']]):
        with pytest.raises(CockpitError, match='requested queue'):
            validated_receipt(prepared, {**receipt, 'state': {**receipt['state'], 'queue': queue}})
    partial = {key: value for key, value in receipt['state'].items() if key not in {'title', 'queue'}}
    pause = {'expected': {**actual, 'status': 'paused'}, 'arguments': {},
             'wire': {'domain': 'media', 'expected_state_version': 1}}
    observed = validated_receipt(pause, {'state_version': 2, 'state': {**partial, 'status': 'paused'}})
    assert observed['queue'] == [] and 'title' not in observed


def test_provider_scoped_place_identity_and_receipt_itinerary_binding():
    google, amap = scoped_place(PLACE, 'google'), scoped_place(PLACE, 'amap')
    assert google['place_ref'] != amap['place_ref'] and google['place_id'] == amap['place_id']
    state = initial_cockpit_state()['navigation']
    command = prepare_navigation('set_origin', {'place_ref': google['place_ref']}, state, {google['place_ref']: google})
    state = command['expected']
    command = prepare_navigation('start', {'place_ref': amap['place_ref']}, state, {amap['place_ref']: amap})
    command['wire'] = {'domain': 'navigation', 'expected_state_version': 0}
    actual = validated_receipt(command, {'state_version': 1, 'state': command['expected']})
    assert actual['origin'] == google and actual['destination'] == amap and actual['mode'] == 'simulated'
    with pytest.raises(CockpitError): validated_receipt(command, {'state': {**command['expected'], 'destination': google}})


@pytest.fixture
async def cockpit_actor(tmp_path):
    actors = []
    async def make(responses=(), routes=None, media=None):
        services = CockpitServices(media or LocalMediaCatalog(ITEMS), routes)
        actor = SessionActor(str(len(actors)), Database(tmp_path / f'{len(actors)}.sqlite'), FakeArk(responses), cockpit=services, maps=object(), web=object(), manual=object())
        actors.append(actor)
        queue = await actor.attach(); await queue.get()
        return actor, queue
    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


async def acknowledge(actor, command, *, ok=True, changes=None):
    pending = actor.pending_cockpit[command['command_id']]
    state = {**deepcopy(pending['expected']), **(changes or {})}
    await actor.post({'type': 'cockpit.receipt', 'command_id': command['command_id'], 'turn_id': command['turn_id'],
                      'state_version': command['expected_state_version'] + 1, 'ok': ok, 'state': state,
                      **{key:command[key] for key in ('request_id','session_id','target_device','kind')}})


async def test_actor_does_not_claim_or_apply_playback_before_actual_device_receipt(cockpit_actor):
    actor, queue = await cockpit_actor()
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'play', 'arguments': {'content_id': 'local:a'}})
    command, _ = await wait_for(queue, 'cockpit.command')
    assert actor.cockpit_state['media']['status'] == 'closed'
    await acknowledge(actor, command)
    event, _ = await wait_for(queue, 'cockpit.state')
    assert event['state']['status'] == 'playing' and event['state']['version'] == 1
    await actor.post({'type': 'cockpit.receipt', 'command_id': command['command_id'], 'ok': True, 'state': {'status': 'paused'}})
    await actor.queue.join()
    assert actor.cockpit_state['media']['status'] == 'playing'


async def test_device_failure_and_wrong_content_receipts_preserve_previous_state(cockpit_actor):
    actor, queue = await cockpit_actor()
    for ok,changes in [(False, {}), (True, {'content_id': 'invented'})]:
        await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'play', 'arguments': {'content_id': 'local:a'}})
        command, _ = await wait_for(queue, 'cockpit.command')
        await acknowledge(actor, command, ok=ok, changes=changes)
        result, _ = await wait_for(queue, 'cockpit.result')
        assert not result['result']['ok'] and actor.cockpit_state['media']['status'] == 'closed'


async def test_search_content_id_play_and_stale_receipt_follow_actor_order(cockpit_actor):
    actor, queue = await cockpit_actor([
        {'tool_calls': [call('search_media', {'kind': 'music', 'query': '测试乐曲'})]},
        {'content': '这就播放。', 'tool_calls': [call('control_media', {'action': 'play', 'content_id': 'local:a'}, id='play')]},
        {'content': '你好。'},
    ])
    await actor.post({'type': 'input', 'text': '播放测试乐曲'})
    command, _ = await wait_for(queue, 'cockpit.command')
    await actor.post({'type': 'input', 'text': '你好'})
    await wait_for(queue, 'stream.done')
    await acknowledge(actor, command)
    await actor.queue.join()
    assert actor.cockpit_state['media']['content_id'] is None


async def test_ui_search_builds_real_queue_and_telemetry_cannot_overwrite_user_pause(cockpit_actor):
    actor, queue = await cockpit_actor()
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'search', 'arguments': {'kind': 'music', 'query': '测试乐曲'}})
    await wait_for(queue, 'cockpit.catalog')
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'play', 'arguments': {'content_id': 'local:a'}})
    command, _ = await wait_for(queue, 'cockpit.command'); await acknowledge(actor, command); await wait_for(queue, 'cockpit.state')
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'pause', 'arguments': {}})
    command, _ = await wait_for(queue, 'cockpit.command'); await acknowledge(actor, command); await wait_for(queue, 'cockpit.state')
    await actor.post({'type': 'cockpit.media_state', 'state_version': 1, 'state': {'content_id': 'local:a', 'status': 'playing'}})
    await actor.queue.join()
    assert actor.cockpit_state['media']['status'] == actor.cockpit_state['media']['desired_status'] == 'paused'


async def test_navigation_requires_prior_selected_provider_scoped_place(cockpit_actor):
    actor, queue = await cockpit_actor([{'tool_calls': [call('manage_navigation', {'action': 'start', 'place_ref': 'invented'})]}, {'content': '请先选择地点。'}])
    await actor.post({'type': 'input', 'text': '导航过去'})
    done, events = await wait_for(queue, 'stream.done')
    assert not any(e['type'] == 'cockpit.command' for e in events)
    assert not actor.cockpit_state['navigation']['active']


def query_receipt(command, result, **changes):
    return {'type': 'cockpit.receipt', **{key:command[key] for key in ('command_id','request_id','session_id','turn_id','target_device','kind')},
            'ok': True, 'result': result, **changes}


async def test_device_catalog_query_is_separate_from_playback_control(cockpit_actor):
    from backend.cockpit import DeviceMediaCatalog
    actor, queue = await cockpit_actor(media=DeviceMediaCatalog())
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'search', 'arguments': {'kind': 'music', 'query': '测试乐曲'}})
    request, _ = await wait_for(queue, 'cockpit.command')
    assert request['kind'] == 'query' and request['execution_location'] == 'device'
    assert request['session_id'] == actor.id and request['target_device'] == 'browser:' + actor.id
    public = [{key:value for key,value in item.items() if key != 'source_url'} for item in ITEMS[:2]]
    await actor.post(query_receipt(request, {'ok': True, 'provider': 'device_catalog', 'items': public}))
    await wait_for(queue, 'cockpit.catalog')
    assert actor.cockpit_state['media']['status'] == 'closed' and actor.cockpit_state['media']['version'] == 0
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'play', 'arguments': {'content_id': 'local:a'}})
    command, _ = await wait_for(queue, 'cockpit.command')
    assert command['kind'] == 'control' and command['arguments']['content_id'] == 'local:a'
    assert 'source_url' not in command['arguments']
    await acknowledge(actor, command)
    await wait_for(queue, 'cockpit.state')
    assert actor.cockpit_state['media']['status'] == 'playing'


@pytest.mark.parametrize('field', ['request_id', 'session_id', 'turn_id', 'target_device', 'kind'])
async def test_wrong_query_identity_is_discarded_without_consuming_valid_pending_request(cockpit_actor, field):
    from backend.cockpit import DeviceMediaCatalog
    actor, queue = await cockpit_actor(media=DeviceMediaCatalog())
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'search', 'arguments': {'kind': 'music', 'query': '测试'}})
    request, _ = await wait_for(queue, 'cockpit.command')
    result = {'ok': True, 'provider': 'device_catalog', 'items': [ITEMS[0]]}
    await actor.post(query_receipt(request, result, **{field:'wrong'}))
    await actor.queue.join()
    assert not actor.media_choices and request['command_id'] in actor.pending_cockpit
    await actor.post(query_receipt(request, result))
    await wait_for(queue, 'cockpit.catalog')
    assert 'local:a' in actor.media_choices


async def test_device_query_timeout_does_not_apply_late_content_or_retry_cloud(cockpit_actor):
    from backend.cockpit import DeviceMediaCatalog
    actor, queue = await cockpit_actor(media=DeviceMediaCatalog())
    await actor.post({'type': 'cockpit.control', 'domain': 'media', 'action': 'search', 'arguments': {'kind': 'music', 'query': '测试'}})
    request, _ = await wait_for(queue, 'cockpit.command')
    await actor.post({'type': '_cockpit_timeout', 'command_id': request['command_id']})
    result, _ = await wait_for(queue, 'cockpit.result')
    assert result['result']['code'] == 'device_timeout'
    await actor.post(query_receipt(request, {'items': [ITEMS[0]]}))
    await actor.queue.join()
    assert not actor.media_choices and actor.cockpit_state['media']['version'] == 0


async def test_device_places_query_preserves_scope_and_does_not_start_navigation(cockpit_actor):
    class DevicePlaces:
        def info(self): return {'execution_location':'device'}
        async def search_places(self, **arguments): raise AssertionError('must not query cloud')
    actor, queue = await cockpit_actor([{'tool_calls': [call('search_places', {'area':'巴黎','query':'咖啡'})]}])
    actor.maps = DevicePlaces()
    actor.configure_nlp()
    await actor.post({'type':'input','text':'巴黎找一家咖啡馆'})
    request, _ = await wait_for(queue, 'cockpit.command')
    assert request['kind'] == 'query' and request['domain'] == 'places'
    await actor.post(query_receipt(request, {'ok':True,'kind':'places','provider':'google','places':[PLACE]}))
    await wait_for(queue, 'stream.done')
    assert 'google:same-id' in actor.places and not actor.cockpit_state['navigation']['active']


async def test_navigation_origin_start_waypoints_cancel_require_matching_device_state(cockpit_actor):
    actor, queue = await cockpit_actor()
    places = {p['place_ref']:p for p in [scoped_place(PLACE,'google'), scoped_place({**PLACE,'place_id':'second','name':'地点二'},'google')]}
    actor.places = deepcopy(places)
    for action,arguments in [('set_origin', {'place_ref':'google:same-id'}),('start',{'place_ref':'google:second'}),
                             ('add_waypoint',{'place_ref':'google:same-id'}),('remove_waypoint',{'waypoint_ref':'google:same-id'}),('cancel',{})]:
        await actor.post({'type':'cockpit.control','domain':'navigation','action':action,'arguments':arguments})
        command, _ = await wait_for(queue,'cockpit.command')
        assert actor.cockpit_state['navigation']['version'] == command['expected_state_version']
        await acknowledge(actor,command); await wait_for(queue,'cockpit.state')
    assert not actor.cockpit_state['navigation']['active']
    assert actor.cockpit_state['navigation']['origin']['place_ref'] == 'google:same-id'


async def test_real_route_result_is_read_only_and_retains_provenance(cockpit_actor):
    class Routes:
        def info(self): return {'provider':'configured_routes','configured':True}
        async def route(self, origin,destination,waypoints):
            assert origin['place_ref']=='google:same-id'
            return {'ok':True,'configured':True,'provider':'configured_routes','distance_meters':1234,'duration_seconds':200,'waypoint_order':[]}
    actor, queue = await cockpit_actor(routes=Routes())
    actor.cockpit_state['navigation'].update(origin=scoped_place(PLACE,'google'),destination=scoped_place({**PLACE,'place_id':'other'},'google'))
    await actor.post({'type':'cockpit.control','domain':'navigation','action':'route','arguments':{}})
    event, events = await wait_for(queue,'cockpit.state')
    assert not any(e['type']=='cockpit.command' for e in events)
    assert event['state']['route']['provider']=='configured_routes'
    assert event['state']['version']==0 and not event['state']['active']


async def test_same_turn_media_controls_wait_for_each_real_receipt(cockpit_actor):
    actor, queue = await cockpit_actor([{'tool_calls':[
        call('control_media',{'action':'play','content_id':'local:a'},id='first'),
        call('control_media',{'action':'pause'},id='second')]}])
    actor.media_choices = {item['content_id']:item for item in (await actor.cockpit.media.search(kind='music', query='测试乐曲'))['items']}
    await actor.post({'type':'input','text':'播放之后暂停'})
    first, _ = await wait_for(queue,'cockpit.command')
    await actor.queue.join()
    assert len(actor.pending_cockpit)==1
    assert not any(event['type'] == 'cockpit.command' for event in list(queue._queue))
    await acknowledge(actor,first)
    second, _ = await wait_for(queue,'cockpit.command')
    assert first['action']=='play' and second['action']=='pause' and second['expected_state_version']==1
    await acknowledge(actor,second); await wait_for(queue,'stream.done')
    assert actor.cockpit_state['media']['status']=='paused' and actor.cockpit_state['media']['version']==2


async def test_state_conflict_observes_device_and_reprepares_once(cockpit_actor):
    actor, queue = await cockpit_actor()
    await actor.post({'type':'cockpit.control','domain':'media','action':'play','arguments':{'content_id':'local:a'}})
    first, _ = await wait_for(queue,'cockpit.command')
    await actor.post({'type':'cockpit.receipt',**{key:first[key] for key in ('command_id','request_id','session_id','turn_id','target_device','kind')},
                      'ok':False,'state_version':1,'error':{'code':'state_conflict','message':'outdated'}})
    observation, _ = await wait_for(queue,'cockpit.command')
    assert observation['kind']=='query' and observation['action']=='get_status'
    assert actor.cockpit_state['media']['version']==0
    actual = {'content_id':'local:b','kind':'music','title':'测试乐曲二','status':'playing','desired_status':'playing','position_seconds':3,'volume':30}
    await actor.post(query_receipt(observation,{'ok':True,'state':actual,'state_version':1}))
    retry, _ = await wait_for(queue,'cockpit.command')
    assert retry['kind']=='control' and retry['expected_state_version']==1 and retry['command_id']!=first['command_id']
    await actor.post({'type':'cockpit.receipt',**{key:retry[key] for key in ('command_id','request_id','session_id','turn_id','target_device','kind')},
                      'ok':False,'error':{'code':'state_conflict','message':'outdated again'}})
    result, _ = await wait_for(queue,'cockpit.result')
    assert not result['result']['ok'] and actor.cockpit_state['media']['version']==1
    assert not actor.pending_cockpit


async def test_media_services_separate_cp_identity_resolution_and_configuration():
    from backend.cockpit import MediaServices
    music = LocalMediaCatalog([{**ITEMS[0], 'content_id':'same'}])
    video = LocalMediaCatalog([{**ITEMS[2], 'content_id':'same'}])
    radio = LocalMediaCatalog([{**ITEMS[3], 'content_id':'same'}])
    broker = MediaServices({'music':music,'video':video,'radio':radio}, {'music':'music_cp','video':'video_cp','radio':'radio_cp'})
    first = (await broker.search(kind='music',query='测试'))['items'][0]
    second = (await broker.search(kind='video',query='测试'))['items'][0]
    assert first['content_id'] != second['content_id']
    assert (await broker.resolve(first['content_id']))['source_url']==ITEMS[0]['source_url']
    assert (await broker.resolve(second['content_id']))['source_url']==ITEMS[2]['source_url']
    assert broker.parse_id(first['content_id'])==('music','music_cp','same')
    assert broker.info()['bindings']['video']=='video_cp'
    with pytest.raises(CockpitError): await broker.resolve(first['content_id'].replace(':music_cp:',':video_cp:'))
    disabled = TencentMusicGateway()
    try:
        with pytest.raises(CockpitError): MediaServices({'music':music,'video':disabled,'radio':radio})
    finally:
        await disabled.aclose()


async def test_device_broker_scopes_results_and_controls_using_raw_device_reference(cockpit_actor):
    from backend.cockpit import MediaServices,DeviceMediaCatalog
    marker = DeviceMediaCatalog()
    broker = MediaServices({kind:marker for kind in ('music','video','radio')},{kind:'device_'+kind for kind in ('music','video','radio')})
    actor,queue = await cockpit_actor(media=broker)
    await actor.post({'type':'cockpit.control','domain':'media','action':'search','arguments':{'kind':'video','query':'测试'}})
    search,_ = await wait_for(queue,'cockpit.command')
    assert search['kind']=='query' and search['arguments']['provider_binding']=='device_video'
    await actor.post(query_receipt(search,{'ok':True,'items':[ITEMS[2]]}))
    result,_ = await wait_for(queue,'cockpit.catalog')
    content_id=result['items'][0]['content_id']
    assert broker.parse_id(content_id)==('video','device_video','local:v')
    await actor.post({'type':'cockpit.control','domain':'media','action':'play','arguments':{'content_id':content_id}})
    command,_ = await wait_for(queue,'cockpit.command')
    assert command['kind']=='control' and command['arguments']['content_id']==content_id
    assert command['arguments']['device_content_id']=='local:v' and 'source_url' not in command['arguments']
    await acknowledge(actor,command);await wait_for(queue,'cockpit.state')
    assert actor.cockpit_state['media']['content_id']==content_id


async def test_device_broker_direct_selection_resolves_raw_id_without_sending_url_to_cloud(cockpit_actor):
    from backend.cockpit import MediaServices,DeviceMediaCatalog
    marker=DeviceMediaCatalog()
    broker=MediaServices({kind:marker for kind in ('music','video','radio')},{kind:'device_'+kind for kind in ('music','video','radio')})
    actor,queue=await cockpit_actor(media=broker)
    content_id=broker.scoped_id('music','local:a')
    await actor.post({'type':'cockpit.control','domain':'media','action':'play','arguments':{'content_id':content_id}})
    query,_=await wait_for(queue,'cockpit.command')
    assert query['kind']=='query' and query['action']=='resolve' and query['arguments']['content_id']=='local:a'
    await actor.post(query_receipt(query,{'ok':True,'item':ITEMS[0]}))
    command,_=await wait_for(queue,'cockpit.command')
    assert command['arguments']['content_id']==content_id and 'source_url' not in command['arguments']
    await acknowledge(actor,command);await wait_for(queue,'cockpit.state')


async def test_independent_cp_bindings_cannot_resolve_other_kinds_or_old_profiles():
    from backend.cockpit import MediaServices
    class Provider:
        def __init__(self, kind): self.kind, self.calls = kind, []
        def info(self): return {'provider': self.kind + '_vendor', 'kinds': [self.kind], 'configured': True, 'execution_location': 'cloud'}
        async def search(self, **arguments):
            self.calls.append(('search', arguments))
            return {'ok': True, 'items': [{'content_id': 'shared-id', 'kind': self.kind, 'title': self.kind}]}
        async def resolve(self, identifier):
            self.calls.append(('resolve', identifier))
            return {'content_id': identifier, 'kind': self.kind, 'title': self.kind, 'source_url': 'https://' + self.kind + '.example/play'}
        async def aclose(self): pass
    providers = {kind: Provider(kind) for kind in ('music', 'video', 'radio')}
    broker = MediaServices(providers, {kind: kind + '_profile' for kind in providers})
    for kind in providers:
        item = (await broker.search(kind=kind, query='anything'))['items'][0]
        assert (await broker.resolve(item['content_id']))['source_url'] == 'https://' + kind + '.example/play'
        assert broker.provider_info(kind)['provider'] == kind + '_vendor'
        assert providers[kind].calls == [('search', {'kind': kind, 'query': 'anything', 'max_results': 5}), ('resolve', 'shared-id')]
    music_id = broker.scoped_id('music', 'shared-id')
    for wrong_id in (music_id.replace(':music:', ':video:'), music_id.replace(':music_profile:', ':removed_profile:')):
        with pytest.raises(CockpitError) as error: await broker.resolve(wrong_id)
        assert error.value.code == 'unknown_content'
    assert all(len(provider.calls) == 2 for provider in providers.values())
    # A provider response must confirm the requested kind, even for a valid scoped ID.
    providers['music'].kind = 'video'
    with pytest.raises(CockpitError) as error: await broker.resolve(music_id)
    assert error.value.code == 'invalid_content'
    with pytest.raises(CockpitError): await broker.search(kind='unsupported', query='anything')
    with pytest.raises(CockpitError): broker.scoped_id('music', '音' * 200)


async def test_media_broker_closes_shared_provider_once_and_keeps_device_ids_scoped():
    from backend.cockpit import DeviceMediaCatalog, MediaServices
    class SharedDevice(DeviceMediaCatalog):
        def __init__(self): self.closed = 0
        async def aclose(self): self.closed += 1
    shared = SharedDevice()
    broker = MediaServices({kind: shared for kind in ('music', 'video', 'radio')}, {kind: kind + '_device' for kind in ('music', 'video', 'radio')})
    raw = '本机:/same:id'
    for kind in ('music', 'video', 'radio'):
        public = broker.scope_device_result(kind, {'ok': True, 'item': {'content_id': raw, 'kind': kind, 'title': kind, 'source_url': 'blob:device-secret'}})
        identifier = public['item']['content_id']
        assert broker.parse_id(identifier) == (kind, kind + '_device', raw)
        assert broker.device_content_id(identifier) == raw
        assert 'source_url' not in public['item']
        assert public['binding'] == kind + '_device'
    await broker.aclose()
    assert shared.closed == 1


async def test_route_provider_error_has_navigation_feedback_and_redacted_trace(cockpit_actor):
    from backend.services.contracts import ProviderError
    class Routes:
        def info(self): return {'provider': 'configured_routes', 'configured': True}
        async def route(self, origin, destination, waypoints):
            raise ProviderError('service_unavailable', 'provider route failed', provider='configured_routes', details={'api_key':'private-value','http_status':403})
    actor, queue = await cockpit_actor(routes=Routes())
    actor.cockpit_state['navigation'].update(origin=scoped_place(PLACE,'google'),destination=scoped_place({**PLACE,'place_id':'other'},'google'))
    await actor.post({'type':'cockpit.control','domain':'navigation','action':'route','arguments':{}})
    result, _ = await wait_for(queue,'cockpit.result')
    assert result['result']['code']=='service_unavailable'
    assert result['result']['reason']=='这次没有查到可用的真实路线，请稍后重试；当前没有距离或到达时间。'
    assert actor.cockpit_state['navigation']['route'] is None
    await actor.trace_queue.join()
    traces = await asyncio.to_thread(actor.db.traces_after, actor.id)
    prepared = next(trace for trace in traces if trace['kind']=='cockpit.prepared')
    assert prepared['data']['error']['provider_error']['code']=='service_unavailable'
    assert prepared['data']['error']['diagnostics']['http_status']==403
    assert 'private-value' not in json.dumps(prepared)


async def test_real_natural_end_telemetry_stops_intent_without_allowing_focus_to_change_it(cockpit_actor):
    actor, queue = await cockpit_actor()
    await actor.post({'type':'cockpit.control','domain':'media','action':'play','arguments':{'content_id':'local:a'}})
    command, _ = await wait_for(queue,'cockpit.command')
    await acknowledge(actor,command); await wait_for(queue,'cockpit.state')
    await actor.post({'type':'cockpit.media_state','state_version':1,'state':{'content_id':'local:a','status':'paused','desired_status':'paused','focus_reason':'speech'}})
    focus, _ = await wait_for(queue,'cockpit.state')
    assert focus['state']['desired_status']=='playing'
    await actor.post({'type':'cockpit.media_state','state_version':1,'event':'ended','state':{'content_id':'local:a','status':'stopped','desired_status':'stopped'}})
    ended, _ = await wait_for(queue,'cockpit.state')
    assert ended['state']['status']==ended['state']['desired_status']=='stopped'
    assert ended['state']['version']==1
    await actor.post({'type':'cockpit.media_state','state_version':0,'event':'ended','state':{'content_id':'local:a','status':'stopped','desired_status':'playing'}})
    await actor.queue.join()
    assert actor.cockpit_state['media']['desired_status']=='stopped'


@pytest.mark.parametrize('device', [False, True])
async def test_new_content_search_does_not_invalidate_existing_playback_queue(device):
    from backend.cockpit import DeviceMediaCatalog
    from backend.cockpit.media import media_item
    services = CockpitServices(DeviceMediaCatalog() if device else LocalMediaCatalog(ITEMS))
    music_choices = {item['content_id']: media_item(item) for item in ITEMS[:2]}
    playing = await prepare_media(services, 'play', {'content_id':'local:a'}, initial_cockpit_state()['media'], music_choices)
    video_choices = {'local:v':media_item(ITEMS[2])}
    next_track = await prepare_media(services, 'next', {}, playing['expected'], video_choices)
    assert next_track['expected']['content_id']=='local:b'
    assert next_track['expected']['queue']==playing['expected']['queue']
    previous = await prepare_media(services, 'previous', {}, next_track['expected'], video_choices)
    assert previous['expected']['content_id']=='local:a'
    if device: assert 'source_url' not in next_track['arguments']


@pytest.mark.parametrize('status,code', [(400,'invalid_response'),(401,'auth'),(403,'auth'),(429,'rate_limit'),(503,'unavailable')])
async def test_cp_gateway_http_errors_keep_redacted_actual_response(status, code):
    from backend.cockpit import HTTPMediaGateway
    secret, signed_url = 'private-cp-key', 'https://streams.example/audio?signature=private-play-capability'
    def respond(request):
        assert request.extensions['timeout']['read'] == 3.5
        return httpx.Response(status, json={'error':{'code':'actual_vendor_code','token':secret,'playback_url':signed_url,'debug':'echo ' + secret + ' ' + signed_url}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway=HTTPMediaGateway(provider_name='video_cp',kind='video',base_url='https://cp.example',api_key=secret,client=client,timeout_seconds=3.5)
        with pytest.raises(ProviderError) as failure: await gateway.search(kind='video',query='内容')
        assert failure.value.code==code and failure.value.provider=='video_cp'
        assert failure.value.details['response']['status_code']==status
        assert failure.value.details['response']['body']['error']['code']=='actual_vendor_code'
        raw=json.dumps(failure.value.details)
        assert secret not in raw and signed_url not in raw and 'private-play-capability' not in raw


@pytest.mark.parametrize('mode,code', [('timeout','timeout'),('connection','unavailable'),('json','invalid_response')])
async def test_cp_gateway_transport_json_and_public_auth_policy(mode, code):
    def respond(request):
        assert 'authorization' not in request.headers
        if mode=='timeout': raise httpx.ReadTimeout('timed out',request=request)
        if mode=='connection': raise httpx.ConnectError('disconnected',request=request)
        return httpx.Response(200,text='not JSON: vendor-format-error')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway=TencentMusicGateway(base_url='https://cp.example',client=client,require_api_key=False,timeout_seconds=2)
        assert gateway.info()['configured'] is True
        with pytest.raises(ProviderError) as failure: await gateway.search(kind='music',query='测试')
        assert failure.value.code==code
        if mode=='json': assert failure.value.details['response']['body']=='not JSON: vendor-format-error'


async def test_real_cp_search_and_resolve_responses_reach_actor_trace_without_capabilities(cockpit_actor):
    from backend.cockpit import MediaServices
    secret, signed_url = 'trace-private-cp-key', 'https://streams.example/play?signature=resolve-secret'
    def respond(request):
        payload=json.loads(request.content)
        if request.url.path.endswith('/search'):
            assert payload['kind']=='music'
            return httpx.Response(200,json={'ok':True,'items':[{'content_id':'same','kind':'music','title':'测试内容','source_url':signed_url}],
                'actual_request_id':'search-receipt-1','debug_key':secret})
        assert payload=={'content_id':'same'}
        return httpx.Response(200,json={'ok':True,'item':{'content_id':'same','kind':'music','title':'测试内容','source_url':signed_url},
            'actual_request_id':'resolve-receipt-2'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cp=TencentMusicGateway(base_url='https://cp.example',api_key=secret,client=client,timeout_seconds=4)
        local=LocalMediaCatalog(ITEMS[2:])
        broker=MediaServices({'music':cp,'video':local,'radio':local},{'music':'tencent','video':'local','radio':'local'})
        actor,queue=await cockpit_actor(media=broker)
        await actor.post({'type':'cockpit.control','domain':'media','action':'search','arguments':{'kind':'music','query':'测试内容'}})
        catalog,_=await wait_for(queue,'cockpit.catalog')
        assert all('_provider_trace' not in item and 'source_url' not in item for item in catalog['items'])
        await actor.post({'type':'cockpit.control','domain':'media','action':'play','arguments':{'content_id':catalog['items'][0]['content_id']}})
        command,_=await wait_for(queue,'cockpit.command')
        assert command['arguments']['source_url']==signed_url and '_provider_trace' not in command['arguments']
        await acknowledge(actor,command);await wait_for(queue,'cockpit.state')
        await actor.trace_queue.join()
        traces=await asyncio.to_thread(actor.db.traces_after,actor.id)
        search=next(row['data'] for row in traces if row['kind']=='cockpit.prepared')
        resolve=next(row['data'] for row in traces if row['kind']=='cockpit.provider')
        assert search['provider_trace']['response']['body']['actual_request_id']=='search-receipt-1'
        assert resolve['provider_trace']['response']['body']['actual_request_id']=='resolve-receipt-2'
        assert '_provider_trace' not in search['result']
        serialized=json.dumps(traces,ensure_ascii=False)
        assert secret not in serialized and signed_url not in serialized and 'resolve-secret' not in serialized
        assert actor.cockpit_state['media']['content_id']==broker.scoped_id('music','same')


async def test_cp_raw_search_diagnostics_never_enter_next_model_request(cockpit_actor):
    from backend.cockpit import MediaServices
    signed='https://stream.example/play?signature=keep-private'
    def respond(request): return httpx.Response(200,json={'ok':True,'items':[{'content_id':'raw-id','kind':'music','title':'实际曲目','source_url':signed}],'upstream_request_id':'raw-response-id'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        cp=TencentMusicGateway(base_url='https://cp.example',api_key='model-private-key',client=client)
        local=LocalMediaCatalog(ITEMS[2:])
        broker=MediaServices({'music':cp,'video':local,'radio':local},{'music':'music_cp','video':'local','radio':'local'})
        actor,queue=await cockpit_actor([{'tool_calls':[call('search_media',{'kind':'music','query':'实际曲目'})]},{'content':'找到实际曲目。'}],media=broker)
        await actor.post({'type':'input','text':'查一下实际曲目'})
        await wait_for(queue,'stream.done')
        assert len(actor.ark.requests)==2
        model_input=json.dumps(actor.ark.requests[-1]['messages'],ensure_ascii=False)
        assert '_provider_trace' not in model_input and 'raw-response-id' not in model_input
        assert signed not in model_input and 'model-private-key' not in model_input
        tool_result=json.loads(next(message['content'] for message in reversed(actor.ark.requests[-1]['messages']) if message['role']=='tool'))
        assert tool_result['items'][0]['content_id']==broker.scoped_id('music','raw-id')
        await actor.trace_queue.join()
        traces=await asyncio.to_thread(actor.db.traces_after,actor.id)
        trace=next(row for row in traces if row['kind']=='observations.received')
        assert trace['data']['observations'][0]['result']['_provider_trace']['response']['body']['upstream_request_id']=='raw-response-id'


@pytest.mark.parametrize('version', [None, True, '1', 1.0, 2])
def test_control_receipt_requires_explicit_actual_integer_revision(version):
    expected=initial_cockpit_state()['media']
    command={'expected':expected,'wire':{'domain':'media','expected_state_version':0}}
    receipt={'state':deepcopy(expected)}
    if version is not None: receipt['state_version']=version
    with pytest.raises(CockpitError) as failure: validated_receipt(command,receipt)
    assert failure.value.code=='stale_receipt'


def test_structured_device_failure_retains_code_and_terminal_capability_policy():
    from backend.cockpit.device import receipt_failure
    request={'target_device':'browser:session'}
    failure=receipt_failure(request,{'ok':False,'error':{'code':'unsupported_capability','retryable':True,'message':'raw device prose','details':{'token':'secret'}}})
    assert failure.code=='unsupported_capability' and failure.retryable is False
    assert failure.provider=='browser:session'
    assert failure.details['receipt']['error']['details']['token']=='[redacted]'
    assert failure.details['receipt']['error']['message']=='raw device prose'
    assert 'raw device prose' not in failure.public()['message']
    assert receipt_failure(request,{'ok':False,'error':{'code':'device_timeout'}}).retryable is True
    assert receipt_failure(request,{'ok':False,'error':{'code':'vendor_unavailable','retryable':False}}).code=='vendor_unavailable'
    invalid=receipt_failure(request,{'ok':False,'error':{'code':'RAW error: print credentials','retryable':'yes'}})
    assert invalid.code=='device_failed' and invalid.retryable is False


async def test_unsupported_device_places_query_keeps_terminal_error_and_complete_safe_trace(cockpit_actor):
    class DevicePlaces:
        def __init__(self): self.cloud_calls=0
        def info(self): return {'execution_location':'device','provider':'device_places'}
        async def search_places(self,**arguments):
            self.cloud_calls+=1
            raise AssertionError('No cloud fallback is permitted')
    actor,queue=await cockpit_actor([{'tool_calls':[call('search_places',{'area':'巴黎','query':'公园'})]}, {'content':'这台设备暂时没有地点查询能力。'}])
    device=DevicePlaces();actor.maps=device;actor.configure_nlp()
    await actor.post({'type':'input','text':'帮我找巴黎的公园'})
    request,_=await wait_for(queue,'cockpit.command')
    raw_error={'code':'unsupported_capability','retryable':True,'message':'TECHNICAL_ONLY https://device.example/handler?signature=private-capability','details':{'vendor_code':'missing_places_handler','token':'private-device-token'}}
    await actor.post(query_receipt(request,{},ok=False,error=raw_error))
    await wait_for(queue,'stream.done')
    assert device.cloud_calls==0 and not actor.places and not actor.cockpit_state['navigation']['active']
    assert len(actor.ark.requests)==2
    messages=actor.ark.requests[-1]['messages']
    result=json.loads(next(message['content'] for message in reversed(messages) if message['role']=='tool'))
    assert result['code']=='unsupported_capability' and result['retryable'] is False
    assert result['provider_error']['code']=='unsupported_capability' and result['provider_error']['retryable'] is False
    assert result['reason']=='这台设备还没有提供这项能力，暂时无法完成这次查询或操作。'
    assert 'TECHNICAL_ONLY' not in json.dumps(messages,ensure_ascii=False)
    assert 'private-device-token' not in json.dumps(messages,ensure_ascii=False)
    await actor.trace_queue.join()
    traces=await asyncio.to_thread(actor.db.traces_after,actor.id)
    raw=next(row for row in traces if row['kind']=='device.receipt')
    completed=next(row for row in traces if row['kind']=='device.query.completed')
    assert raw['data']['receipt']['error']['code']=='unsupported_capability'
    assert completed['status']=='failed'
    assert completed['data']['provider_error']['retryable'] is False
    assert completed['data']['receipt']['error']['details']['vendor_code']=='missing_places_handler'
    assert completed['data']['diagnostics']['receipt']['error']['code']=='unsupported_capability'
    assert 'TECHNICAL_ONLY' in json.dumps(completed)
    assert 'private-device-token' not in json.dumps(traces) and 'private-capability' not in json.dumps(traces)
    assert len([row for row in traces if row['kind']=='device.query.dispatched'])==1


async def test_device_control_failures_preserve_safe_structured_code(cockpit_actor):
    actor,queue=await cockpit_actor()
    await actor.post({'type':'cockpit.control','domain':'media','action':'play','arguments':{'content_id':'local:a'}})
    request,_=await wait_for(queue,'cockpit.command')
    await actor.post(query_receipt(request,{},ok=False,error={'code':'unsupported_capability','message':'INTERNAL_DEVICE_FAILURE','retryable':True}))
    result,_=await wait_for(queue,'cockpit.result')
    assert result['result']['code']=='unsupported_capability' and result['result']['retryable'] is False
    assert 'INTERNAL_DEVICE_FAILURE' not in json.dumps(result)
    assert actor.cockpit_state['media']['status']=='closed'
