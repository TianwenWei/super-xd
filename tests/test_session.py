"""Fault-injection tests for actor ordering; all model doubles are explicit."""
import asyncio
import json
import copy
import pytest
from backend.db import Database
from backend.session import SessionActor, Turn, effective_text
from backend.catalog import TOOLS
from vehicle_fakes import FakeVehiclePlanner, plan_response, planned, expected_talker_tool_names


def call(name, arguments=None, id='call_1'):
    return {'id': id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments or {})}}


class FakeArk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def stream(self, messages, tools):
        self.requests.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            delay, response = response
            await asyncio.sleep(delay)
        if response.get('content'):
            yield {'type': 'delta', 'text': response['content']}
        yield {'type': 'message', 'message': {'role': 'assistant', **response}}


@pytest.fixture
async def make_actor(tmp_path):
    actors = []
    async def make(responses, vehicle_planner=None):
        ark = FakeArk(responses)
        db = Database(tmp_path / f'{len(actors)}.sqlite3')
        actor = SessionActor(f'session{len(actors)}', db, ark,
                             vehicle_planner=vehicle_planner or FakeVehiclePlanner())
        actors.append(actor)
        outgoing = await actor.attach()
        await outgoing.get()  # initial snapshot
        return actor, outgoing, ark
    yield make
    await asyncio.gather(*(a.close() for a in actors))


def sync_tasks(actor):
    """Mirror rows written straight to SQLite, as the actor's event poller would."""
    for task in actor.db.list_tasks(actor.id):
        actor.remember_task(task)


async def wait_for(q, type_, ack_actor=None, timeout=2):
    events = []
    async with asyncio.timeout(timeout):
        while True:
            event = await q.get()
            events.append(event)
            if event['type'] == 'vehicle.command' and ack_actor:
                await ack_actor.post({'type': 'vehicle.execute', 'command_id': event['command_id']})
            if event['type'] == type_:
                return event, events


async def test_hot_path_uses_fixed_tool_and_full_receipt(make_actor):
    actor, q, ark = await make_actor([{'tool_calls': [call('close_all_windows')]}])
    await actor.post({'type': 'input', 'text': '关闭全部车窗'})
    done, events = await wait_for(q, 'stream.done', actor)
    receipt = next(e for e in events if e['type'] == 'vehicle.result')
    assert receipt['result']['ok']
    assert set(actor.vehicle['windows']['positions'].values()) == {0}
    assert done['metrics']['model_calls'] == 1
    assert receipt['latency_ms'] <= done['metrics']['total_ms']
    assert [c['function']['name'] for m in actor.history() for c in m.get('tool_calls', [])] == ['close_all_windows']
    assert any(m['role'] == 'tool' for m in actor.history())


async def test_vehicle_function_calls_arrive_before_dispatch_and_survive_snapshot(make_actor):
    calls = [call('close_all_windows', id='windows'), call('set_volume', {'volume': 35}, id='volume')]
    actor, q, _ = await make_actor([{'tool_calls': calls}, {'content': '你好。'}])
    await actor.post({'type': 'input', 'text': '关上全部车窗，音量调到35'})
    done, events = await wait_for(q, 'stream.done', actor)
    generated = [e for e in events if e['type'] == 'vehicle.function_call']
    assert [e['call'] for e in generated] == calls
    assert all(e['turn_id'] == done['turn_id'] for e in generated)
    for event in generated:
        command = next(e for e in events if e['type'] == 'vehicle.command' and e['name'] == event['call']['function']['name'])
        assert events.index(event) < events.index(command)
    await actor.post({'type': 'input', 'text': '你好'})
    _, events = await wait_for(q, 'stream.done')
    assert not any(e['type'] == 'vehicle.function_call' for e in events)
    restored = await actor.attach()
    snapshot, _ = await wait_for(restored, 'session.snapshot')
    assert snapshot['turns'][0]['vehicle_calls'] == calls
    assert snapshot['turns'][1]['vehicle_calls'] == []


async def test_browser_ack_is_not_success_and_timeout_preserves_state(make_actor, monkeypatch):
    monkeypatch.setattr('backend.session.TOOL_TIMEOUT', .03)
    actor, q, ark = await make_actor([{'tool_calls': [call('set_volume', {'volume': 55})]}])
    old = copy.deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '音量55'})
    command, _ = await wait_for(q, 'vehicle.command')
    assert actor.vehicle == old
    done, events = await wait_for(q, 'stream.done')
    assert not next(e for e in events if e['type'] == 'vehicle.result')['result']['ok']
    assert '状态尚未确认' in done['text'] and '没能执行' not in done['text']
    assert actor.vehicle == old
    await actor.post({'type': 'vehicle.execute', 'command_id': command['command_id']})
    await actor.queue.join()
    assert actor.vehicle == old


async def test_interruption_late_receipt_and_duplicate_execution(make_actor):
    actor, q, ark = await make_actor([{'tool_calls': [call('set_volume', {'volume': 55})]}, {'content': '今天的天空很清澈。'}])
    await actor.post({'type': 'input', 'text': '音量55'})
    command, _ = await wait_for(q, 'vehicle.command')
    old_turn = actor.current
    await actor.post({'type': 'input', 'text': '聊聊天'})
    await wait_for(q, 'stream.done')
    await actor.post({'type': 'vehicle.execute', 'command_id': command['command_id']})
    await actor.queue.join()
    assert actor.vehicle['media']['volume'] != 55
    assert not any(e['type'] == 'vehicle.result' for e in q._queue)
    await actor.post({'type': 'vehicle.execute', 'command_id': command['command_id']})
    await actor.post({'type': '_delta', 'turn': old_turn, 'text': '不应显示的迟到文字'})
    await actor.queue.join()
    assert len(actor.completed_commands) == 0
    assert not any(m.get('tool_call_id') == 'call_1' for m in old_turn.messages)
    assert '不应显示' not in actor.current.output
    assert all(e['type'] != 'stream.delta' for e in list(q._queue))


