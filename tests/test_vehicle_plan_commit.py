"""Offline commit-time races; model doubles do not count as live acceptance."""

from copy import deepcopy
from backend.demo.vehicle_simulator import initial_vehicle_state
from test_session import make_actor, wait_for
from test_vehicle_delegation import delegate, split_plan
from vehicle_fakes import FakeVehiclePlanner, plan_response


async def test_late_prior_sync_cannot_change_current_plan_preserved_temperature(make_actor):
    original = '主驾温度设为24度，副驾温度保持22度，然后打开后视镜加热。'
    plan = split_plan(original, [
        {'text': '主驾温度设为24度', 'operations': [('set_temperature', {'zone': 'driver', 'temperature': 24})]},
        {'text': '副驾温度保持22度', 'status': 'keep', 'keep_paths': ['climate.temperatures.front_passenger']},
        {'text': '然后打开后视镜加热', 'operations': [('set_mirror_heating', {'enabled': True})]},
    ])
    plan['actions'][1]['depends_on'] = [plan['actions'][0]['id']]
    planner = FakeVehiclePlanner(plan_response(('sync_climate_zones', {'enabled': True})), plan)
    actor, queue, _ = await make_actor([
        {'tool_calls': [delegate(['climate'], 'old')]},
        {'tool_calls': [delegate(['climate', 'mirrors'], 'current')]},
    ], planner)
    await actor.post({'type': 'input', 'text': '开启温区同步。'})
    old, _ = await wait_for(queue, 'vehicle.command')
    await actor.post({'type': 'input', 'text': original})
    current, _ = await wait_for(queue, 'vehicle.command')
    assert current['name'] == 'set_temperature' and not actor.vehicle['climate']['sync_zones']
    receipt = await actor.simulator.execute(old['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': old['command_id'], 'receipt_id': receipt['receipt_id']})
    receipt = await actor.simulator.execute(current['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': current['command_id'], 'receipt_id': receipt['receipt_id']})
    done, events = await wait_for(queue, 'stream.done', actor)
    ledger = actor.current.tool_results['current']
    assert actor.vehicle['climate']['sync_zones'] is False
    assert actor.vehicle['climate']['temperatures'] == {'driver': 24, 'front_passenger': 22, 'rear': 22}
    assert actor.vehicle['mirrors']['heating'] is True
    assert ledger['ok'] is True and ledger['executed'] is True
    assert [item['status'] for item in ledger['receipts']] == ['completed', 'completed']
    assert not any(e['type'] == 'vehicle.result' and e['command_id'] == old['command_id'] for e in events)


async def test_baseline_change_after_dispatch_is_not_reported_as_kept(make_actor):
    original = '副驾车窗保持关闭，打开后视镜加热。'
    plan = split_plan(original, [
        {'text': '副驾车窗保持关闭', 'status': 'keep', 'keep_paths': ['windows.positions.front_passenger']},
        {'text': '打开后视镜加热', 'operations': [('set_mirror_heating', {'enabled': True})]},
    ])
    actor, queue, _ = await make_actor([{'tool_calls': [delegate(['windows', 'mirrors'])]},
        {'content': '副驾车窗发生了变化，后视镜加热没有执行。'}], FakeVehiclePlanner(plan))
    await actor.post({'type': 'input', 'text': original})
    command, _ = await wait_for(queue, 'vehicle.command')
    actor.vehicle['windows']['positions']['front_passenger'] = 37  # A physical-state update after dispatch.
    actor.simulator.state = deepcopy(actor.vehicle)
    receipt = await actor.simulator.execute(command['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': command['command_id'], 'receipt_id': receipt['receipt_id']})
    done, _ = await wait_for(queue, 'stream.done')
    ledger = actor.current.tool_results['delegate']
    assert ledger['ok'] is False and ledger['executed'] is False
    assert ledger['receipts'][0]['result']['reason_code'] == 'keep_conflict'
    assert ledger['remaining_actions_not_executed'] is True
    assert ledger['receipts'][0]['result']['state']['windows']['positions']['front_passenger'] == 37
    assert actor.vehicle['mirrors']['heating'] is False and actor.vehicle['windows']['positions']['front_passenger'] == 37
    assert '要求保持的设置未调整' not in done['text']
    assert '发生了变化' in done['text']


