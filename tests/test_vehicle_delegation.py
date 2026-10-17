"""Offline actor acceptance for the explicit Mini → Pro vehicle-plan boundary."""

import asyncio
from copy import deepcopy
import json

import pytest

from backend.catalog import HOT_TOOL_NAMES, COLD_TOOL_NAMES, TOOLS
from test_session import call, make_actor, wait_for
from vehicle_fakes import FakeVehiclePlanner, planned, plan_response, expected_talker_tool_names


def delegate(skills=(), id="delegate"):
    return call("delegate_vehicle_control", {"skill_ids": list(skills)}, id)


def split_plan(original, definitions):
    """Explicit source spans and intent/action links, without intent inference."""
    intents, actions, keeps = [], [], []
    for index, definition in enumerate(definitions, 1):
        text = definition["text"]
        start = original.index(text)
        identity = f"intent-{index}"
        action_ids = []
        for operation in definition.get("operations", []):
            action_id = f"action-{len(actions) + 1}"
            action_ids.append(action_id)
            actions.append({"id": action_id, "intent_id": identity, "name": operation[0],
                            "arguments": deepcopy(operation[1]), "depends_on": []})
        paths = list(definition.get("keep_paths", []))
        keeps.extend(paths)
        intents.append({"id": identity, "source_text": text, "text": text,
                        "source_start": start, "source_end": start + len(text),
                        "status": definition.get("status", "planned"),
                        "action_ids": action_ids, "keep_paths": paths,
                        "completed_operation_ids": definition.get("completed_operation_ids", []),
                        "question": "", "reason": definition.get("reason", "")})
    return {"ok": True, "status": "planned", "intents": intents, "actions": actions,
            "keep_paths": keeps, "question": "", "reason": "", "plan_call_id": "fake-multi-plan",
            "loaded_skills": list(dict.fromkeys(TOOLS[a["name"]].skill_id for a in actions)),
            "executed": False}


async def test_fixed_vehicle_talker_schema_cannot_expand_after_any_cold_plan(make_actor):
    planner = FakeVehiclePlanner(plan_response(("set_mirror_heating", {"enabled": True})),
                                 plan_response(("set_mirror_fold", {"folded": True})))
    actor, q, ark = await make_actor([
        {"tool_calls": [delegate(["mirrors"], "heat")]},
        {"tool_calls": [call("set_mirror_fold", {"folded": True}, "cold-direct")]},
        {"tool_calls": [delegate(["mirrors"], "fold")]},
    ], planner)
    await actor.post({"type": "input", "text": "打开后视镜加热。"})
    await wait_for(q, "stream.done", actor)
    await actor.post({"type": "input", "text": "把后视镜折叠。"})
    denied, events = await wait_for(q, "stream.done", actor)
    assert denied['metrics']['error'] == 'unknown_tool'
    assert not any(event['type'] == 'vehicle.command' for event in events)
    assert actor.current.checkpoint.run.pending == []
    await actor.post({"type": "input", "text": "请通过车控规划把后视镜折叠。"})
    done, events = await wait_for(q, "stream.done", actor)
    assert [e["name"] for e in events if e["type"] == "vehicle.command"] == ["set_mirror_fold"]
    names = [[tool["function"]["name"] for tool in request["tools"]] for request in ark.requests]
    assert all(row == expected_talker_tool_names() and row[:15] == HOT_TOOL_NAMES for row in names)
    assert all(set(row).isdisjoint(COLD_TOOL_NAMES) and "load_skill" not in row for row in names)
    assert len(names) == 3 and len(planner.requests) == 2
    assert done["metrics"]["model_calls"] == 2