async def test_two_tasks_require_cancel_disambiguation(make_actor):
    actor, q, ark = await make_actor([{'tool_calls': [call('manage_task', {'action':'cancel'})]}])
    a = actor.db.create_task(actor.id, '调研甲')
    b = actor.db.create_task(actor.id, '调研乙')
    sync_tasks(actor)
    await actor.post({'type': 'input', 'text': '把那个取消'})
    done, _ = await wait_for(q, 'stream.done')
    assert '哪项任务' in done['text'] and done['text'].count('？') == 1
    assert actor.db.get_task(a['id'])['status'] == actor.db.get_task(b['id'])['status'] == 'queued'


async def test_stale_revision_cannot_announce_or_overwrite(make_actor):
    actor, q, ark = await make_actor([])
    task = actor.db.create_task(actor.id, '查餐厅')
    actor.db.save_task(task['id'], 1, status='completed', result={'summary': '旧结果'}, summary='旧结果')
    old_event = actor.db.events_after(0, actor.id)[-1]
    updated = actor.db.mutate_task(task['id'], 'update', '必须有素食', expected_revision=1)
    assert not actor.db.save_task(task['id'], 1, status='completed', result={'summary': '旧条件迟到'})
    await actor.post({'type': '_task_event', 'event': old_event})
    await actor.queue.join()
    await asyncio.sleep(.3)
    assert not any(e['type'] == 'turn.started' for e in list(q._queue))
    assert actor.db.get_task(task['id'])['revision'] == updated['revision']


async def test_cancelled_task_cannot_revive_and_duplicate_event_once(make_actor):
    actor, q, ark = await make_actor([])
    task = actor.db.create_task(actor.id, '调研')
    actor.db.mutate_task(task['id'], 'cancel')
    actor.db.save_task(task['id'], 2, status='cancelled', summary='已取消')
    assert not actor.db.save_task(task['id'], 2, status='completed', result={'summary': '迟到'})
    await asyncio.sleep(.45)
    seen = list(q._queue)
    assert sum(e['type'] == 'turn.started' for e in seen) == 1
    event = actor.db.events_after(0, actor.id)[-1]
    await actor.post({'type': '_task_event', 'event': event})
    actor.db.event(task['id'], 'completed', {'task': {**task, 'revision': 2, 'status': 'completed'}})
    await asyncio.sleep(.4)
    assert sum(e['type'] == 'turn.started' for e in list(q._queue)) == 1
    assert actor.db.get_task(task['id'])['status'] == 'cancelled'


async def test_playback_blocks_proactive_result_until_end(make_actor):
    actor, q, ark = await make_actor([{'content': '先说这一段文字。'}])
    await actor.post({'type': 'input', 'text': '开始聊天'})
    await wait_for(q, 'stream.done')
    await actor.post({'type': 'playback', 'speaking': True})
    task = actor.db.create_task(actor.id, '后台')
    actor.db.save_task(task['id'], 1, status='completed', result={'summary': '后台最终结果'}, summary='完成')
    await asyncio.sleep(.5)
    assert any(e['type'] == 'task.event' for e in list(q._queue))
    assert not any(e['type'] == 'turn.started' for e in list(q._queue))
    await actor.post({'type': 'playback', 'speaking': False})
    done, _ = await wait_for(q, 'stream.done')
    assert '后台最终结果' in done['text']


async def test_clarification_switch_persona_keeps_task_and_constraints(make_actor):
    actor, q, ark = await make_actor([])
    task = actor.db.create_task(actor.id, '查餐厅')
    actor.db.save_task(task['id'], 1, status='waiting_clarification', question='哪个城市？', summary='待回复')
    sync_tasks(actor)
    await actor.post({'type': 'persona.set', 'preset': 'playful'})
    await actor.post({'type': 'task.reply', 'task_id': task['id'], 'text': '上海，只要素食', 'expected_revision': 1})
    await wait_for(q, 'ui.card')  # the write completes off the actor loop
    current = actor.db.get_task(task['id'])
    assert current['constraints'] == ['上海，只要素食']
    assert current['id'] == task['id'] and current['revision'] == 2
    actor.db.save_task(task['id'], 2, status='completed', result={'summary': '上海素食结果'}, summary='完成')
    done, _ = await wait_for(q, 'stream.done')
    assert done['text'] == '上海素食结果。'
    assert actor.persona.id == 'playful'


async def test_current_turn_persona_is_frozen(make_actor):
    actor, q, ark = await make_actor([(.05, {'tool_calls': [call('close_all_windows')]})])
    await actor.post({'type': 'input', 'text': '关窗'})
    await wait_for(q, 'turn.started')
    await actor.post({'type': 'persona.set', 'preset': 'professional'})
    done, _ = await wait_for(q, 'stream.done', actor)
    assert done['text'].endswith('了。')
    assert '全部车窗' in done['text']
    assert actor.current.persona.id == 'warm' and actor.persona.id == 'professional'


