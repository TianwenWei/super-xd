"""Terminal planner failures and success text are grounded in their receipts."""
from copy import deepcopy

import pytest

from super_xd.demo.devices.simulator import execute_tool, initial_vehicle_state
from super_xd.nlp.prompts.speech import receipt_detail
from super_xd.nlp.prompts.templates import load_prompt
from tests.support.session import make_actor, wait_for
from tests.support.vehicle_delegation import delegate
from tests.support.vehicle import FakeVehiclePlanner


@pytest.mark.parametrize('name,arguments,expected', [
    ('set_mirror_fold', {'folded': True}, '左右后视镜已折叠。'),
    ('set_mirror_fold', {'folded': False}, '左右后视镜已展开。'),
    ('set_window_child_lock', {'enabled': True}, '后排车窗童锁已开启。'),
    ('set_window_child_lock', {'enabled': False}, '后排车窗童锁已关闭。'),
    ('set_rain_sensitivity', {'level': 4}, '自动雨刷雨量灵敏度设为四档。'),
])
def test_control_receipt_uses_specific_actual_value(name, arguments, expected):
    state = initial_vehicle_state()
    receipt = execute_tool(name, arguments, state)
    assert receipt['ok']
    # Deliberately inconsistent caller args demonstrate that feedback selects
    # the value in the real success receipt, not the stale argument copy.
    stale = {'folded': not arguments['folded']} if 'folded' in arguments else (
        {'enabled': not arguments['enabled']} if 'enabled' in arguments else {'level': 1})
    assert receipt_detail(name, stale, receipt) == expected
    assert '模拟车控' not in expected and '超过5' not in expected


@pytest.mark.parametrize('reason,code,expected_code', [
    ('planner_model_step_limit', None, 'planner_model_step_limit'),
    ('HTTP 403 (content_blocked)', 'ContentBlocked', 'ContentBlocked'),
    ('network timeout', None, 'vehicle_planning_failed'),
    ('invalid_model_message', None, 'invalid_model_message'),
])
async def test_empty_builtin_failure_preserves_original_cause_without_schema_mask(make_actor, reason, code, expected_code):
    failure = {'status': 'refused', 'ok': False, 'reason': reason, 'question': '',
               'intents': [], 'actions': [], 'keep_paths': [], 'model_calls': 4,
               'plan_call_id': 'actual-plan-call', 'model_call_id': 'actual-pro-call'}
    if code:
        failure['code'] = code
    actor, queue, _ = await make_actor([{'tool_calls': [delegate(['mirrors'])]},
        {'content': '规划没有完成，动作没有下发。'}], FakeVehiclePlanner(failure))
    before = deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '折叠后视镜。'})
    done, events = await wait_for(queue, 'stream.done')
    result = actor.current.tool_results['delegate']
    assert result['code'] == expected_code and result['reason'] == reason
    assert result['ok'] is False and result['executed'] is False
    assert result['receipts'] == [] and result['plan']['intents'] == []
    assert actor.vehicle == before and not any(e['type'] == 'vehicle.command' for e in events)
    assert '规划没有完成' in done['text'] and '动作没有下发' in done['text']
    assert 'minItems' not in result['reason'] and result['code'] != 'vehicle_plan_invalid'
    assert done['metrics']['error'] is None  # The model explained an explicit planner failure.


@pytest.mark.parametrize('status,actions,intents', [
    ('planned', [{'id': 'unsafe', 'intent_id': 'missing', 'name': 'set_mirror_fold',
                  'arguments': {'folded': True}, 'depends_on': []}], []),
    ('refused', [{'id': 'unsafe', 'intent_id': 'missing', 'name': 'set_mirror_fold',
                 'arguments': {'folded': True}, 'depends_on': []}], []),
    ('refused', [], [{'id': 'forged', 'status': 'completed'}]),
])
async def test_failure_handling_cannot_bypass_action_or_intent_validation(make_actor, status, actions, intents):
    invalid = {'status': status, 'ok': False, 'reason': 'planner_model_step_limit',
               'question': '', 'actions': actions, 'intents': intents, 'keep_paths': []}
    actor, queue, _ = await make_actor([{'tool_calls': [delegate(['mirrors'])]},
        {'content': '计划未通过核实，没有执行。'}], FakeVehiclePlanner(invalid))
    await actor.post({'type': 'input', 'text': '折叠后视镜。'})
    _, events = await wait_for(queue, 'stream.done')
    assert actor.current.tool_results['delegate']['code'] == 'vehicle_plan_invalid'
    assert actor.vehicle['mirrors']['folded'] is False
    assert not any(e['type'] == 'vehicle.command' for e in events)


async def test_dependency_and_authorized_prerequisite_instruction_reaches_talker(make_actor):
    """Prompt plumbing only; fake output does not prove live routing quality."""
    actor, queue, ark = await make_actor([{'content': '离线提示传递检查。'}])
    actor.vehicle['windows']['child_lock'] = True
    await actor.post({'type': 'input', 'text': '先解除后排车窗童锁，再把左后窗开到30%。'})
    await wait_for(queue, 'stream.done')
    instructions = ark.requests[0]['messages'][0]['content']
    assert instructions.startswith(load_prompt('common'))
    assert 'delegate_vehicle_control' in instructions
    assert '多个车控存在执行依赖' in instructions
    assert '整句只调用一次' in instructions
    assert '不先执行其中的热车控' in instructions
    assert actor.vehicle['windows']['child_lock'] is True