async def test_legacy_load_skill_is_rejected_instead_of_promoting_cold_schemas(make_actor):
    planner = FakeVehiclePlanner(plan_response(("set_mirror_heating", {"enabled": True})))
    actor, q, ark = await make_actor([
        {"tool_calls": [call("load_skill", {"skill_id": "mirrors"}, "legacy")]},
        {"tool_calls": [delegate(["mirrors"])]},
    ], planner)
    await actor.post({"type": "input", "text": "打开后视镜加热。"})
    denied, events = await wait_for(q, "stream.done", actor)
    assert denied['metrics']['error'] == 'unknown_tool'
    assert not any(event['type'] == 'vehicle.command' for event in events)
    await actor.post({"type": "input", "text": "通过车控规划打开后视镜加热。"})
    _, events = await wait_for(q, "stream.done", actor)
    assert [e["name"] for e in events if e["type"] == "vehicle.command"] == ["set_mirror_heating"]
    assert all([t["function"]["name"] for t in request["tools"]] == expected_talker_tool_names() for request in ark.requests)

async def test_planner_receives_original_request_latest_snapshot_and_actual_history(make_actor):
    planner = FakeVehiclePlanner(plan_response(("set_mirror_heating", {"enabled": True})))
    actor, q, _ = await make_actor([
        {"content": "准备好了。"}, {"tool_calls": [delegate([])]},
    ], planner)
    await actor.post({"type": "input", "text": "你好，准备出发。"})
    await wait_for(q, "stream.done")
    actor.vehicle["windows"]["positions"]["front_passenger"] = 37
    actor.simulator.state = deepcopy(actor.vehicle)
    snapshot = deepcopy(actor.vehicle)
    original = "打开后视镜加热；副驾车窗保持37%，别调整温度。"
    await actor.post({"type": "input", "text": original})
    await wait_for(q, "stream.done", actor)
    request = planner.requests[0]
    assert request["request_text"] == original and request["skill_ids"] == []
    assert request["vehicle"] == snapshot and request["completed_operations"] == []
    assert any(record.get("content") == "你好，准备出发。" for record in request["history"])
    assert any(record.get("content") == "准备好了。" for record in request["history"])
    assert actor.vehicle["windows"]["positions"]["front_passenger"] == 37
    assert request["vehicle"]["mirrors"]["heating"] is False  # Independent input copy.


@pytest.mark.parametrize("mixed", ["hot_and_delegate", "two_delegates", "cold_and_hot"])
async def test_mini_mixed_execution_groups_reject_before_any_partial_dispatch(make_actor, mixed):
    planner = FakeVehiclePlanner(plan_response(("set_volume", {"volume": 35}),
                                              ("set_mirror_heating", {"enabled": True})))
    bad = ([call("set_volume", {"volume": 35}, "bad-hot"), delegate(["mirrors"], "bad-delegate")]
           if mixed == "hot_and_delegate" else
           [delegate(["mirrors"], "bad-one"), delegate(["media"], "bad-two")]
           if mixed == "two_delegates" else
           [call("set_volume", {"volume": 35}, "bad-hot"), call("set_mirror_heating", {"enabled": True}, "bad-cold")])
    actor, q, ark = await make_actor([
        {"tool_calls": bad}, {"tool_calls": [delegate(["media", "mirrors"], "whole")]},
    ], planner)
    await actor.post({"type": "input", "text": "音量设为35，并打开后视镜加热。"})
    _, events = await wait_for(q, "stream.done", actor)
    if mixed == 'cold_and_hot':
        assert not any(event['type'] == 'vehicle.command' for event in events)
        assert actor.current.checkpoint.run.error_code == 'unknown_tool'
        await actor.post({'type': 'input', 'text': '音量设为35，并打开后视镜加热。'})
        _, events = await wait_for(q, 'stream.done', actor)
    else:
        assert all(actor.current.tool_results[item['id']]['code'] == 'planned_vehicle_group_conflict' for item in bad)
        assert all(actor.current.tool_results[item['id']]['executed'] is False for item in bad)
    assert [e["name"] for e in events if e["type"] == "vehicle.command"] == ["set_volume", "set_mirror_heating"]
    assert len(planner.requests) == 1 and len(ark.requests) == 2
    assert actor.vehicle["media"]["volume"] == 35 and actor.vehicle["mirrors"]["heating"] is True