async def test_cold_vehicle_delegation_does_not_add_schema_or_second_talker_call(make_actor):
    planner = FakeVehiclePlanner(plan_response(('set_mirror_heating', {'enabled': True})))
    actor, q, ark = await make_actor([
        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['mirrors']})]},
    ], planner)
    await actor.post({'type': 'input', 'text': '打开后视镜加热'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert done['metrics']['model_calls'] == 1 and len(planner.requests) == 1
    assert actor.vehicle['mirrors']['heating']
    assert len(ark.requests[-1]['tools']) == 26
    assert all(t['function']['name'] != 'set_mirror_heating' for t in ark.requests[-1]['tools'])


async def test_invalid_arguments_do_not_change_state(make_actor):
    actor, q, ark = await make_actor([{'tool_calls': [call('set_volume', {'volume':999})]}])
    old = copy.deepcopy(actor.vehicle)
    await actor.post({'type':'input','text':'音量999'})
    done, events = await wait_for(q,'stream.done')
    assert actor.vehicle == old
    assert not any(e['type']=='vehicle.command' for e in events)
    assert [e['call'] for e in events if e['type'] == 'vehicle.function_call'] == [call('set_volume', {'volume': 999})]
    assert actor.snapshot()['turns'][0]['vehicle_calls'] == [call('set_volume', {'volume': 999})]
    assert '零到一百之间' in done['text'] and '没能执行' in done['text']
    assert '没确认清楚' not in done['text']


@pytest.mark.parametrize('destination', ['沪', 'Shanghai Hongqiao Railway Station', '上海南站'])
async def test_destination_not_in_user_words_is_rejected_and_model_asked_again(make_actor, destination):
    actor, q, ark = await make_actor([{'tool_calls': [call('set_navigation_destination', {'destination': destination}, 'nav')]},
                                      {'content': '你要去哪里？'}])
    old = copy.deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '把模拟导航目的地设为上海虹桥站'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert actor.vehicle == old
    assert not any(e['type'] == 'vehicle.command' for e in events)
    assert actor.current.tool_results['nav']['code'] == 'destination_not_in_request'
    assert len(ark.requests) == 2 and done['text'] == '你要去哪里？'


async def test_destination_may_come_from_recent_dialogue_with_asr_spacing(make_actor):
    actor, q, _ = await make_actor([{'content': '要导航去上海 虹桥站吗？'},
                                    {'tool_calls': [call('set_navigation_destination', {'destination': '上海虹桥站'})]}])
    await actor.post({'type': 'input', 'text': '我要去火车站'})
    await wait_for(q, 'stream.done', actor)
    await actor.post({'type': 'input', 'text': '对，就去那里'})
    await wait_for(q, 'stream.done', actor)
    assert actor.vehicle['navigation']['destination'] == '上海虹桥站'


@pytest.mark.parametrize(('text', 'destination'), [
    ('把模拟导航目的地设为上海虹桥站', '上海虹桥站'),
    ('导航到“北京南站”。', '北京南站'),
    ('把目的地改为上海市虹桥路123号，谢谢', '上海市虹桥路123号'),
    ('导航到100号酒吧', '100号酒吧'),
])
async def test_navigation_retains_full_literal_place(make_actor, text, destination):
    actor, q, _ = await make_actor([{'tool_calls': [call('set_navigation_destination', {'destination': destination})]}])
    await actor.post({'type': 'input', 'text': text})
    done, events = await wait_for(q, 'stream.done', actor)
    assert actor.vehicle['navigation']['destination'] == destination
    assert next(e for e in events if e['type'] == 'vehicle.result')['result']['ok']


async def test_network_error_is_explicit_failure(make_actor):
    actor, q, ark = await make_actor([RuntimeError('Ark HTTP 401')])
    await actor.post({'type':'input','text':'你好'})
    done, _ = await wait_for(q,'stream.done')
    assert '401' in done['metrics']['error'] and '连接出了问题' in done['text']
    assert '401' not in done['text']


@pytest.mark.parametrize('value', ['', '，。！ ', '好的', '……'])
def test_short_or_punctuation_only_text_is_not_effective(value):
    assert not effective_text(value)


def test_real_content_is_effective():
    assert effective_text('晚上好，今天想聊些什么？')


@pytest.mark.parametrize('name', __import__('backend.catalog',fromlist=['HOT_TOOL_NAMES']).HOT_TOOL_NAMES)
async def test_every_hot_receipt_template_completes_after_state_commit(make_actor, name):
    actor, q, _ = await make_actor([{'tool_calls': [call(name, TOOLS[name].example)]}])
    # The request quotes the example so a destination is grounded in user words.
    await actor.post({'type':'input','text':'明确执行热工具示例：' + json.dumps(TOOLS[name].example, ensure_ascii=False)})
    done, events = await wait_for(q, 'stream.done', actor)
    receipt = next(e for e in events if e['type']=='vehicle.result')
    assert receipt['result']['ok'] and done['metrics']['error'] is None
    assert done['metrics']['model_calls'] == 1
    assert not any(e['type']=='error' for e in events)


async def test_fixed_prefix_and_tool_order_do_not_change_with_persona(make_actor):
    from backend.session import Turn
    from backend.persona import PERSONAS
    actor, _, _ = await make_actor([])
    prefix, definitions = None, None
    for persona in PERSONAS.values():
        turn = Turn('id','你好',persona,1)
        messages=actor.messages_for(turn)
        tools=actor.tools_for(turn)
        if prefix is None: prefix,definitions=messages[0],tools
        assert messages[0]==prefix and tools==definitions
        assert len(tools)==26
    assert [t['function']['name'] for t in definitions[:15]] == __import__('backend.catalog',fromlist=['HOT_TOOL_NAMES']).HOT_TOOL_NAMES
    assert [t['function']['name'] for t in definitions[15:]] == ['delegate_vehicle_control', 'delegate_task', 'manage_task', 'search_places', 'get_weather', 'start_poi_navigation', 'web_search', 'search_manual', 'search_media', 'control_media', 'manage_navigation']


async def test_compound_hot_receipts_cover_every_intent_without_unrelated_settings(make_actor):
    actor, q, _ = await make_actor([
        {'tool_calls': [call('set_climate_power', {'enabled': True}, 'power'),
                        call('close_all_windows', id='windows'),
                        call('set_seat_massage', {'seat': 'driver', 'mode': 'relax'}, 'massage')]},
    ])
    actor.vehicle['windows']['positions']['driver'] = 50
    before = copy.deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '打开空调、关上全部车窗、打开主驾放松按摩'})
    done, events = await wait_for(q, 'stream.done', actor)
    commands = [e['name'] for e in events if e['type'] == 'vehicle.command']
    assert commands == ['set_climate_power', 'close_all_windows', 'set_seat_massage']
    assert actor.vehicle['climate']['power'] is True
    assert set(actor.vehicle['windows']['positions'].values()) == {0}
    assert actor.vehicle['seats']['positions']['driver']['massage'] == 'relax'
    assert actor.vehicle['climate']['temperatures'] == before['climate']['temperatures']
    assert actor.vehicle['climate']['fan_speed'] == before['climate']['fan_speed']
    assert all(actor.vehicle['seats']['positions'][seat]['heating'] == before['seats']['positions'][seat]['heating']
               for seat in before['seats']['positions'])
    assert all(word in done['text'] for word in ('空调', '车窗', '主驾', '按摩', '放松'))
    assert done['text'].count('空调') == 1 and done['text'].count('按摩') == 1


