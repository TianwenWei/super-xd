"""Explicit FakeArk outputs only: no provider, network or actual car execution."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.catalog import HOT_TOOL_NAMES, SKILLS, TOOLS, execute_tool, initial_vehicle_state
from backend.settings import VEHICLE_CONTROL_MODEL
from backend.vehicle_planner import (VehicleControlPlanner, plan_schema, validate_vehicle_action_keep,
                                     validate_vehicle_plan, _submission_schema as submission_schema,
                                     _resolve_source_spans as resolve_source_spans)


def action(identity, intent, name, arguments, depends=()):
    return {'id': identity, 'intent_id': intent, 'name': name,
            'arguments': arguments, 'depends_on': list(depends)}


def intent(identity, query, source=None, *, actions=(), keep=(), status='planned', completed=(), question='', reason=''):
    source = query if source is None else source
    start = query.index(source)
    return {'id': identity, 'source_text': source, 'source_start': start, 'source_end': start + len(source),
            'status': status, 'action_ids': list(actions), 'keep_paths': list(keep),
            'completed_operation_ids': list(completed), 'question': question, 'reason': reason}


def plan(intents, actions=(), *, status='planned', keep=(), question='', reason=''):
    return {'status': status, 'intents': intents, 'actions': list(actions),
            'keep_paths': list(keep), 'question': question, 'reason': reason}


def temperature_plan(query='把所有温区设为23度。'):
    return plan([intent('temperature', query, actions=['temperature'])],
                [action('temperature', 'temperature', 'set_temperature', {'zone': 'all', 'temperature': 23})])


def tool(name, arguments, identity='submit'):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def wire_plan(value):
    value=deepcopy(value)
    for item in value.get('intents',[]):
        item.pop('source_start',None)
        item.pop('source_end',None)
    return value


def response(value, *, name='submit_vehicle_plan', identity='submit'):
    if name=='submit_vehicle_plan':
        value=wire_plan(value)
    return {'role': 'assistant', 'content': None, 'tool_calls': [tool(name, value, identity)],
            '_meta': {'model': VEHICLE_CONTROL_MODEL, 'usage': None, 'provider_id': identity}}


class FakeArk:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def complete(self, messages, tools=None, **kwargs):
        self.requests.append({'messages': deepcopy(messages), 'tools': deepcopy(tools), **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, asyncio.Event):
            await reply.wait()
            raise AssertionError('test should cancel instead of unblocking provider')
        return deepcopy(reply)


def names(request):
    return {tool['function']['name'] for tool in request['tools']}


def action_names(request):
    submit = next(t for t in request['tools'] if t['function']['name'] == 'submit_vehicle_plan')
    variants = submit['function']['parameters']['properties']['actions']['items']['anyOf']
    return {item['properties']['name']['enum'][0] for item in variants}


async def test_one_pro_submission_returns_bound_plan_and_only_simulates_private_state():
    query = '把所有温区设为23度。'
    state = initial_vehicle_state(); before = deepcopy(state)
    raw = response(temperature_plan(query))
    ark = FakeArk([raw]); traces = []
    result = await VehicleControlPlanner(ark).plan(query, state, [], trace=lambda kind, data, **fields: traces.append((kind, data, fields)))
    assert result['status'] == 'planned' and result['ok'] and not result['executed']
    assert result['model_calls'] == 1 and result['loaded_skills'] == []
    assert state == before and result['simulation']['executed'] is False
    assert result['simulation']['final_state']['climate']['temperatures'] == {'driver': 23, 'front_passenger': 23, 'rear': 23}
    assert result['simulation']['actions'][0]['executed'] is False
    assert result['intents'][0]['text'] == query
    assert result['validation']['semantic_coverage_proven'] is False
    assert ark.requests[0]['model'] == VEHICLE_CONTROL_MODEL and ark.requests[0]['thinking'] is False
    assert ark.requests[0]['max_tokens'] == 2000
    assert result['model_observations'][0]['raw_return'] == raw
    assert result['model_observations'][0]['meta']['usage'] is None
    assert result['provenance']['submit_call_id'] == 'submit'
    completed = next(item for item in traces if item[0] == 'model.completed')
    assert completed[2]['call_id'] == result['model_call_id'] and completed[1]['message']['content'] is None
    assert not any(kind.startswith('vehicle.receipt') for kind, _, _ in traces)


async def test_initial_tools_are_hot15_plus_only_requested_skills_and_submit_schema_is_dynamic():
    query = '开后视镜加热。'
    value = plan([intent('mirror', query, actions=['mirror'])],
                 [action('mirror', 'mirror', 'set_mirror_heating', {'enabled': True})])
    ark = FakeArk([response(value)])
    result = await VehicleControlPlanner(ark).plan(query, initial_vehicle_state(), ['mirrors'])
    assert result['ok']
    request = ark.requests[0]
    payload=json.loads(request['messages'][1]['content'])
    assert payload['loaded_skills']==['mirrors']
    assert set(payload['available_action_names'])==set(HOT_TOOL_NAMES)|set(SKILLS['mirrors'].tool_names)
    actual = action_names(request)
    assert len(HOT_TOOL_NAMES) == 15
    assert names(request) == {'load_skill', 'submit_vehicle_plan'}
    assert actual == set(HOT_TOOL_NAMES) | set(SKILLS['mirrors'].tool_names)
    assert 'set_child_lock' not in actual and 'set_door_lock' not in actual
    submit = next(t for t in request['tools'] if t['function']['name'] == 'submit_vehicle_plan')
    variants = submit['function']['parameters']['properties']['actions']['items']['anyOf']
    assert {item['properties']['name']['enum'][0] for item in variants} == actual
    mirror = next(item for item in variants if item['properties']['name']['enum'] == ['set_mirror_heating'])
    assert mirror['properties']['arguments'] == TOOLS['set_mirror_heating'].parameters


async def test_pro_load_skill_expands_only_future_pro_tools_and_preserves_load_evidence():
    query = '启用左后儿童安全锁。'
    value = plan([intent('child', query, actions=['child'])],
                 [action('child', 'child', 'set_child_lock', {'side': 'left', 'enabled': True})])
    ark = FakeArk([response({'skill_id': 'locks'}, name='load_skill', identity='load'), response(value, identity='planned')])
    hot_before = list(HOT_TOOL_NAMES)
    result = await VehicleControlPlanner(ark).plan(query, initial_vehicle_state(), [])
    assert result['ok'] and result['loaded_skills'] == ['locks'] and result['model_calls'] == 2
    assert 'set_child_lock' not in action_names(ark.requests[0]) and 'set_child_lock' in action_names(ark.requests[1])
    assert json.loads(ark.requests[1]['messages'][1]['content'])['loaded_skills']==['locks']
    assert 'set_child_lock' in json.loads(ark.requests[1]['messages'][1]['content'])['available_action_names']
    assert HOT_TOOL_NAMES == hot_before and result['load_evidence'][0]['result']['ok']
    assert {t['name'] for t in result['load_evidence'][0]['result']['tools']} == set(SKILLS['locks'].tool_names)


async def test_direct_vehicle_function_call_is_not_executed_or_accepted_as_a_plan():
    query = '把所有温区设为23度。'; state = initial_vehicle_state(); before = deepcopy(state)
    ark = FakeArk([response({'zone': 'all', 'temperature': 23}, name='set_temperature', identity='direct'),
                   response(temperature_plan(query))])
    result = await VehicleControlPlanner(ark).plan(query, state, [])
    assert result['ok'] and state == before and result['model_calls'] == 2
    refusal = next(e for e in result['trace_events'] if e['kind'] == 'tool.error')
    assert 'plan_submission_required' in refusal['data']['result']['error']
    assert refusal['data']['result']['executed'] is False


@pytest.mark.parametrize('mutation', [
    'unknown_tool', 'cold_unloaded', 'string_bool', 'bad_number', 'extra_argument', 'source_changed',
    'source_offset', 'empty_source', 'uncovered_clause', 'duplicate_intent_id', 'duplicate_source_span',
    'unknown_intent', 'wrong_binding', 'duplicate_action_id', 'duplicate_action',
    'unknown_dependency', 'self_dependency', 'cycle', 'unresolved_planned', 'empty_planned', 'extra_top_field',
    'intent_question', 'plan_question',
])
def test_malformed_plan_is_rejected_whole_before_real_state_changes(mutation):
    query = '把所有温区设为23度。'; value = temperature_plan(query)
    state = initial_vehicle_state(); before = deepcopy(state)
    a, i = value['actions'][0], value['intents'][0]
    if mutation == 'unknown_tool': a['name'] = 'invented_vehicle_function'
    elif mutation == 'cold_unloaded': a.update(name='set_child_lock', arguments={'side': 'left', 'enabled': True})
    elif mutation == 'string_bool': a.update(name='set_climate_power', arguments={'enabled': 'true'})
    elif mutation == 'bad_number': a['arguments']['temperature'] = 100
    elif mutation == 'extra_argument': a['arguments']['enabled'] = True
    elif mutation == 'source_changed': i['source_text'] = '请打开车窗'
    elif mutation == 'source_offset': i['source_start'] += 1
    elif mutation == 'empty_source': i['source_text'] = ''
    elif mutation == 'uncovered_clause':
        query += '音量设为25。'
    elif mutation == 'duplicate_intent_id': value['intents'].append(deepcopy(i))
    elif mutation == 'duplicate_source_span':
        other = deepcopy(i); other.update(id='other', action_ids=[], status='keep', keep_paths=['media.volume']); value['intents'].append(other); value['keep_paths']=['media.volume']
    elif mutation == 'unknown_intent': a['intent_id'] = 'missing'
    elif mutation == 'wrong_binding': i['action_ids'] = []
    elif mutation == 'duplicate_action_id': value['actions'].append(deepcopy(a))
    elif mutation == 'duplicate_action':
        other = deepcopy(a); other['id']='duplicate'; value['actions'].append(other); i['action_ids'].append('duplicate')
    elif mutation == 'unknown_dependency': a['depends_on'] = ['missing']
    elif mutation == 'self_dependency': a['depends_on'] = [a['id']]
    elif mutation == 'cycle':
        other=action('second','temperature','set_volume',{'volume':25},['temperature']); a['depends_on']=['second']; value['actions'].append(other); i['action_ids'].append('second')
    elif mutation == 'unresolved_planned': i.update(status='clarification', question='哪个温区？')
    elif mutation == 'empty_planned': value['actions']=[]; i['action_ids']=[]
    elif mutation == 'extra_top_field': value['fake_receipt']={'ok':True}
    elif mutation == 'intent_question': i['question']='到底哪个温区？'
    elif mutation == 'plan_question': value['question']='请确认温度。'
    if mutation == 'uncovered_clause':
        result = validate_vehicle_plan(value, query, state)
        assert result['validation']['warnings'][0]['code'] == 'unaccounted_request_text'
    else:
        with pytest.raises(ValueError):
            validate_vehicle_plan(value, query, state)
    assert state == before


def test_dependency_execution_order_is_topological_and_arguments_are_not_rewritten():
    query = '先把全部温区设23度，再把后排设21度。'
    value=plan([intent('climate',query,actions=['rear','all'])],
               [action('rear','climate','set_temperature',{'zone':'rear','temperature':21},['all']),
                action('all','climate','set_temperature',{'zone':'all','temperature':23})])
    original=deepcopy(value);state=initial_vehicle_state()
    result=validate_vehicle_plan(value,query,state)
    assert [a['id'] for a in result['actions']]==['all','rear']
    assert result['simulation']['final_state']['climate']['temperatures']=={'driver':23,'front_passenger':23,'rear':21}
    assert value==original and state['climate']['temperatures']['rear']==22


def test_duplicate_numeric_equivalent_actions_cannot_bypass_duplicate_guard():
    query='所有温区调23度。';value=temperature_plan(query)
    value['actions'].append(action('same-float','temperature','set_temperature',{'zone':'all','temperature':23.0}))
    value['intents'][0]['action_ids'].append('same-float')
    with pytest.raises(ValueError,match='duplicate_action'):
        validate_vehicle_plan(value,query,initial_vehicle_state())


def window_keep_plan():
    query='副驾车窗别动，把其余三扇窗都关上。';keep='windows.positions.front_passenger'
    value=plan([intent('hold',query,'副驾车窗别动',status='keep',keep=[keep]),
                intent('close',query,'把其余三扇窗都关上。',actions=['driver','rear_left','rear_right'])],
               [action(seat,'close','set_window_position',{'window':seat,'position':0}) for seat in ('driver','rear_left','rear_right')],keep=[keep])
    state=initial_vehicle_state();state['windows']['positions'].update(driver=10,front_passenger=50,rear_left=20,rear_right=30)
    return query,value,state


def test_declared_keep_path_preserved_at_each_simulated_action():
    query,value,state=window_keep_plan();before=deepcopy(state)
    result=validate_vehicle_plan(value,query,state)
    assert state==before and result['simulation']['final_state']['windows']['positions']=={'driver':0,'front_passenger':50,'rear_left':0,'rear_right':0}


def test_public_action_keep_check_rejects_declared_noop_write_without_mutation():
    state=initial_vehicle_state();before=deepcopy(state)
    with pytest.raises(ValueError,match='keep_path_write_conflict'):
        validate_vehicle_action_keep('set_window_position',
                                     {'window':'front_passenger','position':state['windows']['positions']['front_passenger']},
                                     state,['windows.positions.front_passenger'])
    assert state==before


def test_public_action_keep_check_covers_containers_wildcards_and_unknown_paths():
    state=initial_vehicle_state();before=deepcopy(state)
    for name,args,keep in [
        ('close_all_windows',{},'windows.positions.front_passenger'),
        ('set_temperature',{'zone':'all','temperature':23},'climate.temperatures.front_passenger'),
        ('set_window_position',{'window':'driver','position':50},'windows'),
        ('sync_climate_zones',{'enabled':state['climate']['sync_zones']},'climate.temperatures.front_passenger'),
    ]:
        with pytest.raises(ValueError,match='keep_path_write_conflict'):
            validate_vehicle_action_keep(name,args,state,[keep])
    with pytest.raises(ValueError,match='unknown_state_path'):
        validate_vehicle_action_keep('set_volume',{'volume':35},state,['windows.missing'])
    validate_vehicle_action_keep('set_window_position',{'window':'driver','position':50},state,
                                 ['windows.positions.front_passenger'])
    validate_vehicle_action_keep('get_seat_state',{'seat':'driver'},state,['seats.positions.driver'])
    assert state==before


@pytest.mark.parametrize('name', ['set_temperature', 'adjust_temperature'])
def test_public_keep_check_rechecks_latest_sync_state_and_rejects_dynamic_noop_write(name):
    state=initial_vehicle_state();held='climate.temperatures.front_passenger'
    arguments={'zone':'driver', 'temperature':state['climate']['temperatures']['driver']} if name=='set_temperature' else {'zone':'driver','delta':0}
    state['climate']['sync_zones']=False
    validate_vehicle_action_keep(name,arguments,state,[held])
    # A late, separate sync receipt changes which zones this action writes.
    state['climate']['sync_zones']=True;before=deepcopy(state)
    with pytest.raises(ValueError,match='keep_path_write_conflict'):
        validate_vehicle_action_keep(name,arguments,state,[held])
    assert state==before


@pytest.mark.parametrize('mutation',['close_all','same_value_write','unknown_path','unbound_path'])
def test_keep_paths_reject_overlapping_or_same_value_writes_and_fake_paths(mutation):
    query,value,state=window_keep_plan()
    if mutation in ['close_all','same_value_write']:
        value['actions']=[action('bad','close','close_all_windows',{}) if mutation=='close_all' else
                          action('bad','close','set_window_position',{'window':'front_passenger','position':50})]
        value['intents'][1]['action_ids']=['bad']
    elif mutation=='unknown_path': value['keep_paths']=value['intents'][0]['keep_paths']=['windows.positions.fake']
    elif mutation=='unbound_path': value['keep_paths']=[]
    with pytest.raises(ValueError):validate_vehicle_plan(value,query,state)


def test_simulation_refusal_on_late_action_rejects_entire_group_and_state_stays_original():
    query='打开空调，并打开尾门。';state=initial_vehicle_state();state['_meta']['speed_kph']=10
    value=plan([intent('both',query,actions=['power','tailgate'])],
              [action('power','both','set_climate_power',{'enabled':True}),
               action('tailgate','both','set_tailgate_open',{'open':True},['power'])])
    before=deepcopy(state)
    with pytest.raises(ValueError,match='simulation_refused'):validate_vehicle_plan(value,query,state,TOOLS)
    assert state==before and not state['climate']['power']


def test_real_completed_receipt_closes_only_its_bound_intent_and_cannot_be_reexecuted():
    query='打开氛围灯，并启用左后门儿童安全锁。';state=initial_vehicle_state()
    receipt=execute_tool('set_ambient_light',{'enabled':True},state)
    operations=[{'operation_id':'actual-command','command_id':'actual-command','name':'set_ambient_light','arguments':{'enabled':True},'result':receipt}]
    value=plan([intent('ambient',query,'打开氛围灯',status='completed',completed=['actual-command']),
                intent('child',query,'并启用左后门儿童安全锁。',actions=['child'])],
               [action('child','child','set_child_lock',{'side':'left','enabled':True})])
    result=validate_vehicle_plan(value,query,state,TOOLS,operations)
    assert result['intents'][0]['status']=='completed' and len(result['actions'])==1
    for mutation in ('missing_receipt','false_receipt','different_args','reexecute'):
        bad=deepcopy(value);proof=deepcopy(operations)
        if mutation=='missing_receipt':proof=[]
        elif mutation=='false_receipt':proof[0]['result']['ok']=False
        elif mutation=='different_args':proof[0]['arguments']['enabled']=False
        else:bad['actions'][0].update(name='set_ambient_light',arguments={'enabled':True})
        with pytest.raises(ValueError):validate_vehicle_plan(bad,query,state,TOOLS,proof)


def test_read_plan_uses_actual_snapshot_value_without_generated_answer():
    query='主驾车窗现在开到多少？';state=initial_vehicle_state();state['windows']['positions']['driver']=37
    value=plan([intent('read',query,actions=['read'])],[action('read','read','get_window_state',{'window':'driver'})])
    result=validate_vehicle_plan(value,query,state,TOOLS)
    assert result['simulation']['actions'][0]['value']['position']==37
    assert result['simulation']['actions'][0]['executed'] is False and state['windows']['positions']['driver']==37


@pytest.mark.parametrize('status',['clarification','refused'])
async def test_explicit_nonplanned_ledger_never_returns_executable_partial_actions(status):
    query='打开车窗。';question='请问哪扇车窗？';reason='当前条件不支持。'
    value=plan([intent('window',query,status=status,question=question if status=='clarification' else '',reason=reason if status=='refused' else '')],
               status=status,question=question if status=='clarification' else '',reason=reason if status=='refused' else '')
    result=await VehicleControlPlanner(FakeArk([response(value)])).plan(query,initial_vehicle_state(),[])
    assert result['status']==status and not result['ok'] and not result['actions']
    bad=deepcopy(value);bad['actions']=[action('window','window','set_window_position',{'window':'driver','position':50})]
    with pytest.raises(ValueError):validate_vehicle_plan(bad,query,initial_vehicle_state())


async def test_invalid_submit_is_preserved_and_retried_without_parameter_coercion():
    query='把所有温区设为23度。';bad=temperature_plan(query);bad['actions'][0]['arguments']['temperature']='23'
    ark=FakeArk([response(bad,identity='bad'),response(temperature_plan(query),identity='good')])
    result=await VehicleControlPlanner(ark).plan(query,initial_vehicle_state(),[])
    assert result['ok'] and result['model_calls']==2
    assert json.loads(result['model_observations'][0]['message']['tool_calls'][0]['function']['arguments'])==wire_plan(bad)
    assert any(e['kind']=='tool.error' and e['call_id']=='bad' for e in result['trace_events'])
    assert result['actions'][0]['arguments']['temperature']==23


def test_wire_schema_removes_offsets_and_repeated_path_enums_but_internal_schema_stays_strict():
    query='打开氛围灯。';state=initial_vehicle_state()
    internal=plan_schema(HOT_TOOL_NAMES,query,state)
    wire=submission_schema(HOT_TOOL_NAMES,query,state)
    source=wire['properties']['intents']['items']
    assert 'source_start' not in source['properties'] and 'source_end' not in source['required']
    assert 'enum' not in wire['properties']['keep_paths']['items']
    assert 'enum' not in source['properties']['keep_paths']['items']
    assert 'windows.positions.front_passenger' in internal['properties']['keep_paths']['items']['enum']
    assert 'source_start' in internal['properties']['intents']['items']['required']
    assert len(json.dumps(internal,ensure_ascii=False))-len(json.dumps(wire,ensure_ascii=False))>20000
    assert wire['properties']['actions']==internal['properties']['actions']


def test_action_variants_preserve_actual_catalog_descriptions_and_exact_argument_schemas():
    state=initial_vehicle_state()
    for build_schema in (plan_schema,submission_schema):
        schema=build_schema(list(TOOLS),'设置温度。',state)
        variants=schema['properties']['actions']['items']['anyOf']
        assert len(variants)==len(TOOLS)
        for variant in variants:
            name=variant['properties']['name']['enum'][0]
            assert variant['description']==TOOLS[name].description
            assert variant['properties']['arguments']==TOOLS[name].parameters
            assert variant['additionalProperties'] is False
        temperature=next(v for v in variants if v['properties']['name']['enum']==['set_temperature'])
        assert '自动打开空调' in temperature['description']


@pytest.mark.parametrize('query,source,error',[
    ('开灯，开灯。','开灯','intent_source_ambiguous'),
    ('哈哈哈','哈哈','intent_source_ambiguous'),
    ('打开氛围灯。','打开车窗','intent_source_not_exact'),
    ('打开氛围灯','打开氛围灯。','intent_source_not_exact'),
])
def test_source_resolution_rejects_all_repeated_including_overlapping_or_changed_original_text(query,source,error):
    value={'intents':[{'id':'i','source_text':source}]};before=deepcopy(value)
    with pytest.raises(ValueError,match=error):resolve_source_spans(value,query)
    assert value==before


def test_source_resolution_adds_only_unique_exact_offsets_and_leaves_arguments_and_constraints_untouched():
    query,value,state=window_keep_plan();wire=wire_plan(value);before=deepcopy(wire)
    resolved=resolve_source_spans(wire,query)
    assert wire==before and resolved==value
    checked=validate_vehicle_plan(resolved,query,state)
    assert checked['keep_paths']==value['keep_paths'] and checked['actions']==value['actions']
    bad=deepcopy(resolved);bad['intents'][0]['source_end']+=1
    with pytest.raises(ValueError,match='intent_source_not_exact'):
        validate_vehicle_plan(bad,query,state)
    with pytest.raises(ValueError,match='wire_source_offsets_not_allowed'):
        resolve_source_spans(value,query)


async def test_wire_invalid_keep_is_not_repaired_and_retry_raw_arguments_are_preserved():
    query='打开氛围灯。'
    good=plan([intent('ambient',query,actions=['a'])],[action('a','ambient','set_ambient_light',{'enabled':True})])
    bad=deepcopy(good);bad['intents'][0]['keep_paths']=['media.volume'];bad['keep_paths']=['media.volume']
    unknown=deepcopy(good);unknown['intents'][0].update(status='keep',action_ids=[],keep_paths=['fake.path']);unknown['actions']=[];unknown['keep_paths']=['fake.path']
    unbound=deepcopy(good);unbound['keep_paths']=['media.volume']
    ark=FakeArk([response(bad,identity='mixed'),response(unknown,identity='unknown'),response(unbound,identity='unbound'),response(good,identity='valid')])
    result=await VehicleControlPlanner(ark).plan(query,initial_vehicle_state(),[])
    assert result['ok'] and result['model_calls']==4
    assert [e['call_id'] for e in result['trace_events'] if e['kind']=='tool.error']==['mixed','unknown','unbound']
    for observation,value in zip(result['model_observations'],[bad,unknown,unbound,good]):
        actual=json.loads(observation['message']['tool_calls'][0]['function']['arguments'])
        assert actual==wire_plan(value)
    assert result['intents'][0]['keep_paths']==[] and result['keep_paths']==[]
    assert any(e['kind']=='vehicle.plan.sources.resolved' and e['call_id']=='valid' for e in result['trace_events'])


async def test_duplicate_load_is_observed_without_changing_available_capabilities_or_executing():
    query='打开后视镜加热。'
    good=plan([intent('mirror',query,actions=['a'])],[action('a','mirror','set_mirror_heating',{'enabled':True})])
    ark=FakeArk([response({'skill_id':'mirrors'},name='load_skill',identity='again'),response(good)])
    state=initial_vehicle_state();before=deepcopy(state)
    result=await VehicleControlPlanner(ark).plan(query,state,['mirrors'])
    assert result['ok'] and result['model_calls']==2 and result['loaded_skills']==['mirrors']
    assert len(result['load_evidence'])==2 and result['load_evidence'][1]['call_id']=='again'
    assert action_names(ark.requests[0])==action_names(ark.requests[1])
    assert state==before


async def test_current_pronoun_action_retains_real_history_for_reference_without_repeating_old_action():
    query='现在打开它。';state=initial_vehicle_state();state['tailgate']['height']=70;before=deepcopy(state)
    history=[{'role':'user','content':'把尾门开启高度设为70。'},{'role':'assistant','content':'尾门开启高度已设为70。'}]
    good=plan([intent('tailgate',query,actions=['open'])],[action('open','tailgate','set_tailgate_open',{'open':True})])
    ark=FakeArk([response(good)])
    result=await VehicleControlPlanner(ark).plan(query,state,['tailgate'],history=history)
    payload=json.loads(ark.requests[0]['messages'][1]['content'])
    assert payload['history_reference_only']==history and payload['request_text']==query
    from backend.prompt_templates import load_prompt
    assert ark.requests[0]['messages'][0]['content'] == load_prompt('vehicle_planner')
    assert '只用来弄清"它""那个"指什么，不能当作本轮的新指令' in ark.requests[0]['messages'][0]['content']
    assert result['ok'] and result['actions']==good['actions'] and state==before


async def test_free_text_cannot_finish_plan_and_four_call_limit_is_explicit():
    ark=FakeArk([{'role':'assistant','content':'都已经执行好了。','_meta':{'usage':None}}]*4)
    state=initial_vehicle_state();before=deepcopy(state)
    result=await VehicleControlPlanner(ark).plan('把空调开到23度。',state,[])
    assert result['status']=='refused' and result['reason']=='planner_model_step_limit'
    assert result['model_calls']==4 and len(result['model_observations'])==4 and state==before


async def test_network_failure_records_attempt_without_fabricated_usage_or_receipt():
    ark=FakeArk([RuntimeError('provider unavailable')]);state=initial_vehicle_state();before=deepcopy(state)
    result=await VehicleControlPlanner(ark).plan('开空调。',state,[])
    assert result['status']=='refused' and result['model_calls']==1 and result['model_observations']==[]
    assert state==before and any(e['kind']=='model.error' for e in result['trace_events'])


async def test_cancellation_leaves_original_state_and_does_not_create_fake_terminal_plan():
    gate=asyncio.Event();ark=FakeArk([gate]);state=initial_vehicle_state();before=deepcopy(state)
    events=[]
    pending=asyncio.create_task(VehicleControlPlanner(ark).plan('开空调。',state,[],trace=lambda kind,data,**fields:events.append((kind,data,fields))))
    while not ark.requests:await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):await pending
    assert state==before
    assert events[-1][0]=='model.cancelled' and events[-1][1]['received_message'] is False


@pytest.mark.parametrize('skills',[['unknown'],['seats','seats'],[{}],'seats'])
async def test_invalid_skill_inputs_fail_before_any_model_call(skills):
    ark=FakeArk([])
    result=await VehicleControlPlanner(ark).plan('开按摩。',initial_vehicle_state(),skills)
    assert result['status']=='refused' and result['model_calls']==0 and ark.requests==[]


def test_latest_actor_snapshot_revalidation_rejects_new_dynamic_safety_condition():
    query='打开尾门。';value=plan([intent('tailgate',query,actions=['tailgate'])],
                               [action('tailgate','tailgate','set_tailgate_open',{'open':True})])
    value['loaded_skills']=['tailgate'];stopped=initial_vehicle_state()
    checked=validate_vehicle_plan(value,query,stopped)
    now=deepcopy(stopped);now['_meta']['speed_kph']=20
    with pytest.raises(ValueError,match='simulation_refused'):validate_vehicle_plan(checked,query,now)
    assert now['tailgate']['open'] is False


def test_completed_status_cannot_be_fabricated_from_summary_or_nonmatching_receipt():
    query='打开座椅按摩。';state=initial_vehicle_state()
    value=plan([intent('massage',query,status='completed',completed=['receipt'])])
    fake={'operation_id':'receipt','command_id':'receipt','name':'set_seat_heating',
          'arguments':{'seat':'driver','level':0},'result':{'ok':True,'name':'set_seat_heating',
          'arguments':{'seat':'driver','level':0},'state':deepcopy(state)}}
    fake['name']='set_seat_massage';fake['arguments']={'seat':'driver','mode':'relax'}
    with pytest.raises(ValueError,match='completed_intent_has_no_real_receipt'):
        validate_vehicle_plan(value,query,state,TOOLS,[fake])