async def test_multi_intent_hot_cold_and_keep_have_separate_actual_receipt_states(make_actor):
    original = "折叠后视镜，副驾车窗别动，音量设为35。"
    plan = split_plan(original, [
        {"text": "折叠后视镜", "operations": [("set_mirror_fold", {"folded": True})]},
        {"text": "副驾车窗别动", "status": "keep", "keep_paths": ["windows.positions.front_passenger"], "reason": "保留副驾开度"},
        {"text": "音量设为35", "operations": [("set_volume", {"volume": 35})]},
    ])
    actor, q, _ = await make_actor([{"tool_calls": [delegate(["mirrors", "windows", "media"])]}],
                                  FakeVehiclePlanner(plan))
    actor.vehicle["windows"]["positions"]["front_passenger"] = 42
    actor.simulator.state = deepcopy(actor.vehicle)
    done, events = None, None
    await actor.post({"type": "input", "text": original})
    done, events = await wait_for(q, "stream.done", actor)
    result = actor.current.tool_results["delegate"]
    assert result['ok'] is True and result['executed'] is True
    accepted = actor.current.checkpoint.orchestration.groups[-1].completed_plans[-1]
    assert [intent.status for intent in accepted.plan.intents] == ['planned', 'keep', 'planned']
    assert [item['name'] for item in result['receipts']] == ['set_mirror_fold', 'set_volume']
    assert all(item['result']['ok'] and item['result']['executed'] for item in result['receipts'])
    assert actor.vehicle["windows"]["positions"]["front_passenger"] == 42
    assert len([e for e in events if e["type"] == "vehicle.command"]) == 2
    for event in events:
        if event["type"] == "vehicle.command":
            assert event["source"] == "vehicle_plan" and event["parent_call_id"] == "delegate"
            assert event["intent_id"] and event["action_id"]
    assert "后视镜" in done["text"] and "音量" in done["text"]