async def test_read_result_is_given_to_model_instead_of_generic_completed_reply(make_actor):
    planner = FakeVehiclePlanner(plan_response(('get_window_state', {'window': 'driver'})))
    actor, q, ark = await make_actor([
        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['windows']}, 'delegate-read')]},
        {'content': '主驾车窗开到一半，其他三扇都关着。'},
    ], planner)
    actor.vehicle['windows']['positions']['driver'] = 50
    before = copy.deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '读一下各个车窗的状态'})
    done, _ = await wait_for(q, 'stream.done', actor)
    assert actor.vehicle == before
    assert done['text'] == '主驾车窗开到一半，其他三扇都关着。'
    outer = next(json.loads(m['content']) for m in ark.requests[-1]['messages'] if m.get('tool_call_id') == 'delegate-read')
    assert outer['read_results'][0]['result']['value']['position'] == 50
    assert not any(c['function']['name'] == 'get_window_state' for m in actor.history() for c in m.get('tool_calls', []))


async def test_fixed_hot_capability_handles_next_turn_correction_without_loading(make_actor):
    actor, q, ark = await make_actor([
        {'tool_calls': [call('set_seat_massage', {'seat': 'driver', 'mode': 'relax'}, 'relax')]},
        {'tool_calls': [call('set_seat_massage', {'seat': 'driver', 'mode': 'lumbar'}, 'lumbar')]},
    ])
    await actor.post({'type': 'input', 'text': '打开主驾放松按摩'})
    await wait_for(q, 'stream.done', actor)
    await actor.post({'type': 'input', 'text': '改成腰部模式，别关掉'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert actor.vehicle['seats']['positions']['driver']['massage'] == 'lumbar'
    assert [e['name'] for e in events if e['type'] == 'vehicle.command'] == ['set_seat_massage']
    assert '腰部' in done['text']
    assert 'set_seat_massage' in [t['function']['name'] for t in ark.requests[-1]['tools']]
    assert len(ark.requests[0]['tools']) == len(ark.requests[-1]['tools']) == 26
    assert not any(c['function']['name'] == 'load_skill' for m in actor.history() for c in m.get('tool_calls', []))


async def test_model_failure_after_partial_execution_still_reports_verified_actions(make_actor):
    planner = FakeVehiclePlanner(plan_response(('set_climate_power', {'enabled': True}),
                                               ('get_window_state', {'window': 'driver'})))
    actor, q, _ = await make_actor([
        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['climate', 'windows']}, 'mixed')]},
        RuntimeError('network failure after execution'),
    ], planner)
    await actor.post({'type': 'input', 'text': '打开空调，再读主驾车窗状态'})
    done, _ = await wait_for(q, 'stream.done', actor)
    assert actor.vehicle['climate']['power'] is True
    assert '空调开了' in done['text'] and '还有请求没确认' in done['text']
    assert '读取指定车窗' not in done['text']
    assert done['metrics']['error']


async def test_mixed_read_write_continuation_requires_actual_state_answer(make_actor):
    planner = FakeVehiclePlanner(plan_response(('get_window_state', {'window': 'driver'}),
                                               ('set_volume', {'volume': 35})))
    actor, q, ark = await make_actor([
        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['windows', 'media']}, 'mixed')]},
        {'content': '主驾车窗开到百分之五十。'},
    ], planner)
    actor.vehicle['windows']['positions']['driver'] = 50
    await actor.post({'type': 'input', 'text': '告诉我主驾车窗开度，再把音量调到35'})
    done, _ = await wait_for(q, 'stream.done', actor)
    assert len(ark.requests) == 2 and len(planner.requests) == 1
    assert '主驾车窗开到百分之五十' in done['text']  # Success is not repeated after the dispatch sentence.
    assert actor.vehicle['media']['volume'] == 35
    result = actor.current.tool_results['mixed']
    assert result['read_results'][0]['result']['value']['position'] == 50


async def test_batch_task_progress_returns_each_selected_task_and_rejects_foreign_ids(make_actor):
    actor, _, _ = await make_actor([])
    a = actor.db.create_task(actor.id, '科学工业城研究', context={'spoken_goal': '科学工业城研究'})
    b = actor.db.create_task(actor.id, '自然历史博物馆研究', context={'spoken_goal': '自然历史博物馆研究'})
    sync_tasks(actor)
    result = actor.task_tool('manage_task', {'action': 'get', 'task_ids': [a['id'], b['id']]})
    assert result['ok'] and [task['id'] for task in result['tasks']] == [a['id'], b['id']]
    assert all(task['goal'] in result['detail'] for task in (a, b))
    foreign = actor.db.create_task('other-session', '其他会话任务')
    refused = actor.task_tool('manage_task', {'action': 'get', 'task_ids': [a['id'], foreign['id']]})
    assert refused['ok'] is False and 'tasks' not in refused
    for args in ({'action':'cancel','task_ids':[a['id']]},
                 {'action':'get','task_ids':[a['id']], 'task_id':b['id']},
                 {'action':'get','task_ids':[a['id'],a['id']]},
                 {'action':'get','task_ids':['',a['id']]},
                 {'action':'get','task_ids':[' \t',a['id']]}):
        assert actor.task_tool('manage_task', args)['ok'] is False
    assert actor.db.get_task(a['id'])['revision'] == 1
    assert actor.db.get_task(b['id'])['revision'] == 1


