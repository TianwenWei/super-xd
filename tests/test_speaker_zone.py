import asyncio
import json

import pytest

from backend.session import Turn
from backend.harness.vehicle_planner import VehicleControlPlanner
from backend.catalog import TOOLS
from backend.demo.vehicle_simulator import initial_vehicle_state
from backend.speaker import speaker_context
from backend.harness.task_runner import TaskRunner
from backend.harness.contracts import ToolRequest
from tests.test_session import make_actor, wait_for, call
from tests.vehicle_fakes import FakeVehiclePlanner, plan_response


@pytest.mark.asyncio
@pytest.mark.parametrize('seat,zone,row,mirror', [
    ('driver', 'driver', 'front', 'left'),
    ('front_passenger', 'front_passenger', 'front', 'right'),
    ('rear_left', 'rear', 'rear', 'left'),
    ('rear_right', 'rear', 'rear', 'right'),
])
async def test_speaker_system_snapshot_and_shared_zone_mapping(make_actor, seat, zone, row, mirror):
    actor, q, ark = await make_actor([])
    await actor.post({'type': 'speaker.set', 'seat': seat})
    await actor.queue.join()
    assert actor.snapshot()['speaker']['seat'] == seat
    turn = Turn('test', '你好', actor.persona, 1)
    start = actor.nlp_start_request(turn)
    messages = actor.nlp.compile_start(start).messages
    snapshot = next(json.loads(item['content'].splitlines()[-1]) for item in messages
                    if item['role'] == 'system' and item['content'].startswith('当前会话快照'))
    speaker = snapshot['speaker']
    assert speaker['audio_zone'] == seat
    assert speaker['default_targets'] == {'seat': seat, 'window': seat, 'door': seat,
                                          'temperature_zone': zone, 'interior_light_zone': row, 'mirror': mirror}
    registered = await actor.nlp.task_service.handle(start, ToolRequest(
        invocation_id='seat-task-' + seat, tool_call_id='seat-task', idempotency_key='seat-task-' + seat,
        name='delegate_task', arguments={'goal': '车辆设置建议'}))
    task = registered['task']
    assert task['context']['speaker']['seat'] == seat
    assert task['context']['speaker']['default_targets'] == speaker['default_targets']
    assert speaker['control_defaults']['set_reading_light'] == {
        'tool': 'delegate_vehicle_control', 'skill_id': 'lights', 'arguments': {'seat': seat}}
    assert speaker['control_defaults']['set_temperature']['arguments'] == {'zone': zone}
    assert speaker['control_defaults']['set_interior_light']['arguments'] == {'zone': row}
    worker = object.__new__(TaskRunner)
    thinker_messages = worker._messages(task)
    assert any(item['role'] == 'system' and '"seat": "' + seat + '"' in item['content'] for item in thinker_messages)


@pytest.mark.asyncio
async def test_invalid_speaker_does_not_change_session_or_start_turn(make_actor):
    actor, q, ark = await make_actor([])
    await actor.post({'type': 'speaker.set', 'seat': 'all'})
    error, _ = await wait_for(q, 'error')
    assert actor.snapshot()['speaker']['seat'] == 'driver'
    await actor.post({'type': 'input', 'text': '开窗', 'speaker_seat': 'unknown'})
    await wait_for(q, 'error')
    assert not actor.turns and not ark.requests


@pytest.mark.asyncio
async def test_speaker_change_does_not_retarget_in_flight_planner(make_actor):
    planner = FakeVehiclePlanner(plan_response(('set_reading_light', {'seat': 'rear_left', 'enabled': True})))
    actor, q, ark = await make_actor([(.08, {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['lights']})]})], planner)
    await actor.post({'type': 'speaker.set', 'seat': 'rear_left'})
    await actor.post({'type': 'input', 'text': '打开阅读灯'})
    await actor.queue.join()
    await actor.post({'type': 'speaker.set', 'seat': 'front_passenger'})
    done, _ = await wait_for(q, 'stream.done', actor)
    assert planner.requests[0]['speaker_seat'] == 'rear_left'
    assert actor.current.speaker_seat == 'rear_left'
    assert actor.snapshot()['speaker']['seat'] == 'front_passenger'
    assert actor.vehicle['lights']['reading']['rear_left'] is True
    assert actor.vehicle['lights']['reading']['front_passenger'] is False


@pytest.mark.asyncio
async def test_input_metadata_overrides_one_turn_without_changing_default(make_actor):
    actor, q, ark = await make_actor([{'content': '第一轮回答。'}, {'content': '第二轮回答。'}])
    await actor.post({'type': 'speaker.set', 'seat': 'front_passenger'})
    await actor.post({'type': 'input', 'text': '你好', 'speaker_seat': 'rear_right'})
    await wait_for(q, 'stream.done')
    first = actor.current
    assert first.speaker_seat == 'rear_right'
    assert first.start_request.speaker_seat == 'rear_right'
    await actor.post({'type': 'input', 'text': '继续'})
    await wait_for(q, 'stream.done')
    assert actor.current.speaker_seat == 'front_passenger'
    assert actor.turns[0].speaker_seat == 'rear_right'


@pytest.mark.asyncio
async def test_planner_receives_speaker_in_system_and_request_stays_exact():
    class Capture:
        async def complete(self, messages, tools=None, **options):
            self.messages = messages
            raise RuntimeError('captured')
    ark = Capture()
    await VehicleControlPlanner(ark).plan('打开阅读灯', initial_vehicle_state(), ['lights'], speaker_seat='rear_right')
    assert any(item['role'] == 'system' and 'rear_right' in item['content'] for item in ark.messages)
    data = json.loads(next(item['content'] for item in ark.messages if item['role'] == 'user'))
    assert data['request_text'] == '打开阅读灯' and data['speaker']['seat'] == 'rear_right'


@pytest.mark.parametrize('seat', ['driver', 'front_passenger', 'rear_left', 'rear_right'])
def test_all_positional_mutation_parameters_have_valid_defaults(seat):
    controls = speaker_context(seat)['control_defaults']
    locations = {'all', 'front', 'rear', 'left', 'right', 'driver', 'front_passenger', 'rear_left', 'rear_right'}
    for name, spec in TOOLS.items():
        if spec.state_mapping.get('operation') == 'read':
            continue
        for field, schema in spec.parameters['properties'].items():
            values = schema.get('enum', [])
            if values and set(values) <= locations:
                assert controls[name]['arguments'][field] in values
