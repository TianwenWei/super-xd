"""Domain policy checks use structured facts and receipts, never live services."""
from copy import deepcopy
import json

import pytest

from super_xd.demo.devices.cockpit import initial_cockpit_state
from super_xd.nlp.adapters.media.base import CockpitError
from super_xd.nlp.policy.product import ProductPolicyError, ProductTalkerPolicy, normalize_search_arguments, normalize_task_arguments, prepare_media_action, prepare_product_action, product_context, resolve_place_reference


def media(identifier='catalog:a', kind='music'):
    return {'content_id': identifier, 'kind': kind, 'title': identifier,
            'provider': 'catalog', 'duration_seconds': 120}


def place(identifier='a', *, turn='older', presented=True):
    return {'place_ref': 'provider:' + identifier, 'name': identifier, 'provider': 'provider',
            'source_turn_id': turn, 'presented_to_user': presented,
            'details': {'place_id': identifier, 'location': {'latitude': 48.8, 'longitude': 2.3},
                        'coordinate_system': 'WGS84'}}


def physical_place(identifier='a'):
    value = place(identifier)
    return {**value['details'], **{key: value[key] for key in ('place_ref', 'name', 'provider')}}


def request(name='control_media', arguments=None, identifier='call'):
    return {'name': name, 'arguments': arguments or {'action': 'pause'}, 'invocation_id': identifier,
            'tool_call_id': identifier, 'idempotency_key': identifier}


def input_(requests=()):
    cockpit = initial_cockpit_state()
    cockpit['media'].update(media(), status='playing', desired_status='playing', queue=[media(), media('catalog:b')])
    cockpit['navigation'].update(origin=physical_place('a'), destination=physical_place('b'),
                                  waypoints=[physical_place('c')], active=True)
    return {'start': {'identity': {'run_id': 'run', 'request_id': 'req', 'turn_id': 'current', 'context_revision': 1},
                      'user_text': '在巴黎找停车场', 'speaker_seat': 'driver', 'history': [],
                      'context': {'vehicle': {'state': {'_meta': {'speed_kph': 0}}},
                                  'environment': {'location': {'name': '巴黎', 'country': '法国'}},
                                  'capabilities': {'tools': []}, 'cockpit': cockpit,
                                  'media': [media(), media('catalog:b')],
                                  'places': [place('a'), place('b'), place('c')],
                                  'tasks': [{'id': 'task', 'revision': 2, 'goal': '查询', 'status': 'running'}]}},
            'checkpoint': {'prepared': {'messages': []}, 'messages': [], 'pending': list(requests)},
            'requests': list(requests)}


def observation(value, item, result, status='completed'):
    return {'identity': value['start']['identity'], 'invocation_id': item['invocation_id'], 'status': status, 'result': result}


def receipt(command, **updates):
    state = {**deepcopy(command['expected']), 'version': command['expected_state_version'] + 1, **updates}
    return {'ok': True, 'executed': True, 'state': state}