async def test_batch_progress_empty_id_cannot_resolve_unique_active_task(make_actor):
    actor, _, _ = await make_actor([])
    only = actor.db.create_task(actor.id, '唯一活跃任务')
    for identity in ('', ' ', '\n'):
        result = actor.task_tool('manage_task', {'action': 'get', 'task_ids': [identity, only['id']]})
        assert result['ok'] is False and 'tasks' not in result


async def test_no_task_tool_misroute_recovers_with_same_talker_and_exact_car_receipt(make_actor):
    planner = FakeVehiclePlanner(plan_response(('set_door_lock', {'door': 'front_passenger', 'locked': False})))
    actor, q, ark = await make_actor([
        {'tool_calls': [call('manage_task', {'action': 'reply', 'text': '只解锁右前副驾车门'}, 'mistake')]},
        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['locks']}, 'unlock')]},
    ], planner)
    await actor.post({'type': 'input', 'text': '只解锁右前副驾车门。'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert [e['name'] for e in events if e['type'] == 'vehicle.command'] == ['set_door_lock']
    assert actor.vehicle['locks']['doors']['front_passenger'] is False
    assert '副驾车门已解锁' in done['text']
    assert len(ark.requests) == 2 and actor.db.list_tasks(actor.id) == []
    mistake = next(json.loads(m['content']) for m in ark.requests[1]['messages'] if m.get('tool_call_id') == 'mistake')
    assert mistake['code'] == 'no_task' and mistake['retry_model'] is True and mistake['executed'] is False


async def test_no_task_progress_error_can_be_answered_without_creating_task(make_actor):
    actor, q, _ = await make_actor([
        {'tool_calls': [call('manage_task', {'action': 'get'}, 'mistake')]},
        {'content': '当前没有后台任务。'},
    ])
    await actor.post({'type': 'input', 'text': '后台任务进度怎么样？'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert done['text'] == '当前没有后台任务。'
    assert actor.db.list_tasks(actor.id) == []
    assert not any(e['type'] == 'vehicle.command' for e in events)


@pytest.mark.parametrize('initial_power', [False, True])
async def test_explicit_ambient_power_preservation_is_passed_to_plan_and_preserved(make_actor, initial_power):
    planner = FakeVehiclePlanner(plan_response(('set_ambient_color', {'color': 'blue'}),
                                               ('set_ambient_brightness', {'brightness': 60}),
                                               keep_paths=['lights.ambient'], keep_text='氛围灯开关别动'))
    actor, q, ark = await make_actor([
        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['lights']}, 'colors')]},
    ], planner)
    actor.vehicle['lights']['ambient'] = initial_power
    original = '氛围灯颜色改蓝色、亮度60%，氛围灯开关别动。'
    await actor.post({'type': 'input', 'text': original})
    done, events = await wait_for(q, 'stream.done', actor)
    assert [e['name'] for e in events if e['type'] == 'vehicle.command'] == ['set_ambient_color', 'set_ambient_brightness']
    assert actor.vehicle['lights']['ambient'] is initial_power
    assert actor.vehicle['lights']['ambient_color'] == 'blue'
    assert actor.vehicle['lights']['ambient_brightness'] == 60
    assert planner.requests[0]['request_text'] == original
    assert actor.current.tool_results['colors']['keep_paths'] == ['lights.ambient']
    assert len(ark.requests) == 1


async def test_corrected_explicit_ambient_power_request_is_executed(make_actor):
    actor, q, _ = await make_actor([
        {'tool_calls': [call('set_ambient_light', {'enabled': True}, 'power')]},
    ])
    await actor.post({'type': 'input', 'text': '不是关闭氛围灯，是打开氛围灯。'})
    done, events = await wait_for(q, 'stream.done', actor)
    assert [e['name'] for e in events if e['type'] == 'vehicle.command'] == ['set_ambient_light']
    assert actor.vehicle['lights']['ambient'] is True and '开' in done['text']


async def test_task_registration_captures_vehicle_history_without_persona(make_actor):
    actor,q,_=await make_actor([{'tool_calls':[call('delegate_task', {'goal':'根据当前车况调整舒适度'})]}])
    await actor.post({'type':'input','text':'根据当前车况调整舒适度'})
    await wait_for(q,'stream.done')
    task=actor.db.list_tasks(actor.id)[0]
    assert task['context']['vehicle']==actor.vehicle
    assert task['context']['recent_history'][-1]['content']=='根据当前车况调整舒适度'
    assert 'persona' not in task['context']


async def test_disconnect_releases_playback_and_pending_result_waits_for_reattach(make_actor):
    actor,q,_=await make_actor([])
    await actor.post({'type':'playback','speaking':True})
    await actor.post({'type':'_detach','outgoing':q})
    await actor.queue.join()
    assert not actor.speaking
    task=actor.db.create_task(actor.id,'断线期间后台完成')
    actor.db.save_task(task['id'],1,status='completed',result={'summary':'重连后的结果'},summary='完成')
    await asyncio.sleep(.4)
    assert actor.announcements and not actor.announced_keys
    new_q=await actor.attach()
    done,_=await wait_for(new_q,'stream.done')
    assert '重连后的结果' in done['text']


def weather_tasks(actor):
    ordinary = actor.db.create_task(actor.id, '查询法国巴黎明天温度')
    actor.db.save_task(ordinary['id'], 1, status='completed', summary='Open-Meteo预报已核实',
                       result={'summary': '完整普通天气报告。',
                               'spoken_summary': '巴黎明天11.2到19.3度，来自Open-Meteo。'})
    official = actor.db.create_task(actor.id, '查询法国气象局官网，核对法国巴黎明天温度')
    actor.db.save_task(official['id'], 1, status='waiting_clarification', summary='等待官网条件',
                       question='请提供法国气象局官网网址。')
    sync_tasks(actor)
    return actor.db.get_task(ordinary['id']), actor.db.get_task(official['id'])