async def test_dependency_is_topologically_ordered_and_only_one_action_is_in_flight(make_actor):
    original = "先展开后视镜，再把左后视镜水平调到5、垂直调到3。"
    plan = planned(original, [("set_mirror_fold", {"folded": False}),
                              ("set_mirror_angle", {"mirror": "left", "horizontal": 5, "vertical": 3})])
    plan["actions"][1]["depends_on"] = ["action-1"]
    plan["actions"].reverse()  # The dependency, not provider array order, controls execution.
    actor, q, _ = await make_actor([{"tool_calls": [delegate(["mirrors"])]}], FakeVehiclePlanner(plan))
    actor.vehicle["mirrors"]["folded"] = True
    actor.simulator.state = deepcopy(actor.vehicle)
    await actor.post({"type": "input", "text": original})
    first, _ = await wait_for(q, "vehicle.command")
    assert first["name"] == "set_mirror_fold"
    await actor.queue.join()
    assert not any(e["type"] == "vehicle.command" for e in q._queue)
    assert actor.vehicle["mirrors"]["folded"] is True
    receipt = await actor.simulator.execute(first["command_id"])
    await actor.post({'type': 'vehicle.receipt', 'command_id': first["command_id"], 'receipt_id': receipt['receipt_id']})
    second, events = await wait_for(q, "vehicle.command")
    assert second["name"] == "set_mirror_angle" and actor.vehicle["mirrors"]["folded"] is False
    assert any(e["type"] == "vehicle.result" and e["command_id"] == first["command_id"] for e in events)
    receipt = await actor.simulator.execute(second["command_id"])
    await actor.post({'type': 'vehicle.receipt', 'command_id': second["command_id"], 'receipt_id': receipt['receipt_id']})
    done, _ = await wait_for(q, "stream.done")
    assert actor.vehicle["mirrors"]["positions"]["left"] == {"horizontal": 5, "vertical": 3}
    assert done["metrics"]["error"] is None


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "bad_schema", "outside_loaded_skill", "keep_violation", "dependency_cycle"])
async def test_entire_plan_is_rejected_before_first_dispatch_for_any_invalid_action(make_actor, invalid):
    original = "打开后视镜加热，音量设为35，副驾车窗别动。"
    plan = planned(original, [("set_mirror_heating", {"enabled": True}), ("set_volume", {"volume": 35})],
                   keep_paths=["windows.positions.front_passenger"], keep_text="副驾车窗别动")
    if invalid == "empty":
        plan["actions"] = []; plan["intents"][0]["action_ids"] = []
    elif invalid == "duplicate":
        copied = deepcopy(plan["actions"][0]); copied["id"] = "duplicate"
        plan["actions"].append(copied); plan["intents"][0]["action_ids"].append("duplicate")
    elif invalid == "bad_schema":
        plan["actions"][1]["arguments"]["volume"] = 999
    elif invalid == "outside_loaded_skill":
        plan["actions"][1].update(name="set_child_lock", arguments={"side": "left", "enabled": True})
        plan["loaded_skills"] = ["mirrors"]
    elif invalid == "keep_violation":
        plan["actions"][1].update(name="set_window_position", arguments={"window": "front_passenger", "position": 0})
    else:
        plan["actions"][0]["depends_on"] = ["action-2"]; plan["actions"][1]["depends_on"] = ["action-1"]
    actor, q, _ = await make_actor([
        {"tool_calls": [delegate(["mirrors", "media"])]}, {"content": "计划未通过核实，没有执行。"},
    ], FakeVehiclePlanner(plan))
    before = deepcopy(actor.vehicle)
    await actor.post({"type": "input", "text": original})
    _, events = await wait_for(q, "stream.done", actor)
    assert not any(e["type"] == "vehicle.command" for e in events) and actor.vehicle == before
    result = actor.current.tool_results["delegate"]
    assert result["code"] == "vehicle_plan_invalid" and result["executed"] is False


async def test_planner_timeout_does_not_dispatch_or_claim_execution(make_actor):
    planner = FakeVehiclePlanner((.1, plan_response(("set_mirror_heating", {"enabled": True}))))
    actor, q, _ = await make_actor([{"tool_calls": [delegate(["mirrors"])]}], planner)
    original_start = actor.nlp_start_request
    def short_budget(turn):
        value = original_start(turn)
        value.budget.active_timeout_seconds = .01
        return value
    actor.nlp_start_request = short_budget
    before = deepcopy(actor.vehicle)
    await actor.post({"type": "input", "text": "打开后视镜加热。"})
    done, events = await wait_for(q, "stream.done")
    assert actor.vehicle == before and not any(e["type"] == "vehicle.command" for e in events)
    assert actor.current.checkpoint.run.status == 'failed'
    assert actor.current.checkpoint.run.error_code == 'active_time_budget_exhausted'
    assert "开了" not in done["text"]


async def test_plan_read_values_are_actual_executor_values_and_never_fake_talker_calls(make_actor):
    planner = FakeVehiclePlanner(plan_response(("get_window_state", {"window": "driver"})))
    actor, q, ark = await make_actor([
        {"tool_calls": [delegate(["windows"])]}, {"content": "主驾车窗开到百分之三十七。"},
    ], planner)
    actor.vehicle["windows"]["positions"]["driver"] = 37
    actor.simulator.state = deepcopy(actor.vehicle)
    before = deepcopy(actor.vehicle)
    await actor.post({"type": "input", "text": "读一下主驾车窗开度。"})
    done, events = await wait_for(q, "stream.done", actor)
    outer = actor.current.tool_results["delegate"]
    assert outer["receipts"][0]["result"]["value"]["position"] == 37
    assert actor.vehicle == before and done["text"] == "主驾车窗开到百分之三十七。"
    assert any(e["type"] == "vehicle.result" and e["result"].get("value", {}).get("position") == 37 for e in events)
    assert [c["function"]["name"] for m in actor.history() for c in m.get("tool_calls", [])] == ["delegate_vehicle_control"]
    assert "receipts" in json.loads(next(m["content"] for m in ark.requests[-1]["messages"] if m.get("tool_call_id") == "delegate"))


