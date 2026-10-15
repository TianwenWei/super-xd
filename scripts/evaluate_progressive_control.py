"""Frozen 15/85 control scenarios, with real HTTP/WS decisions and receipts.

Default --plan is network-free. --run uses the configured real Mini/Pro clients.
The oracle compares requested operations and committed state independently of
the plan's own completeness claim. All attempts are preserved, never retried by
this evaluator. This is a text/control test, not a microphone/audio benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

import httpx

from backend.catalog import HOT_TOOL_NAMES, COLD_TOOL_NAMES
from scripts.evaluate_interactions import LiveSession, canonical, changed_paths, flatten, path_allowed

ORACLE_VERSION = 'progressive-vehicle-control-v1'


def op(name, **arguments):
    return {'name': name, 'arguments': arguments}


def case(id, text, actions=(), targets=None, changes=None, route='either', **extra):
    return {'id': id, 'text': text, 'actions': list(actions), 'targets': targets or {},
            'allowed_changes': list(changes if changes is not None else (targets or {})),
            'route': route, **extra}


SCENARIOS = [
    {'id': 'hot_core', 'steps': [
        case('all23', '把所有温区的空调温度设为23度。', [op('set_temperature', zone='all', temperature=23)],
             {'climate.power': True, **{f'climate.temperatures.{z}': 23 for z in ['driver','front_passenger','rear']}}, route='hot'),
        case('massage', '只打开主驾座椅的舒缓按摩，不要开启加热。', [op('set_seat_massage', seat='driver', mode='relax')],
             {'seats.positions.driver.massage':'relax'}, route='hot'),
        case('ventilation', '把副驾座椅通风设为2档。', [op('set_seat_ventilation', seat='front_passenger', level=2)],
             {'seats.positions.front_passenger.ventilation':2}, route='hot'),
        case('seat_height', '把主驾座椅高度设为60。', [op('set_seat_position', seat='driver', axis='height', position=60)],
             {'seats.positions.driver.height':60}, route='hot'),
        case('window50', '主驾车窗打开一半。', [op('set_window_position', window='driver', position=50)],
             {'windows.positions.driver':50}, route='hot'),
        case('window_state', '现在主驾车窗开到多少？', route='read', answer_pattern=r'一半|50|五十'),
    ]},
    {'id': 'cold_mirrors', 'steps': [
        case('heat_on', '打开后视镜加热。', [op('set_mirror_heating', enabled=True)], {'mirrors.heating':True}, route='pro'),
        case('heat_state', '后视镜加热现在开启了吗？', route='read', answer_pattern=r'开启|打开|开着|开了',
             forbidden_answer=r'未开启|没(?:有)?开|关闭|关着'),
        case('fold', '折叠两侧后视镜。', [op('set_mirror_fold', folded=True)], {'mirrors.folded':True}, route='pro'),
        case('unfold_correction', '不是继续折叠，是把两侧后视镜展开。', [op('set_mirror_fold', folded=False)],
             {'mirrors.folded':False}, route='pro'),
    ]},
    {'id': 'mixed_and_holds', 'steps': [
        case('passenger50', '先把副驾车窗打开一半。', [op('set_window_position', window='front_passenger', position=50)],
             {'windows.positions.front_passenger':50}, route='hot'),
        case('mixed_temperature_mirror', '空调温度设为23度，打开后视镜加热，副驾车窗别动。',
             [op('set_temperature', zone='all', temperature=23), op('set_mirror_heating', enabled=True)],
             {'climate.power':True, **{f'climate.temperatures.{z}':23 for z in ['driver','front_passenger','rear']},
              'mirrors.heating':True}, route='pro', preserved={'windows.positions.front_passenger':50}),
        case('light_attributes_and_lock', '氛围灯开关别动，颜色改紫色、亮度设45%，并启用左后门儿童安全锁。',
             [op('set_ambient_color', color='purple'), op('set_ambient_brightness', brightness=45),
              op('set_child_lock', side='left', enabled=True)],
             {'lights.ambient_color':'purple','lights.ambient_brightness':45,'locks.child_lock.left':True},
             route='pro', preserved={'lights.ambient':False}),
        case('mixed_locks_and_ambient', '打开氛围灯，解除左后门儿童安全锁，启用右后门儿童安全锁，颜色和亮度别动。',
             [op('set_ambient_light', enabled=True),op('set_child_lock',side='left',enabled=False),
              op('set_child_lock',side='right',enabled=True)],
             {'lights.ambient':True,'locks.child_lock.left':False,'locks.child_lock.right':True}, route='pro',
             preserved={'lights.ambient_color':'purple','lights.ambient_brightness':45}),
        case('remaining_windows', '副驾车窗别动，把其余三扇窗都关上。',
             [op('set_window_position',window=w,position=0) for w in ['driver','rear_left','rear_right']],
             {f'windows.positions.{w}':0 for w in ['driver','rear_left','rear_right']},
             preserved={'windows.positions.front_passenger':50}),
    ]},
    {'id': 'cold_wipers', 'steps': [
        case('low', '把前雨刷设为低速。', [op('set_wiper_mode',mode='low')], {'wipers.mode':'low'}, route='pro'),
        case('auto_and_sensitivity', '改成自动模式，并把雨量灵敏度设为2档。',
             [op('set_wiper_mode',mode='auto'),op('set_rain_sensitivity',level=2)],
             {'wipers.mode':'auto','wipers.rain_sensitivity':2}, route='pro'),
    ]},
    {'id': 'cold_tailgate', 'steps': [
        case('height70', '把尾门下次开启高度设为70%。', [op('set_tailgate_height',height=70)],
             {'tailgate.height':70}, route='pro'),
        case('open_it', '现在打开它。', [op('set_tailgate_open',open=True)],
             {'tailgate.open':True,'tailgate.position':70,'tailgate.last_action':'opened'},
             changes=['tailgate.open','tailgate.position','tailgate.moving','tailgate.last_action'],route='pro'),
        case('close_it', '把它关上。', [op('set_tailgate_open',open=False)],
             {'tailgate.open':False,'tailgate.position':0,'tailgate.last_action':'closed'},
             changes=['tailgate.open','tailgate.position','tailgate.moving','tailgate.last_action'],route='pro'),
    ]},
    {'id': 'dependency_order', 'steps': [
        case('window_lock_on', '启用后排车窗童锁。', [op('set_window_child_lock',enabled=True)],
             {'windows.child_lock':True}, route='pro'),
        case('unlock_then_open', '先解除后排车窗童锁，再把左后车窗开到30%，其他设置不变。',
             [op('set_window_child_lock',enabled=False),op('set_window_position',window='rear_left',position=30)],
             {'windows.child_lock':False,'windows.positions.rear_left':30},route='pro',
             ordered_names=['set_window_child_lock','set_window_position']),
        case('close_rear', '把左后车窗关上。', [op('set_window_position',window='rear_left',position=0)],
             {'windows.positions.rear_left':0},route='hot'),
    ]},
    {'id': 'missing_location', 'steps': [
        case('airport', '导航去最近的机场。',route='clarify',answer_pattern=r'哪里|城市|地址|位置|出发'),
    ]},
]


def has_value(value, target):
    if value == target:
        return True
    if isinstance(value, dict):
        return any(has_value(item, target) for item in value.values())
    if isinstance(value, list):
        return any(has_value(item, target) for item in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return False
        return has_value(parsed, target) if parsed != value else False
    return False


def evaluate(spec, row, expected_model):
    errors = list(row.get('errors', []))
    actual = [{'name':item['name'],'arguments':item['arguments']} for item in row['commands']]
    reads = []
    # Read-only commands are permitted solely for declared read scenarios.
    if spec['route'] == 'read':
        reads = [item for item in actual if item['name'].startswith('get_')]
        actual = [item for item in actual if not item['name'].startswith('get_')]
    if Counter(map(canonical,actual)) != Counter(map(canonical,spec['actions'])):
        errors.append({'wrong_commands':actual,'expected':spec['actions']})
    if len(row['receipts']) != len(row['commands']):
        errors.append('receipt_count_mismatch')
    for command in row['commands']:
        receipt = next((item for item in row['receipts'] if item['command_id']==command['command_id']), None)
        result = (receipt or {}).get('result', {})
        if (result.get('ok') is not True or result.get('name') != command['name']
                or canonical(result.get('arguments')) != canonical(command['arguments'])
                or not isinstance(result.get('state'),dict)):
            errors.append({'invalid_actual_receipt':command['command_id']})
    before = row['before']['vehicle']; after = row['after']['vehicle']; values = flatten(after)
    changes = changed_paths(before,after); row['changed_paths'] = changes
    unexpected = [path for path in changes if not path_allowed(path,spec['allowed_changes'])]
    if unexpected: errors.append({'extra_state_changes':unexpected})
    for path,value in {**spec['targets'],**spec.get('preserved',{})}.items():
        if values.get(path) != value: errors.append({'wrong_state':path,'expected':value,'actual':values.get(path)})
    if spec.get('ordered_names') and [item['name'] for item in actual] != spec['ordered_names']:
        errors.append('dependency_order_not_preserved')
    text = row.get('text','')
    if spec.get('answer_pattern') and not re.search(spec['answer_pattern'],text): errors.append('answer_missing_expected_fact_or_question')
    if spec.get('forbidden_answer') and re.search(spec['forbidden_answer'],text): errors.append('answer_contradicts_actual_state')
    traces = [event for event in row['traces'] if event.get('turn_id')==row.get('turn_id')]
    if not any(event['kind']=='turn.completed' for event in traces):
        errors.append('missing_turn_completion_trace')
    if row.get('trace_errors'):
        errors.append({'trace_recording_errors':row['trace_errors']})
    mini = [event for event in traces if event['kind']=='model.started' and event.get('data',{}).get('agent')=='talker']
    required_tools = set(HOT_TOOL_NAMES) | {'delegate_vehicle_control','delegate_task','manage_task'}
    for event in mini:
        names = [tool['function']['name'] for tool in event['data']['tools']]
        # Other read-only foreground capabilities (e.g. concurrent Maps work)
        # are outside the 15/85 vehicle partition and do not weaken it.
        if (len(names)!=len(set(names)) or not required_tools.issubset(names)
                or set(names)&set(COLD_TOOL_NAMES) or 'load_skill' in names):
            errors.append({'talker_capability_boundary_broken':names})
    pro = [event for event in traces if event['kind']=='model.completed' and event.get('data',{}).get('agent')=='vehicle_planner']
    started = [event for event in traces if event['kind']=='model.started' and event.get('data',{}).get('agent')=='vehicle_planner']
    row['mini_calls'] = len(mini); row['pro_calls'] = len(pro); row['actual_read_commands'] = reads
    row['pro_requests'] = len(started)
    row['pro_completed_calls'] = len(pro)
    if not mini: errors.append('missing_mini_request_trace')
    if spec['route']=='hot' and pro: errors.append('hot_path_unexpected_pro_call')
    if spec['route']=='pro' and not pro: errors.append('missing_actual_pro_decision')
    for event in pro:
        if event['data'].get('meta',{}).get('model') != expected_model:
            errors.append({'wrong_planner_model':event['data'].get('meta',{}).get('model')})
    if pro:
        if not any(has_value(event['data'].get('messages',[]),spec['text']) for event in started):
            errors.append('planner_did_not_receive_verbatim_original_request')
        accepted = [event for event in traces if event['kind']=='vehicle.plan.accepted']
        completed = [event for event in traces if event['kind']=='vehicle.plan.completed']
        row['vehicle_plan_acceptances'] = accepted; row['vehicle_plan_completions'] = completed
        if row['commands'] and (not accepted or not completed):
            errors.append('missing_plan_or_execution_ledger')
        for completion in completed:
            ledger = completion['data']['ledger']
            if ledger.get('ok') is not True and spec['actions']:
                errors.append({'plan_not_completed':ledger.get('status')})
            if ledger.get('ok') is True:
                if not ledger.get('intents') or any(intent['status'] not in {'completed','kept'} for intent in ledger['intents']):
                    errors.append('unresolved_intent_claimed_complete')
                for item in ledger.get('action_receipts',[]):
                    wire = next((command for command in row['commands'] if command['command_id']==item.get('command_id')),None)
                    receipt = next((event for event in row['receipts'] if event['command_id']==item.get('command_id')),None)
                    if (item.get('status')!='completed' or not wire or not receipt
                            or item['name']!=wire['name'] or canonical(item['arguments'])!=canonical(wire['arguments'])
                            or canonical(item['result'])!=canonical(receipt['result'])
                            or wire.get('action_id')!=item['action_id'] or wire.get('intent_id')!=item['intent_id']):
                        errors.append({'ledger_not_bound_to_actual_receipt':item.get('action_id')})
                bound_ids = {item.get('command_id') for item in ledger.get('action_receipts',[])}
                if any(command.get('parent_call_id')==completion.get('call_id') and command['command_id'] not in bound_ids
                       for command in row['commands']):
                    errors.append('executed_action_missing_from_ledger')
    row['raw_model_attempts'] = [event for event in traces if event['kind']=='model.completed']
    row['guarded_attempts'] = [event for event in traces if event['kind'] in ['tool.error','model.output_rejected','vehicle.plan.rejected']]
    if row.get('metrics',{}).get('error'): errors.append({'turn_error':row['metrics']['error']})
    row['assertion_failures'] = errors; row['pass'] = not errors
    row['language_scope'] = 'Exact action/receipt/state and bounded fact claims only; open-ended language needs human audit.'


class ProgressiveSession(LiveSession):
    async def run_case(self, spec, expected_model):
        row = {'step_id':spec['id'],'query':spec['text'],'expectation':deepcopy(spec),'session_id':self.id,
               'client_request_id':uuid4().hex,'commands':[],'receipts':[],'ws_events':[],'errors':[],'pass':False}
        try:
            row['before'] = await self.snapshot(); row['_started'] = time.perf_counter()
            row['_done'] = asyncio.get_running_loop().create_future(); self.pending[row['client_request_id']] = row
            await self.ws.send(json.dumps({'type':'input','text':spec['text'],'client_request_id':row['client_request_id']},ensure_ascii=False))
            await asyncio.wait_for(asyncio.shield(row['_done']),self.timeout)
            row['after'] = await self.snapshot(); row['traces'] = deepcopy(await self.traces(row['turn_id']))
            row['trace_errors'] = deepcopy(self.trace_errors)
            evaluate(spec,row,expected_model)
        except Exception as exc:
            row['errors'].append(f'{type(exc).__name__}: {str(exc)[:240]}'); row['assertion_failures'] = list(row['errors'])
            try: row['after'] = await self.snapshot(); row['traces'] = deepcopy(await self.traces(row.get('turn_id')))
            except Exception: pass
        finally:
            row.pop('_started',None); future = row.pop('_done',None)
            if future and not future.done(): future.cancel()
            self.pending.pop(row['client_request_id'],None); self.turns.pop(row.get('turn_id'),None)
        return row


async def run(args):
    scenarios = deepcopy(SCENARIOS)
    if args.scenario: scenarios = [item for item in scenarios if item['id'] in args.scenario]
    if not args.run:
        print(json.dumps({'oracle_version':ORACLE_VERSION,'planned_turns':sum(len(g['steps']) for g in scenarios),
                          'requests_sent':0,'scenarios':scenarios},ensure_ascii=False,indent=2)); return 0
    output = Path(args.output)
    report = {'oracle_version':ORACLE_VERSION,'created_at':datetime.now(timezone.utc).isoformat(),'base':args.base,
              'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'groups':[],
              'planned_turns':sum(len(g['steps']) for g in scenarios),'run_status':'running',
              'boundary':'Real text HTTP/control WS and actual mock-state commit. No ASR/TTS/browser/acoustic measurement.',
              'no_result_replacement':True,'no_evaluator_model':True}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as handle: json.dump(report,handle,ensure_ascii=False,indent=2)
    def save(): output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    async with httpx.AsyncClient(timeout=20) as client:
        config = (await client.get(args.base+'/api/config')).json(); report['config'] = {k:v for k,v in config.items() if k not in ['personas','catalog']}
        expected_model = config['model']['vehicle_planner']
        for spec in scenarios:
            group = {'id':spec['id'],'records':[]}; report['groups'].append(group); save()
            live = ProgressiveSession(client,args.base,args.timeout)
            try:
                await live.open(); group['session_id']=live.id; group['initial']=live.initial
                for item in spec['steps']:
                    row = await live.run_case(item,expected_model); group['records'].append(row); save()
                    print(spec['id']+'/'+item['id'], 'PASS' if row['pass'] else 'FAIL',row.get('client_total_ms'),flush=True)
            except Exception as exc: group['error']=f'{type(exc).__name__}: {str(exc)[:240]}'
            finally:
                group['cleanup']=await live.close(); group['traces']=list(live.trace_events.values()); group['trace_errors']=live.trace_errors; save()
    rows = [row for group in report['groups'] for row in group['records']]
    report['summary']={'planned':report['planned_turns'],'attempted':len(rows),'passed':sum(row['pass'] for row in rows),
                       'failed':sum(not row['pass'] for row in rows),
                       'groups_with_errors':sum(bool(g.get('error') or g.get('trace_errors')) for g in report['groups'])}
    report['run_status']='completed'; report['finished_at']=datetime.now(timezone.utc).isoformat(); save()
    print(report['summary'],flush=True)
    return 0 if len(rows)==report['planned_turns'] and all(row['pass'] for row in rows) and not report['summary']['groups_with_errors'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--base',default='http://127.0.0.1:8003')
    parser.add_argument('--scenario',action='append')
    parser.add_argument('--output',default='docs/progressive-control-live.json')
    parser.add_argument('--timeout',type=float,default=120)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