class HeldDeltaArk(FakeArk):
    def __init__(self, responses):
        super().__init__(responses)
        self.delta_sent = asyncio.Event()

    async def stream(self, messages, tools):
        self.requests.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)})
        response = copy.copy(self.responses.pop(0))
        gate = response.pop('gate', None)
        if response.get('content'):
            yield {'type': 'delta', 'text': response['content']}
            self.delta_sent.set()
        if gate:
            await gate.wait()
        yield {'type': 'message', 'message': {'role': 'assistant', **response}, 'meta': {'test_provider': 'actual_fake_return'}}


class SpeechRecorder:
    def __init__(self):
        self.texts = []

    async def stream_synthesize(self, texts):
        async for text in texts:
            self.texts.append(text)
            yield {'type': 'audio', 'audio': b'\x01\x00'}








async def test_proactive_history_keeps_exact_text_and_immutable_task_attribution(make_actor):
    actor, q, _ = await make_actor([{'content': '你好。'}])
    ordinary, official = weather_tasks(actor)
    actor.announcements = {ordinary['id']: ordinary}
    await actor.post({'type': '_announce'})
    done, _ = await wait_for(q, 'stream.done')
    announced = actor.current
    text = done['text']
    attribution = json.loads(announced.messages[0]['content'].splitlines()[-1])
    assert attribution == {key: ordinary[key] for key in ('id', 'goal', 'revision', 'status')}
    assert announced.messages[1] == {'role': 'assistant', 'content': text}
    actor.db.mutate_task(ordinary['id'], 'update', '改查后天')
    sync_tasks(actor)
    await actor.post({'type': 'input', 'text': '你好'})
    await wait_for(q, 'stream.done')
    context = actor.messages_for(actor.current)
    assert announced.messages[0] in context and announced.messages[1] in context
    assert json.loads(announced.messages[0]['content'].splitlines()[-1])['revision'] == 1
    current = next(json.loads(m['content'].splitlines()[-1])['background_tasks_reference_only']
                   for m in context if m['role'] == 'system' and m['content'].startswith('当前会话快照'))
    assert next(t for t in current if t['id'] == ordinary['id'])['revision'] == 2
    assert next(t for t in current if t['id'] == official['id'])['status'] == 'waiting_clarification'


async def test_current_task_references_include_recent_completed_results_without_expanding_full_reports(make_actor):
    actor, _, _ = await make_actor([])
    ids = []
    for index in range(10):
        task = actor.db.create_task(actor.id, f'任务{index}')
        actor.db.save_task(task['id'], 1, status='completed', summary=f'状态{index}',
                           result={'summary': '完整报告' * 100, 'spoken_summary': f'真实短结论{index}'})
        ids.append(task['id'])
    before = copy.deepcopy(actor.db.list_tasks(actor.id))
    sync_tasks(actor)
    turn = Turn('current', '你好', actor.persona, 1)
    context = json.loads(actor.messages_for(turn)[2]['content'].splitlines()[-1])
    refs = context['background_tasks_reference_only']
    assert [t['id'] for t in refs] == ids[-8:]
    assert all(t['status'] == 'completed' for t in refs)
    assert [t['result'] for t in refs] == [{'spoken_summary': f'真实短结论{i}'} for i in range(2, 10)]
    assert actor.db.list_tasks(actor.id) == before








@pytest.mark.parametrize('text', [
    '你好，我准备出发了。', '现在主驾车窗开到多少？', '根据当前车况回答空调温度。',
    '请新查法国巴黎明天的天气。', '不要用已有结果回答，重新查询。', '不用已有结果回答，重新查询。', '“用已有结果回答”是什么意思？',
])
async def test_chat_state_queries_new_queries_and_negations_use_normal_model_output(make_actor, text):
    actor, q, ark = await make_actor([{'content': '这是测试模型的直接回答。'}])
    weather_tasks(actor)
    await actor.post({'type': 'input', 'text': text})
    done, _ = await wait_for(q, 'stream.done')
    assert done['text'] == '这是测试模型的直接回答。' and len(ark.requests) == 1
    assert not hasattr(actor.current, 'task_read_required')


async def test_explicit_existing_result_without_any_task_remains_normal_model_answer(make_actor):
    actor, q, ark = await make_actor([{'content': '当前没有后台任务结果。'}])
    await actor.post({'type': 'input', 'text': '用已有结果回答。'})
    done, _ = await wait_for(q, 'stream.done')
    assert len(ark.requests) == 1 and done['text'] == '当前没有后台任务结果。'
    assert not hasattr(actor.current, 'task_read_required')








async def test_quoted_stroke_list_as_literal_target_is_not_rejected_or_rewritten(make_actor):
    actor, q, ark = await make_actor([{'content': '横、竖。'}])
    await actor.post({'type': 'input', 'text': '“横、竖”怎么写？'})
    done, _ = await wait_for(q, 'stream.done')
    assert done['text'] == '横、竖。' and len(ark.requests) == 1 and not hasattr(actor.current, 'word_shape_only')


async def test_existing_records_car_state_question_does_not_read_unrelated_background_tasks(make_actor):
    actor, q, ark = await make_actor([{'content': '全部车窗都关着。'}])
    weather_tasks(actor)
    before = copy.deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '根据已有记录回答，现在全部车窗都关着吗？'})
    done, events = await wait_for(q, 'stream.done')
    assert len(ark.requests) == 1 and not hasattr(actor.current, 'task_read_required')
    assert done['text'] == '全部车窗都关着。' and done['metrics']['error'] is None
    assert actor.vehicle == before and not actor.current.tool_results
    assert not any(e['type'] == 'vehicle.command' for e in events)