async def test_interrupted_late_plan_never_dispatches_into_new_turn(make_actor):
    entered, release = asyncio.Event(), asyncio.Event()

    class LatePlanner(FakeVehiclePlanner):
        async def plan(self, request_text, vehicle, skill_ids, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # Deliberately model a cancellation-ignorant upstream.
            return planned(request_text, [("set_mirror_heating", {"enabled": True})])

    actor, q, _ = await make_actor([
        {"tool_calls": [delegate(["mirrors"])]}, {"content": "新话题正常回答。"},
    ], LatePlanner())
    await actor.post({"type": "input", "text": "打开后视镜加热。"})
    await asyncio.wait_for(entered.wait(), 1)
    old = actor.current
    await actor.post({"type": "input", "text": "停下，聊点别的。"})
    done, events = await wait_for(q, "stream.done")
    release.set()
    await asyncio.sleep(.01)
    await actor.queue.join()
    assert done["turn_id"] != old.id and actor.vehicle["mirrors"]["heating"] is False
    assert not any(e["type"] == "vehicle.command" for e in [*events, *q._queue])
    await actor.trace_queue.join()
    assert old.interrupted and old.audio_cancelled.is_set()
    assert not any(row['kind'] == 'vehicle.dispatched' and row['turn_id'] == old.id
                   for row in actor.db.traces_after(actor.id))


async def test_completed_hot_operation_is_bound_to_real_receipt_and_not_reexecuted_by_plan(make_actor):
    original = '打开氛围灯，并启用左后门儿童安全锁。'
    planner = FakeVehiclePlanner()
    def complete_remaining(text):
        operation = planner.requests[-1]['completed_operations'][0]
        assert operation['result']['ok'] is True
        assert operation['name'] == 'set_ambient_light' and operation['arguments'] == {'enabled': True}
        return split_plan(text, [
            {'text': '打开氛围灯', 'status': 'completed',
             'completed_operation_ids': [operation['operation_id']]},
            {'text': '并启用左后门儿童安全锁。', 'operations': [('set_child_lock', {'side': 'left', 'enabled': True})]},
        ])
    planner.responses = [complete_remaining]
    actor, q, ark = await make_actor([
        # A real query receipt returns control after the accepted hot action.
        {'tool_calls': [call('set_ambient_light', {'enabled': True}, 'ambient-first'),
                        call('web_search', {'query': '操作参考资料'}, 'query')]},
        {'tool_calls': [delegate(['locks'], 'remaining')]},
    ], planner)
    class QueryFixture:
        def info(self): return {'configured': True}
        async def web_search(self, **arguments): return {'ok': False, 'reason': 'No reference available'}
        async def aclose(self): pass
    actor.web = QueryFixture()
    actor.configure_nlp()
    await actor.post({'type': 'input', 'text': original})
    done, events = await wait_for(q, 'stream.done', actor)
    commands = [e for e in events if e['type'] == 'vehicle.command']
    assert [e['name'] for e in commands] == ['set_ambient_light', 'set_child_lock']
    operation = planner.requests[0]['completed_operations'][0]
    assert operation['operation_id'] == operation['command_id'] == commands[0]['command_id']
    assert operation['result']['name'] == operation['name'] and operation['result']['arguments'] == operation['arguments']
    assert planner.requests[0]['request_text'] == original and len(ark.requests) == 2
    outer = actor.current.tool_results['remaining']
    accepted = actor.current.checkpoint.orchestration.groups[-1].completed_plans[-1].plan
    assert accepted.intents[0].completed_operation_ids == [commands[0]['command_id']]
    assert [intent.status for intent in accepted.intents] == ['completed', 'planned']
    assert [item['name'] for item in outer['receipts']] == ['set_child_lock']
    assert '氛围灯' in done['text'] and '儿童安全锁' in done['text']


async def test_device_rechecks_latest_vehicle_when_planner_snapshot_is_stale(make_actor):
    entered, release = asyncio.Event(), asyncio.Event()
    class WaitingPlanner(FakeVehiclePlanner):
        async def plan(self, request_text, vehicle, skill_ids, **kwargs):
            entered.set()
            await release.wait()
            assert vehicle['mirrors']['folded'] is False  # Original independent snapshot.
            return planned(request_text, [('set_volume', {'volume': 35}),
                                          ('set_mirror_angle', {'mirror': 'left', 'horizontal': 5, 'vertical': 3})])
    actor, q, _ = await make_actor([{'tool_calls': [delegate(['media', 'mirrors'])]},
        {'content': '音量已调到35；后视镜处于折叠状态，角度没有调整。'}], WaitingPlanner())
    await actor.post({'type': 'input', 'text': '音量设为35，左后视镜水平调到5、垂直调到3。'})
    await asyncio.wait_for(entered.wait(), 1)
    actor.vehicle['mirrors']['folded'] = True
    actor.simulator.state = deepcopy(actor.vehicle)
    release.set()
    done, events = await wait_for(q, 'stream.done', actor)
    assert actor.vehicle['media']['volume'] == 35
    assert [e['name'] for e in events if e['type'] == 'vehicle.command'] == ['set_volume', 'set_mirror_angle']
    result = actor.current.tool_results['delegate']
    assert result['code'] == 'plan_action_failed' and result['executed'] is True
    assert result['receipts'][0]['result']['ok'] is True and result['receipts'][1]['result']['executed'] is False
    assert '角度没有调整' in done['text']


async def test_failed_actual_action_blocks_its_dependent_without_using_simulated_success(make_actor):
    original = '主驾车窗开到百分之八十，然后音量设为35。'
    plan = planned(original, [('set_window_position', {'window': 'driver', 'position': 80}),
                              ('set_volume', {'volume': 35})])
    plan['actions'][1]['depends_on'] = ['action-1']
    actor, q, _ = await make_actor([{'tool_calls': [delegate(['windows', 'media'])]},
        {'content': '车辆正在行驶，车窗未打开，后续音量操作也未执行。'}], FakeVehiclePlanner(plan))
    await actor.post({'type': 'input', 'text': original})
    command, _ = await wait_for(q, 'vehicle.command')
    actor.vehicle['_meta']['speed_kph'] = 90  # State changed after validation but before real execution.
    actor.simulator.state = deepcopy(actor.vehicle)
    receipt = await actor.simulator.execute(command['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': command['command_id'], 'receipt_id': receipt['receipt_id']})
    done, events = await wait_for(q, 'stream.done')
    outer = actor.current.tool_results['delegate']
    assert outer['ok'] is False and outer['executed'] is False
    assert outer['remaining_actions_not_executed'] is True and len(outer['receipts']) == 1
    assert not outer['receipts'][0]['result']['ok']
    assert not any(e['type'] == 'vehicle.command' for e in events)
    assert actor.vehicle['windows']['positions']['driver'] == 0 and actor.vehicle['media']['volume'] == 30
    assert done['metrics']['error'] is None
    assert outer['code'] == 'plan_action_failed'


async def test_plan_read_uses_changed_actual_value_instead_of_preflight_simulation(make_actor):
    planner = FakeVehiclePlanner(plan_response(('get_window_state', {'window': 'driver'})))
    actor, q, _ = await make_actor([
        {'tool_calls': [delegate(['windows'])]}, {'content': '主驾车窗开到百分之六十一。'},
    ], planner)
    actor.vehicle['windows']['positions']['driver'] = 37
    actor.simulator.state = deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '读取主驾车窗开度。'})
    command, _ = await wait_for(q, 'vehicle.command')
    actor.vehicle['windows']['positions']['driver'] = 61
    actor.simulator.state = deepcopy(actor.vehicle)
    receipt = await actor.simulator.execute(command['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': command['command_id'], 'receipt_id': receipt['receipt_id']})
    done, _ = await wait_for(q, 'stream.done')
    outer = actor.current.tool_results['delegate']
    assert outer['receipts'][0]['result']['value']['position'] == 61
    assert '六十一' in done['text']
    assert planner.requests[0]['vehicle']['windows']['positions']['driver'] == 37


