"""Production contracts must neither import nor execute Demo device behavior."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from super_xd.nlp.catalog.vehicle import action_write_paths
from super_xd.nlp.policy.vehicle import validate_vehicle_action_keep, validate_vehicle_plan
from tests.support.vehicle_planner import action, intent, plan


def test_production_entrypoint_and_tool_construction_exclude_demo_and_retrieval_implementations():
    code = '''
import sys
import super_xd.nlp.api.app
import super_xd.nlp.assembly
from super_xd.nlp.catalog.vehicle import TOOLS
from super_xd.nlp.orchestration.talker import talker_tools
from super_xd.nlp.prompts.config import validate_all
validate_all()
assert len(TOOLS) == 100 and len(talker_tools()) == 26
forbidden = {'numpy', 'sqlite3', 'super_xd.demo.resources.manual_search', 'super_xd.demo.resources.web_search',
             'super_xd.demo.session.actor', 'super_xd.demo.tasks.worker', 'super_xd.demo.tasks.database'}
loaded = forbidden.intersection(sys.modules) | {name for name in sys.modules if name.startswith('super_xd.demo')}
assert not loaded, sorted(loaded)
assert not hasattr(sys.modules['super_xd.nlp.catalog.vehicle'], 'execute_tool')
assert not hasattr(sys.modules['super_xd.nlp.catalog.vehicle'], 'initial_vehicle_state')
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('state', [{}, {'_meta': {'speed_kph': 90}},
                                  {'tailgate': {'open': False}, '_meta': {'gear': 'D'}}])
def test_plan_does_not_require_or_invent_demo_vehicle_defaults(state):
    query = '打开尾门。'
    value = plan([intent('tailgate', query, actions=['tailgate'])],
                 [action('tailgate', 'tailgate', 'set_tailgate_open', {'open': True})])
    value['loaded_skills'] = ['tailgate']
    before = deepcopy(state)
    checked = validate_vehicle_plan(value, query, state)
    assert checked['ok'] and not checked['executed'] and state == before
    assert checked['validation']['executor_validation_required'] is True
    assert 'simulation' not in checked and 'final_state' not in checked


def test_declared_effects_account_for_sync_without_simulating_a_value():
    state = {'climate': {'sync_zones': True, 'temperatures': {'driver': 22, 'rear': 21}}}
    before = deepcopy(state)
    paths = action_write_paths('set_temperature', {'zone': 'driver', 'temperature': 22}, state)
    assert 'climate.temperatures' in paths and 'climate.power' in paths
    assert action_write_paths('get_climate_state', {}, state) == []
    assert state == before


async def test_cold_plan_uses_real_device_acceptance_and_stops_after_rejected_receipt():
    from super_xd.nlp.orchestration.vehicle_planner import VehicleControlPlanner
    from tests.support.orchestration import (CompleteModel, StreamModel, call, kernel, message,
                                    outputs, planner_submission, request, resume)

    start = request(tools=[('set_mirror_heating', {}, 'control', 'device'),
                           ('set_mirror_fold', {}, 'control', 'device'),
                           ('set_mirror_angle', {}, 'control', 'device')])
    # These Demo guards would reject the plan and later the fold before handoff.
    start.context.vehicle.state['_meta']['speed_kph'] = 90
    submitted = planner_submission(start.user_text)
    body = json.loads(submitted['tool_calls'][0]['function']['arguments'])
    body['intents'][0]['action_ids'].append('angle')
    body['actions'].append(action('angle', 'intent', 'set_mirror_angle',
                                  {'mirror': 'left', 'horizontal': 2, 'vertical': 3}, ['fold']))
    submitted = message('submit_vehicle_plan', body)
    talker = StreamModel({'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['mirrors']})]},
                         {'content': '加热已开启，折叠被车端拒绝，未继续调节角度。'})
    service = kernel(talker, start, planner=VehicleControlPlanner(CompleteModel(submitted)))
    first = (await outputs(service.start(start)))[-1]
    assert first.type == 'run.suspended'
    assert [item.request.name for item in first.requests] == ['set_mirror_heating']
    actual = deepcopy(start.context.vehicle.state)
    actual['mirrors']['heating'] = True
    second = (await outputs(resume(service, first,
        {'ok': True, 'executed': True, 'vehicle_state': actual})))[-1]
    assert [item.request.name for item in second.requests] == ['set_mirror_fold']
    finished = await outputs(resume(service, second,
        {'ok': False, 'executed': False, 'reason': 'Device policy refused folding', 'code': 'vehicle_moving'},
        status='rejected'))
    assert not any(event.requests for event in finished)
    group = finished[-1].checkpoint.orchestration.groups[-1]
    result = next(iter(group.results.values())).result
    assert result['ok'] is False and result['remaining_actions_not_executed'] is True
    assert len(result['receipts']) == 2
    assert {**start.context.vehicle.state, **group.vehicle_state} == actual


@pytest.mark.parametrize('name,arguments', [
    ('set_navigation_destination', {'destination': '巴黎'}), ('cancel_navigation', {}),
])
def test_navigation_effects_protect_the_accepted_destination_place(name, arguments):
    state = {'navigation': {'destination_place': {'place_id': 'accepted:place'}}}
    with pytest.raises(ValueError, match='keep_path_write_conflict'):
        validate_vehicle_action_keep(name, arguments, state, ['navigation.destination_place'])