@pytest.mark.parametrize('mode', ['research', None])
async def test_typed_weather_profile_rejects_before_registration_then_same_talker_selects_quick(make_actor, mode):
    actor, q, _ = await make_actor([])
    goal = '查询法国气象局官网，核对法国巴黎明天的温度，提供简短结论和官方来源'
    wrong_args = {'goal': goal, 'query_domain': 'weather', 'read_details': False}
    if mode is not None:
        wrong_args['query_mode'] = mode
    corrected_args = {**wrong_args, 'query_mode': 'quick'}
    gate = asyncio.Event()
    ark = actor.ark = HeldDeltaArk([
        {'tool_calls': [call('delegate_task', wrong_args, 'wrong-profile')]},
        {'tool_calls': [call('delegate_task', corrected_args, 'quick-profile')], 'gate': gate},
    ])
    await actor.post({'type': 'input', 'text': '查询法国气象局官网，核对法国巴黎明天的温度。'})
    async with asyncio.timeout(1):
        while len(ark.requests) < 2:
            await asyncio.sleep(.001)
    await actor.queue.join()
    assert actor.db.list_tasks(actor.id) == []
    refused = actor.current.tool_results['wrong-profile']
    assert refused['ok'] is False and refused['executed'] is False and refused['retry_model'] is True
    assert refused['code'] == 'weather_quick_required'
    assert refused['delegation_policy_guard']['requested_query_mode'] == mode
    original_call = next(c for m in actor.current.messages for c in m.get('tool_calls', []) if c['id'] == 'wrong-profile')
    assert json.loads(original_call['function']['arguments']) == wrong_args
    assert not any(e['type'] == 'ui.card' for e in q._queue)
    gate.set()
    done, events = await wait_for(q, 'stream.done')
    await actor.trace_queue.join()
    tasks = actor.db.list_tasks(actor.id)
    assert len(ark.requests) == 2 and len(tasks) == 1
    assert tasks[0]['goal'] == goal and tasks[0]['context']['query_domain'] == 'weather'
    assert tasks[0]['context']['query_mode'] == 'quick' and done['metrics']['error'] is None
    assert len([e for e in events if e['type'] == 'ui.card']) == 1
    traces = actor.db.traces_after(actor.id)
    error = next(e for e in traces if e['kind'] == 'tool.error' and e['call_id'] == 'wrong-profile')
    assert json.loads(error['data']['arguments']) == wrong_args
    assert error['data']['result']['code'] == 'weather_quick_required'
    assert any(e['kind'] == 'model.completed' and e['data']['message'].get('tool_calls') == [original_call] for e in traces)
    schema = next(t['function']['parameters']['properties'] for t in ark.requests[0]['tools'] if t['function']['name'] == 'delegate_task')
    assert 'weather' in schema['query_domain']['description'] and 'quick' in schema['query_domain']['description']


@pytest.mark.parametrize('mode', ['research', None])
async def test_nonweather_research_profile_and_existing_default_are_unchanged(make_actor, mode):
    args = {'goal': '比较科学工业城与自然博物馆官网信息和预约条件', 'query_domain': 'general'}
    if mode is not None:
        args['query_mode'] = mode
    actor, q, _ = await make_actor([{'tool_calls': [call('delegate_task', args, 'task')]}])
    await actor.post({'type': 'input', 'text': args['goal']})
    await wait_for(q, 'stream.done')
    result = actor.current.tool_results['task']
    assert result['ok'] is True and len(actor.db.list_tasks(actor.id)) == 1
    assert result['task']['context']['query_mode'] == 'research'


async def test_repeated_weather_research_parameters_stop_at_existing_five_call_limit(make_actor):
    wrong = call('delegate_task', {'goal': '法国巴黎明天天气', 'query_domain': 'weather', 'query_mode': 'research'})
    actor, q, ark = await make_actor([{'tool_calls': [wrong]}] * 5)
    await actor.post({'type': 'input', 'text': '查法国巴黎明天天气。'})
    done, events = await wait_for(q, 'stream.done')
    assert len(ark.requests) == 5 and done['metrics']['error'] == 'model_step_limit'
    assert actor.db.list_tasks(actor.id) == []
    assert not any(e['type'] in {'ui.card', 'vehicle.command'} for e in events)


async def test_relative_all_zone_feedback_reports_actual_nonuniform_targets_not_delta(make_actor):
    actor, q, _ = await make_actor([
        {'tool_calls': [call('adjust_temperature', {'zone': 'all', 'delta': 1}, 'relative')]},
    ])
    actor.vehicle['climate']['temperatures'].update(driver=20, front_passenger=22, rear=24)
    await actor.post({'type': 'input', 'text': '所有温区都调高一度。'})
    done, events = await wait_for(q, 'stream.done', actor)
    receipt = next(e for e in events if e['type'] == 'vehicle.result')['result']
    assert receipt['value'] == actor.vehicle['climate']['temperatures'] == {
        'driver': 21, 'front_passenger': 23, 'rear': 25}
    assert all(value in done['text'] for value in ('二十一', '二十三', '二十五'))
    assert '调到一度' not in done['text'] and '设为一度' not in done['text']


async def test_hot_seat_backrest_feedback_preserves_actual_angle_unit(make_actor):
    actor, q, _ = await make_actor([
        {'tool_calls': [call('set_seat_position', {'seat': 'driver', 'axis': 'backrest', 'position': 110}, 'backrest')]},
    ])
    await actor.post({'type': 'input', 'text': '主驾靠背调到110度。'})
    done, events = await wait_for(q, 'stream.done', actor)
    receipt = next(e for e in events if e['type'] == 'vehicle.result')['result']
    assert receipt['value'] == actor.vehicle['seats']['positions']['driver']['backrest'] == 110
    assert '靠背' in done['text'] and '度' in done['text']
    assert '百分之' not in done['text']


@pytest.mark.parametrize('utterance', [
    '用已有结果回答刚才那个任务。',
    '“天气”这两个字怎么写？不要查天气。',
])
async def test_prompt_decisions_do_not_create_text_classification_or_rejection_retry(make_actor, utterance):
    # The package deliberately assigns meaning to the model; the actor no longer
    # classifies these strings or rejects plain answers with a hidden retry.
    answer = '这是测试模型提供的直接回答。'
    actor, q, ark = await make_actor([{'content': answer}])
    weather_tasks(actor)
    await actor.post({'type': 'input', 'text': utterance})
    done, events = await wait_for(q, 'stream.done')
    await actor.trace_queue.join()
    assert done['text'] == answer and len(ark.requests) == 1
    assert not any(hasattr(actor.current, field) for field in (
        'task_read_required', 'task_read_rejections', 'word_shape_only', 'word_scope_rejections'))
    assert not any(event['kind'] == 'model.output_rejected' for event in actor.db.traces_after(actor.id))
    assert not any(event['type'] == 'vehicle.command' for event in events)
    snapshots = [message['content'] for message in ark.requests[0]['messages']
                 if message['role'] == 'system' and message['content'].startswith('当前会话快照')]
    assert len(snapshots) == 1
    assert 'answer_constraints' not in json.loads(snapshots[0].splitlines()[-1])