async def test_interruption_discards_late_receipt_and_cancels_dependent_actions(make_actor):
    value = planned('折叠后视镜，再打开后视镜加热。', [('set_mirror_fold', {'folded': True}), ('set_mirror_heating', {'enabled': True})])
    value['actions'][1]['depends_on'] = [value['actions'][0]['id']]
    actor, q, _ = await make_actor([
        {'tool_calls': [delegate(['mirrors'], 'old-plan')]}, {'content': '新话题正常回答。'},
    ], FakeVehiclePlanner(value))
    await actor.post({'type': 'input', 'text': '折叠后视镜，再打开后视镜加热。'})
    command, _ = await wait_for(q, 'vehicle.command')
    old_turn = actor.current
    await actor.post({'type': 'input', 'text': '停下，聊点别的。'})
    done, _ = await wait_for(q, 'stream.done')
    receipt = await actor.simulator.execute(command['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': command['command_id'], 'receipt_id': receipt['receipt_id']})
    await actor.queue.join()
    assert actor.vehicle['mirrors']['folded'] is False and actor.vehicle['mirrors']['heating'] is False
    assert not any(e['type'] in {'vehicle.command', 'vehicle.result'} for e in q._queue)
    assert old_turn.interrupted
    assert actor.current.id == done['turn_id'] and actor.current.output == '新话题正常回答。'