async def test_dynamic_same_value_write_to_keep_path_is_still_rejected_at_ack(make_actor):
    original = '主驾温度设为24度，副驾温度保持24度。'
    plan = split_plan(original, [
        {'text': '主驾温度设为24度', 'operations': [('set_temperature', {'zone': 'driver', 'temperature': 24})]},
        {'text': '副驾温度保持24度', 'status': 'keep', 'keep_paths': ['climate.temperatures.front_passenger']},
    ])
    actor, queue, _ = await make_actor([{'tool_calls': [delegate(['climate'])]}, {'content': '温区同步会影响副驾温度，本动作没有执行。'}], FakeVehiclePlanner(plan))
    actor.vehicle['climate']['temperatures'] = {'driver': 24, 'front_passenger': 24, 'rear': 24}
    actor.simulator.state = deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': original})
    command, _ = await wait_for(queue, 'vehicle.command')
    actor.vehicle['climate']['sync_zones'] = True
    actor.simulator.state = deepcopy(actor.vehicle)
    receipt = await actor.simulator.execute(command['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': command['command_id'], 'receipt_id': receipt['receipt_id']})
    await wait_for(queue, 'stream.done')
    ledger = actor.current.tool_results['delegate']
    assert ledger['ok'] is False and ledger['executed'] is False
    assert ledger['receipts'][0]['result']['reason_code'] == 'keep_conflict'
    assert all(value == 24 for value in actor.vehicle['climate']['temperatures'].values())
    assert actor.vehicle['climate']['power'] == initial_vehicle_state()['climate']['power']


async def test_keep_failure_after_first_real_success_preserves_partial_receipt(make_actor):
    original = '音量设为35，副驾车窗保持关闭，打开后视镜加热。'
    plan = split_plan(original, [
        {'text': '音量设为35', 'operations': [('set_volume', {'volume': 35})]},
        {'text': '副驾车窗保持关闭', 'status': 'keep', 'keep_paths': ['windows.positions.front_passenger']},
        {'text': '打开后视镜加热', 'operations': [('set_mirror_heating', {'enabled': True})]},
    ])
    plan['actions'][1]['depends_on'] = [plan['actions'][0]['id']]
    actor, queue, _ = await make_actor([{'tool_calls': [delegate(['media', 'windows', 'mirrors'])]},
        {'content': '音量已调到百分之35。副驾车窗发生变化，后视镜加热本动作没有执行。'}], FakeVehiclePlanner(plan))
    await actor.post({'type': 'input', 'text': original})
    first, _ = await wait_for(queue, 'vehicle.command')
    receipt = await actor.simulator.execute(first['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': first['command_id'], 'receipt_id': receipt['receipt_id']})
    second, _ = await wait_for(queue, 'vehicle.command')
    actor.vehicle['windows']['positions']['front_passenger'] = 37
    actor.simulator.state = deepcopy(actor.vehicle)
    receipt = await actor.simulator.execute(second['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': second['command_id'], 'receipt_id': receipt['receipt_id']})
    done, _ = await wait_for(queue, 'stream.done')
    ledger = actor.current.tool_results['delegate']
    assert ledger['ok'] is False and ledger['executed'] is True
    assert [item['result']['executed'] for item in ledger['receipts']] == [True, False]
    assert ledger['remaining_actions_not_executed'] is True
    assert actor.vehicle['media']['volume'] == 35 and actor.vehicle['mirrors']['heating'] is False
    assert '音量' in done['text'] and '百分之35' in done['text']
    assert '本动作没有执行' in done['text'] and '要求保持的设置未调整' not in done['text']