async def test_actor_never_calls_sqlite_on_the_event_loop_thread(tmp_path):
    """Invariant 2: every database call from the actor runs in a worker thread."""
    import threading
    loop_thread = threading.get_ident()
    guarded = {'create_task', 'mutate_task', 'list_tasks', 'get_task', 'events_after', 'append_trace', 'save_task'}

    class GuardedDatabase(Database):
        def __getattribute__(self, name):
            value = object.__getattribute__(self, name)
            if name not in guarded:
                return value
            def checked(*args, **kwargs):
                if threading.get_ident() == loop_thread:
                    raise AssertionError(f'{name} called on the event loop')
                return value(*args, **kwargs)
            return checked

    db = GuardedDatabase(tmp_path / 'guarded.sqlite3')
    ark = FakeArk([{'tool_calls': [call('delegate_task', {'goal': '比较两家酒店'}, 'create')]},
                   {'tool_calls': [call('manage_task', {'action': 'update', 'text': '只看有停车场的'}, 'update')]}])
    actor = SessionActor('guarded', db, ark, vehicle_planner=FakeVehiclePlanner())
    try:
        q = await actor.attach()
        await q.get()
        await actor.post({'type': 'input', 'text': '比较两家酒店'})
        await wait_for(q, 'stream.done')
        task_id = actor.current.tool_results['create']['task']['id']
        await actor.post({'type': 'input', 'text': '只看有停车场的'})
        await wait_for(q, 'stream.done')
        claimed = await asyncio.to_thread(db.claim_next)
        await asyncio.to_thread(db.save_task, claimed['id'], claimed['revision'], status='completed',
                                result={'summary': '两家都有停车场'}, summary='完成')
        done, _ = await wait_for(q, 'stream.done', timeout=3)
        assert done['text'] == '两家都有停车场。' and actor.tasks[task_id]['status'] == 'completed'
    finally:
        await actor.close()


@pytest.mark.parametrize('preset', ['professional', 'playful'])
async def test_one_streamed_call_carries_the_persona_and_tools(make_actor, preset):
    """The decision and the answer are one call; persona sits right after the fixed rules."""
    from backend.persona import PERSONAS, style_prompt
    actor, q, ark = await make_actor([{'content': '秋天很适合出游。'}])
    await actor.post({'type': 'persona.set', 'preset': preset})
    await actor.post({'type': 'input', 'text': '聊聊秋天'})
    done, _ = await wait_for(q, 'stream.done')
    (request,) = ark.requests
    assert request['tools'] and request['messages'][1]['content'] == style_prompt(PERSONAS[preset])
    assert done['text'] == '秋天很适合出游。' and done['metrics']['model_calls'] == 1


async def test_text_is_spoken_while_the_model_is_still_generating(tmp_path):
    gate = asyncio.Event()

    class GatedArk:
        async def stream(self, messages, tools):
            yield {'type': 'delta', 'text': '这就把主驾调到二十三度。'}
            await gate.wait()
            yield {'type': 'message', 'message': {'role': 'assistant', 'content': '这就把主驾调到二十三度。',
                   'tool_calls': [call('set_temperature', {'zone': 'driver', 'temperature': 23})]}}

    actor = SessionActor('stream', Database(tmp_path / 's.sqlite3'), GatedArk(), vehicle_planner=FakeVehiclePlanner())
    try:
        q = await actor.attach(); await q.get()
        await actor.post({'type': 'input', 'text': '主驾调到23度'})
        await asyncio.sleep(.05)
        await actor.queue.join()
        assert actor.current.metrics.get('first_speech_ms') is not None  # before the tool call arrived
        gate.set()
        done, _ = await wait_for(q, 'stream.done', actor)
        assert done['metrics']['preface'] == '这就把主驾调到二十三度。'
        assert actor.vehicle['climate']['temperatures']['driver'] == 23
    finally:
        await actor.close()


def test_first_clause_is_fed_at_a_comma_later_ones_at_sentence_ends(tmp_path):
    actor = object.__new__(SessionActor)
    fed = []
    actor.feed_audio = lambda turn, text, source: fed.append(text)
    turn = Turn('t', 'x', None, 1)
    turn.sentence_buffer = '好，这就把主驾调到二十三度，音量调到三十五，副驾不动。'
    actor.feed_sentences(turn)
    assert fed == ['好，这就把主驾调到二十三度，', '音量调到三十五，副驾不动。']


async def test_voice_activity_stops_speech_but_keeps_dispatched_vehicle_command(make_actor):
    actor, q, _ = await make_actor([{'content': '这就关车窗。', 'tool_calls': [call('close_all_windows', id='w')]}])
    actor.vehicle['windows']['positions']['driver'] = 40
    actor.simulator.state['windows']['positions']['driver'] = 40
    await actor.post({'type': 'input', 'text': '关上车窗'})
    command, _ = await wait_for(q, 'vehicle.command')
    turn = actor.current
    await actor.post({'type': 'asr.capture', 'capture_id': 'cough', 'active': True, 'interrupt': True})
    await actor.queue.join()
    assert turn.audio_cancelled.is_set() and not turn.interrupted
    receipt = await actor.simulator.execute(command['command_id'])
    await actor.post({'type': 'vehicle.receipt', 'command_id': command['command_id'], 'receipt_id': receipt['receipt_id']})
    done, _ = await wait_for(q, 'stream.done')
    assert receipt['ok'] and actor.vehicle['windows']['positions']['driver'] == 0