async def test_new_delegation_cannot_receive_or_execute_previous_planners_late_result(make_actor):
    entered, release = asyncio.Event(), asyncio.Event()
    class CompetingPlanner(FakeVehiclePlanner):
        async def plan(self, request_text, vehicle, skill_ids, **kwargs):
            self.requests.append({'request_text': request_text})
            if len(self.requests) == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return planned(request_text, [('set_mirror_heating', {'enabled': True})])
            return planned(request_text, [('set_mirror_fold', {'folded': True})])
    planner = CompetingPlanner()
    actor, q, _ = await make_actor([
        {'tool_calls': [delegate(['mirrors'], 'old')]}, {'tool_calls': [delegate(['mirrors'], 'new')]},
    ], planner)
    await actor.post({'type': 'input', 'text': '打开后视镜加热。'})
    await asyncio.wait_for(entered.wait(), 1)
    old_turn = actor.current
    await actor.post({'type': 'input', 'text': '先不加热了，把后视镜折叠。'})
    done, events = await wait_for(q, 'stream.done', actor)
    release.set()
    await asyncio.sleep(.01)
    await actor.queue.join()
    assert len(planner.requests) == 2 and old_turn.id != done['turn_id']
    assert actor.vehicle['mirrors']['folded'] is True and actor.vehicle['mirrors']['heating'] is False
    assert [e['parent_call_id'] for e in events if e['type'] == 'vehicle.command'] == ['new']
    assert not any(e['type'] == 'vehicle.command' for e in q._queue)
    assert actor.current.tool_results['new']['ok'] is True
    assert old_turn.interrupted and old_turn.audio_cancelled.is_set()