def policy(value):
    bindings = {}
    capabilities = value['start']['context']['capabilities']['tools']
    for item in value['requests']:
        name, arguments = item['name'], item['arguments']
        if name in {'delegate_task', 'delegate_vehicle_control', 'manage_task'}:
            continue
        operation = 'query' if name in {'search_media', 'search_places', 'get_weather'} or arguments.get('action') in {'get_status', 'route'} else 'control'
        location = 'cloud' if operation == 'query' else 'device'
        bindings[ProductTalkerPolicy.binding_key(item)] = {'kind': 'tool_execution', 'operation': operation, 'execution_location': location}
        capabilities.append({'name': name,
                             'action': arguments.get('action') if name in {'control_media', 'manage_navigation'} else None,
                             'content_kind': arguments.get('kind') if name == 'search_media' else None,
                             'operation': operation, 'execution_location': location})
    return ProductTalkerPolicy(bindings, enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management'])


@pytest.mark.parametrize('action,arguments,status', [
    ('play', {'content_id': 'catalog:b'}, 'playing'), ('pause', {}, 'paused'),
    ('resume', {}, 'playing'), ('stop', {}, 'stopped'), ('exit', {}, 'closed'),
    ('seek', {'position_seconds': 30}, 'playing'), ('volume', {'volume': 45}, 'playing'),
    ('next', {}, 'playing'),
])
def test_media_actions_prepare_distinct_intents_without_changing_actual_state(action, arguments, status):
    item = request(arguments={'action': action, **arguments})
    value = input_([item])
    before = deepcopy(value)
    decision = policy(value).validate_group(value)[0]
    command = decision['prepared_command']
    assert command['expected']['status'] == status and command['expected_state_version'] == 0
    if action == 'stop':
        assert command['expected']['position_seconds'] == 0 and command['expected']['content_id'] == 'catalog:a'
    if action == 'exit':
        assert command['expected']['content_id'] is None
    if action == 'next':
        assert decision['execution_arguments']['content_id'] == 'catalog:b'
    assert 'source_url' not in command['arguments']
    assert value['start']['context']['cockpit'] == before['start']['context']['cockpit']


def test_previous_uses_actual_queue_position_and_refuses_queue_edges():
    item = request(arguments={'action': 'previous'})
    value = input_([item])
    with pytest.raises(CockpitError) as edge:
        prepare_product_action(value, item)
    assert edge.value.code == 'queue_boundary'
    value['start']['context']['cockpit']['media']['content_id'] = 'catalog:b'
    assert prepare_product_action(value, item)['execution_arguments']['content_id'] == 'catalog:a'


@pytest.mark.parametrize('action,arguments', [
    ('seek', {}), ('seek', {'position_seconds': -1}), ('seek', {'position_seconds': 121}),
    ('volume', {}), ('volume', {'volume': 101}), ('volume', {'volume': float('nan')}),
    ('play', {'content_id': 'invented'}),
])
def test_media_invalid_parameters_never_become_commands(action, arguments):
    state = input_()['start']['context']['cockpit']['media']
    with pytest.raises(CockpitError):
        prepare_media_action(action, arguments, state, {'catalog:a': media()})


@pytest.mark.parametrize('action', ['pause', 'resume', 'stop', 'seek'])
def test_media_requires_current_content_for_relative_controls(action):
    with pytest.raises(CockpitError) as missing:
        prepare_media_action(action, {'position_seconds': 1}, initial_cockpit_state()['media'], {})
    assert missing.value.code == 'no_content'


def test_driver_video_resume_is_checked_but_pause_remains_available():
    value = input_()
    value['start']['context']['vehicle']['state']['_meta']['speed_kph'] = 30
    value['start']['context']['cockpit']['media']['kind'] = 'video'
    with pytest.raises(ProductPolicyError) as moving:
        prepare_product_action(value, request(arguments={'action': 'resume'}))
    assert moving.value.code == 'video_driving'
    assert prepare_product_action(value, request())['prepared_command']['expected']['status'] == 'paused'


@pytest.mark.parametrize('action,arguments', [
    ('start', {'place_ref': 'b'}), ('add_waypoint', {'place_ref': 'a'}),
    ('remove_waypoint', {'waypoint_ref': 'c'}), ('cancel', {}), ('set_origin', {'place_ref': 'b'}),
])
def test_navigation_actions_reuse_controller_preparation(action, arguments):
    item = request('manage_navigation', {'action': action, **arguments})
    value = input_([item])
    command = policy(value).validate_group(value)[0]['prepared_command']
    assert command['domain'] == 'navigation' and command['action'] == action
    assert command['expected_state_version'] == 0
    assert 'source_turn_id' not in (command['expected'].get('destination') or {})
    if action == 'cancel':
        assert command['expected']['active'] is False and command['expected']['waypoints'] == []
    if action == 'remove_waypoint':
        assert command['expected']['waypoints'] == []
    if action == 'set_origin':
        assert command['expected']['origin']['place_ref'] == 'provider:b'


def test_navigation_requires_previously_presented_unambiguous_place_ids():
    places = {'one:a': {'place_id': 'a'}, 'two:a': {'place_id': 'a'}}
    assert resolve_place_reference('a', places) is None
    item = request('manage_navigation', {'action': 'start', 'place_ref': 'a'})
    value = input_([item])
    value['start']['context']['places'][0]['source_turn_id'] = 'current'
    with pytest.raises(ProductPolicyError) as same_turn:
        prepare_product_action(value, item)
    assert same_turn.value.code == 'place_not_confirmed'


def test_navigation_waypoint_limit_missing_destination_and_unknown_removal_fail():
    value = input_()
    state = value['start']['context']['cockpit']['navigation']
    state['waypoints'] = [physical_place(str(index)) for index in range(10)]
    with pytest.raises(CockpitError):
        prepare_product_action(value, request('manage_navigation', {'action': 'add_waypoint', 'place_ref': 'a'}))
    state['destination'] = None
    with pytest.raises(CockpitError):
        prepare_product_action(value, request('manage_navigation', {'action': 'add_waypoint', 'place_ref': 'a'}))
    with pytest.raises(CockpitError):
        prepare_product_action(value, request('manage_navigation', {'action': 'remove_waypoint', 'waypoint_ref': 'missing'}))


def test_route_is_a_query_with_real_endpoints_and_does_not_start_navigation():
    item = request('manage_navigation', {'action': 'route', 'origin_ref': 'a'})
    value = input_([item])
    decision = policy(value).validate_group(value)[0]
    assert decision['operation'] == 'query' and decision['prepared_command'] is None
    assert decision['execution_arguments']['origin']['place_ref'] == 'provider:a'
    assert decision['execution_arguments']['destination']['place_ref'] == 'provider:b'
    value['start']['context']['cockpit']['navigation']['destination']['coordinate_system'] = 'GCJ02'
    with pytest.raises(ProductPolicyError) as mismatch:
        prepare_product_action(value, item)
    assert mismatch.value.code == 'unsupported_coordinate_system'


def test_group_defers_relative_action_until_receipt_and_reprepares_from_real_version():
    play = request(arguments={'action': 'play', 'content_id': 'catalog:b'}, identifier='play')
    pause = request(identifier='pause')
    value = input_([play, pause])
    product = policy(value)
    plans = product.validate_group(value)
    assert plans[1]['depends_on'] == ['play'] and plans[1]['prepared_command'] is None
    with pytest.raises(ProductPolicyError) as pending:
        product.prepare_product_action(value, pause, {})
    assert pending.value.code == 'pending_receipt'
    actual = observation(value, play, receipt(plans[0]['prepared_command'], position_seconds=3))
    prepared = product.prepare_product_action(value, pause, {'play': actual})['prepared_command']
    assert prepared['expected_state_version'] == 1
    assert prepared['expected']['content_id'] == 'catalog:b' and prepared['expected']['position_seconds'] == 3
    assert prepared['expected']['status'] == 'paused'
    assert value['start']['context']['cockpit']['media']['version'] == 0


@pytest.mark.parametrize('updates', [{'content_id': 'wrong'}, {'version': 0}, {'status': 'stopped'}])
def test_actual_receipts_must_match_prepared_content_status_and_version(updates):
    item = request(arguments={'action': 'play', 'content_id': 'catalog:b'})
    value = input_([item])
    product = policy(value)
    command = product.validate_group(value)[0]['prepared_command']
    with pytest.raises(CockpitError):
        product.validate_observations(value, {'call': observation(value, item, receipt(command, **updates))})


def test_failed_and_unknown_receipts_do_not_advance_product_state():
    item = request()
    value = input_([item])
    for status in ('rejected', 'unknown'):
        actual = observation(value, item, {'ok': True, 'state': {'status': 'paused'}}, status)
        assert product_context(value, {'call': actual}) == value['start']['context']


def test_media_search_receipts_supply_catalog_facts_but_model_text_does_not():
    item = request(arguments={'action': 'play', 'content_id': 'catalog:new'})
    value = input_([item])
    value['checkpoint']['messages'] = [
        {'role': 'assistant', 'tool_calls': [{'id': 'search', 'function': {
            'name': 'search_media', 'arguments': json.dumps({'kind': 'music', 'query': '曲子'})}}]},
        {'role': 'tool', 'tool_call_id': 'search', 'content': json.dumps({'ok': True, 'items': [media('catalog:new')]})},
    ]
    assert prepare_product_action(value, item)['prepared_command']['expected']['content_id'] == 'catalog:new'
    value['checkpoint']['messages'] = [{'role': 'assistant', 'content': json.dumps({'items': [media('catalog:new')]})}]
    with pytest.raises(CockpitError):
        prepare_product_action(value, item)


def test_area_keeps_grounded_tokens_and_changes_only_execution_parameters():
    item = request('search_places', {'query': '停车场', 'area': '巴黎 中国'})
    value = input_([item])
    decision = policy(value).validate_group(value)[0]
    assert decision['execution_arguments'] == {'query': '停车场', 'near': '巴黎'}
    assert item['arguments']['area'] == '巴黎 中国'
    with pytest.raises(ProductPolicyError):
        normalize_search_arguments({'area': '无来源城市'}, ['巴黎'])


def test_task_selection_infers_only_unique_target_and_binds_mutation_revision():
    tasks = input_()['start']['context']['tasks']
    assert normalize_task_arguments({'action': 'cancel'}, tasks) == {'action': 'cancel', 'task_id': 'task', 'expected_revision': 2}
    tasks.append({'id': 'other', 'revision': 1, 'goal': '别的查询', 'status': 'queued'})
    with pytest.raises(ProductPolicyError) as ambiguous:
        normalize_task_arguments({'action': 'cancel'}, tasks)
    assert ambiguous.value.result['needs_clarification'] is True
    selected = normalize_task_arguments({'action': 'get', 'task_ids': ['other', 'task']}, tasks)
    assert selected['task_ids'] == ['other', 'task']
    for invalid in ({'action': 'get', 'task_ids': ['task', 'foreign']},
                    {'action': 'cancel', 'task_ids': ['task']}, {'action': 'reply', 'task_id': 'task', 'text': ' '}):
        with pytest.raises(ProductPolicyError):
            normalize_task_arguments(invalid, tasks)


def test_all_capabilities_and_whole_group_conflicts_remain_enforced():
    first = request('delegate_vehicle_control', {'skill_ids': ['navigation']}, 'planner')
    second = request('manage_navigation', {'action': 'cancel'}, 'direct')
    value = input_([first, second])
    with pytest.raises(ProductPolicyError) as conflict:
        policy(value).validate_group(value)
    assert conflict.value.code == 'planned_vehicle_group_conflict'
    value = input_([request()])
    product = policy(value)
    value['start']['context']['capabilities']['tools'][0]['available'] = False
    with pytest.raises(ProductPolicyError) as unavailable:
        product.validate_group(value)
    assert unavailable.value.code == 'unsupported_capability'


def test_hot_media_aliases_preserve_real_player_semantics():
    volume = request('set_volume', {'volume': 42})
    value = input_([volume])
    prepared = policy(value).validate_group(value)[0]
    assert prepared['domain'] == 'media' and prepared['execution_arguments'] == {'action': 'volume', 'volume': 42}
    playback = request('control_playback', {'action': 'play'})
    value = input_([playback])
    assert policy(value).validate_group(value)[0]['execution_arguments'] == {'action': 'resume'}


@pytest.mark.parametrize('name,arguments', [
    ('control_media', {'action': 'seek'}), ('control_media', {'action': 'volume'}),
    ('manage_navigation', {'action': 'start'}), ('manage_navigation', {'action': 'remove_waypoint'}),
])
def test_later_action_missing_required_argument_rejects_whole_group(name, arguments):
    first = request(name, {'action': 'pause'} if name == 'control_media' else {'action': 'cancel'}, 'first')
    second = request(name, arguments, 'second')
    value = input_([first, second])
    with pytest.raises(ProductPolicyError) as invalid:
        policy(value).validate_group(value)
    assert invalid.value.code == 'invalid_action'


def test_missing_place_reference_cannot_select_a_single_result_without_provider_alias():
    assert resolve_place_reference(None, {'provider:a': {'name': 'a'}}) is None
    value = input_([request('manage_navigation', {'action': 'start'})])
    value['start']['context']['places'] = [{'place_ref': 'provider:a', 'name': 'a', 'provider': 'provider',
                                          'source_turn_id': 'older', 'presented_to_user': True}]
    with pytest.raises(ProductPolicyError):
        policy(value).validate_group(value)


def test_status_query_can_supply_first_actual_state_before_dependent_control():
    query = request(arguments={'action': 'get_status'}, identifier='query')
    pause = request(identifier='pause')
    value = input_([query, pause])
    actual = value['start']['context']['cockpit'].pop('media')
    product = policy(value)
    plans = product.validate_group(value)
    assert plans[1]['depends_on'] == ['query'] and plans[1]['prepared_command'] is None
    received = observation(value, query, {'ok': True, 'state': actual})
    prepared = product.prepare_product_action(value, pause, {'query': received})
    assert prepared['prepared_command']['expected_state_version'] == 0
    assert prepared['prepared_command']['expected']['status'] == 'paused'


def test_control_without_execution_confirmation_is_not_applied_as_fact():
    item = request()
    value = input_([item])
    product = policy(value)
    actual = receipt(product.validate_group(value)[0]['prepared_command'])
    actual.pop('executed')
    with pytest.raises(ProductPolicyError) as unconfirmed:
        product.validate_observations(value, {'call': observation(value, item, actual)})
    assert unconfirmed.value.code == 'execution_not_confirmed'


def test_actual_vehicle_snapshot_updates_later_product_guards():
    value = input_()
    value['start']['context']['cockpit']['media']['kind'] = 'video'
    value['checkpoint']['messages'] = [
        {'role': 'assistant', 'tool_calls': [{'id': 'control', 'function': {
            'name': 'delegate_vehicle_control', 'arguments': json.dumps({'skill_ids': ['driving']})}}]},
        {'role': 'tool', 'tool_call_id': 'control', 'content': json.dumps({
            'ok': True, 'executed': True, 'vehicle_state': {'_meta': {'speed_kph': 30}}})},
    ]
    with pytest.raises(ProductPolicyError) as moving:
        prepare_product_action(value, request(arguments={'action': 'resume'}))
    assert moving.value.code == 'video_driving'


def test_binding_domain_survives_for_resource_and_hot_vehicle_routes():
    item = request('get_weather', {'location': '巴黎', 'date': 'today'})
    value = input_([item])
    product = policy(value)
    product.bindings['get_weather']['domain'] = 'weather'
    assert product.validate_group(value)[0]['domain'] == 'weather'


@pytest.mark.parametrize('items', [[None], [{'content_id': 'a', 'kind': 'video', 'title': 'a', 'provider': 'cp'}],
                                   [media(), media()]])
def test_malformed_or_mismatched_catalog_receipt_is_rejected(items):
    query = request('search_media', {'kind': 'music', 'query': '曲子'})
    value = input_([query])
    with pytest.raises((ProductPolicyError, CockpitError)):
        product_context(value, {'call': observation(value, query, {'ok': True, 'items': items})})


@pytest.mark.parametrize('update', [{'id': 'foreign'}, {'revision': '3'}, {'status': 'invented'}])
def test_task_receipts_cannot_introduce_unselected_or_malformed_task_facts(update):
    query = request('manage_task', {'action': 'get', 'task_id': 'task'})
    value = input_([query])
    actual = {**value['start']['context']['tasks'][0], **update}
    with pytest.raises(ProductPolicyError):
        product_context(value, {'call': observation(value, query, {'ok': True, 'task': actual})})


async def test_demo_replaying_actual_content_can_resolve_missing_descriptive_metadata():
    from super_xd.demo.devices.cockpit import CockpitServices
    from super_xd.demo.resources.media import LocalMediaCatalog
    from super_xd.demo.devices.preparation import prepare_media
    item = {**media(), 'source_url': 'https://media.example.test/a.mp3'}
    services = CockpitServices(LocalMediaCatalog([item]))
    state = initial_cockpit_state()['media']
    state.update(content_id=item['content_id'], kind='music', status='paused')
    prepared = await prepare_media(services, 'play', {'content_id': item['content_id']}, state, {})
    assert prepared['expected']['title'] == item['title']
    assert prepared['arguments']['source_url'] == item['source_url']


def test_closed_status_cannot_retain_old_content_or_accept_ambiguous_queue():
    from super_xd.nlp.policy.cockpit import observed_media_state
    previous = input_()['start']['context']['cockpit']['media']
    actual = observed_media_state(previous, {'state_version': 1, 'state': {'status': 'closed'}})
    assert actual['content_id'] is None and actual['desired_status'] == 'closed'
    with pytest.raises(CockpitError):
        observed_media_state(previous, {'state_version': 1, 'state': {**previous, 'queue': [media(), media()]}})
