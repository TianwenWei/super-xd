"""Independent evaluation-oracle tests. All network/tool records are fixtures."""
from copy import deepcopy
import json
import pytest

from scripts.evaluate_interactions import (
    ADDITIONAL_SCENARIOS, BASELINE, BOUNDED_V6_SCENARIO_NAMES, HELDOUT_SCENARIOS, PARIS_FORECAST_CONTRACT, SCENARIOS, action, changed_paths, claim_supported, evaluate_record,
    forecast_date_evidence, reassess_report, report_summary, selected_scenarios, state_question, task_operation, task_web_evidence, temperature, volume,
)


def initial():
    return {'climate':{'power':False,'temperatures':{'driver':22,'front_passenger':22,'rear':22},'fan_speed':3},
            'windows':{'positions':{'driver':0,'front_passenger':0,'rear_left':0,'rear_right':0}},
            'media':{'volume':30},'seats':{'positions':{'driver':{'massage':'off','heating':0}}}}


def evidence(spec):
    before=initial();after=deepcopy(before)
    for path,value in spec['target_state'].items():
        cursor=after;parts=path.split('.')
        for key in parts[:-1]:cursor=cursor.setdefault(key,{})
        cursor[parts[-1]]=value
    calls=deepcopy([*spec['actions'],*[action('load_skill',skill_id=s) for s in spec['loads']]])
    # Put capability loads before their actions, like actual model turns.
    calls.sort(key=lambda c:c['name']!='load_skill')
    row={'turn_id':'turn','text':'主驾空调调到23度了。','errors':[],
         'before':{'vehicle':before,'tasks':[]},'after':{'vehicle':after,'tasks':[]},
         'commands':[],'receipts':[],'traces':[],'metrics':{}}
    for i,call in enumerate(calls):
        row['traces'].append({'trace_id':i+1,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker',**call}})
        if call['name']!='load_skill':
            command={'command_id':f'cmd-{i}','turn_id':'turn',**call};row['commands'].append(command)
            row['receipts'].append({'command_id':f'cmd-{i}','turn_id':'turn','result':{'ok':True,**call,'state':deepcopy(after)}})
    return row


def test_plan_covers_at_least_40_turns_and_12_independent_groups_without_baseline():
    cases=selected_scenarios(None)
    assert len(cases)==17 and sum(len(s['steps']) for s in cases)==61
    assert 'baseline_regressions' not in {s['id'] for s in cases}
    assert len(BASELINE['steps'])==5
    assert selected_scenarios(['baseline_regressions'])[0]['baseline_only']
    for scenario in cases:
        for spec in scenario['steps']:
            assert all(k in spec for k in ['actions','loads','target_state','allowed_changed_paths','kind'])


def test_actual_correct_tool_receipt_and_state_are_required_together():
    spec=temperature('test','只调主驾23度',23,'driver');row=evidence(spec)
    assert evaluate_record(spec,row,{})['pass']
    for mutation in ['receipt','arguments','target','extra_state']:
        bad=evidence(spec)
        if mutation=='receipt':bad['receipts']=[]
        elif mutation=='arguments':bad['commands'][0]['arguments']['zone']='all'
        elif mutation=='target':bad['after']['vehicle']['climate']['temperatures']['driver']=24
        else:bad['after']['vehicle']['climate']['fan_speed']=7
        assert not evaluate_record(spec,bad,{})['pass'],mutation


def test_successful_but_unrequested_heating_cannot_substitute_for_massage():
    spec=deepcopy(SCENARIOS[1]['steps'][0]);row=evidence(spec)
    row['commands'][0]['name']='set_seat_heating';row['commands'][0]['arguments']={'seat':'driver','level':0}
    row['text']='主驾座椅加热关了。'
    assert not evaluate_record(spec,row,{})['pass']


def test_state_question_fails_unrelated_task_progress_even_if_tool_result_ok():
    spec=state_question('state','现在主驾车窗多少',[{'kind':'window','seat':'driver','value':33}])
    row=evidence(spec);row['text']='请告诉我所在城市。'
    row['traces']=[{'trace_id':1,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker','name':'manage_task','arguments':{'action':'get','task_id':'airport'}}}]
    result=evaluate_record(spec,row,{})
    assert not result['pass']
    assert any(isinstance(e,dict) and 'wrong_tool_set'in e for e in result['assertion_failures'])


def test_entity_and_predicate_are_bound_and_negation_or_questions_do_not_pass():
    claim={'kind':'window','seat':'driver','value':33}
    assert claim_supported('左前车窗开到百分之三十三。',claim)
    assert not claim_supported('左前车窗关着，副驾开到33%。',claim)
    assert not claim_supported('主驾不是开到33%，目前无法确认。',claim)
    assert not claim_supported('主驾是否开到33%？',claim)
    t={'kind':'temperature','seat':'driver','value':23}
    assert claim_supported('二十三度。',t)
    assert not claim_supported('主驾21度，副驾空调23度。',t)


def test_extra_tools_are_failures_even_when_no_extra_state_change_occurs():
    spec=volume('volume','音量35',35);row=evidence(spec)
    row['traces'].append({'trace_id':9,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker','name':'set_seat_heating','arguments':{'seat':'driver','level':0}}})
    assert not evaluate_record(spec,row,{})['pass']


def test_schema_refusal_and_model_refusal_both_need_unchanged_state_and_real_bounds():
    spec=deepcopy(SCENARIOS[7]['steps'][0]);row=evidence(spec);row['text']='音量只能在零到一百之间，999无法设置。'
    assert evaluate_record(spec,row,{})['pass']
    rejected=evidence(spec);rejected['text']=row['text'];rejected['traces']=[{'trace_id':1,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker',**spec['rejected_call']}}]
    assert evaluate_record(spec,rejected,{})['pass']
    clipped=evidence(spec);clipped['text']='已设为100。';clipped['after']['vehicle']['media']['volume']=100
    assert not evaluate_record(spec,clipped,{})['pass']


def test_upper_limit_refusal_does_not_have_to_recite_irrelevant_lower_bound():
    spec=deepcopy(SCENARIOS[7]['steps'][0]);row=evidence(spec)
    row['text']='音量最高只能设到100，要按100来设置吗？'
    assert evaluate_record(spec,row,{})['pass']
    for text in ['音量最高只能设到100，已经调到100了。','音量最大只能到200。']:
        bad=evidence(spec);bad['text']=text
        assert not evaluate_record(spec,bad,{})['pass']


def test_all_closed_synonyms_preserve_negative_counterexamples():
    claim={'kind':'windows_all','value':0}
    assert claim_supported('四扇车窗都处于全关状态。',claim)
    assert not claim_supported('四扇车窗没有全关。',claim)
    assert not claim_supported('四扇车窗不是全关状态。',claim)


def test_planned_state_claim_must_match_actual_snapshot_even_if_model_repeats_gold_value():
    spec=state_question('state','主驾车窗多少',[{'kind':'window','seat':'driver','value':33}])
    row=evidence(spec);row['text']='主驾车窗开到33%。'
    assert not evaluate_record(spec,row,{})['pass']
    row=evidence(spec);row['text']='主驾车窗开到33%。'
    row['before']['vehicle']['windows']['positions']['driver']=33
    row['after']['vehicle']['windows']['positions']['driver']=33
    assert evaluate_record(spec,row,{})['pass']


def test_checkpoint_is_never_complete_before_finished_at_and_full_plan():
    report={'planned_user_turns':61,'planned_group_runs':17,
            'groups':[{'spec':{'steps':[{}]},'records':[{'pass':True}]}]}
    assert not report_summary(report)['all_planned_turns_passed']
    complete={'planned_user_turns':1,'planned_group_runs':1,**{'groups':report['groups']}}
    assert not report_summary(complete)['all_planned_turns_passed']
    complete['finished_at']='2026-10-13T00:00:00Z'
    assert report_summary(complete)['all_planned_turns_passed']


def test_all_ten_vehicle_subsystems_have_exact_requested_action_gold():
    from backend.catalog import TOOLS
    skills={TOOLS[a['name']].skill_id for s in SCENARIOS for spec in s['steps'] for a in spec['actions']}
    assert skills=={'climate','seats','windows','mirrors','lights','locks','tailgate','wipers','media','navigation'}


def test_capability_persistence_changes_load_requirement_not_requested_action_or_state():
    spec=deepcopy(SCENARIOS[1]['steps'][1]);row=evidence(spec)
    row['traces']=[e for e in row['traces'] if e['data']['name']!='load_skill']
    prior={'trace_id':0,'turn_id':'prior','kind':'tool.completed','data':{'agent':'talker','name':'load_skill','arguments':json.dumps({'skill_id':'seats'}),'result':{'ok':True}}}
    row['traces'].insert(0,prior)
    assert evaluate_record(spec,row,{})['pass']
    bad=evidence(spec);bad['traces']=[e for e in bad['traces'] if e['data']['name']!='load_skill']
    assert not evaluate_record(spec,bad,{})['pass']


def test_unknown_scenario_and_incomplete_execution_cannot_be_success():
    try:selected_scenarios(['invented'])
    except ValueError:pass
    else:raise AssertionError('unknown scenario accepted')
    report={'groups':[{'spec':{'steps':[{},{}]},'records':[{'pass':True}]}]}
    summary=report_summary(report)
    assert summary['planned_user_turns']==2 and summary['attempted_user_turns']==1
    assert not summary['all_planned_turns_passed']


def test_boolean_and_number_state_changes_are_distinct():
    assert changed_paths({'value':False},{'value':0})==['value']


def batch_task_fixture(arguments, *, result_ids=('science', 'natural'), completed=True, ok=True):
    spec=task_operation('both','两个研究进度',[
        {'action':'get','task_id':'$science'},{'action':'get','task_id':'$natural'}])
    row=evidence(spec);row['text']='科学研究和自然研究都还在进行。'
    tasks=[{'id':name,'status':'running','revision':1} for name in ('science','natural','other')]
    row['before']['tasks']=deepcopy(tasks);row['after']['tasks']=deepcopy(tasks)
    row['traces']=[{'trace_id':1,'turn_id':'turn','kind':'tool.started','call_id':'batch',
                    'data':{'agent':'talker','name':'manage_task','arguments':arguments}}]
    if completed:
        row['traces'].append({'trace_id':2,'turn_id':'turn','kind':'tool.completed' if ok else 'tool.error','call_id':'batch',
                              'data':{'agent':'talker','name':'manage_task','result':{'ok':ok,'tasks':[{'id':name} for name in result_ids]}}})
    return spec,row,{'science':'science','natural':'natural'}


def test_batch_get_is_semantically_two_reads_without_rewriting_raw_calls_or_gold():
    args={'action':'get','task_ids':['natural','science'],'read_details':False}
    spec,row,aliases=batch_task_fixture(args)
    result=evaluate_record(spec,row,aliases)
    assert result['pass']
    assert result['observed_tool_calls'][0]['arguments']==args
    assert len(result['observed_tool_calls'])==1 and len(result['observed_task_operations'])==2
    assert spec['task_operations']==[{'action':'get','task_id':'$science'},{'action':'get','task_id':'$natural'}]


@pytest.mark.parametrize('arguments',[
    {'action':'get','task_ids':['science']},
    {'action':'get','task_ids':['science','science']},
    {'action':'get','task_ids':['science','natural','other']},
    {'action':'get','task_ids':[]},
    {'action':'get','task_ids':'science'},
    {'action':'get','task_ids':['science','natural'],'task_id':'science'},
    {'action':'reply','task_ids':['science','natural'],'text':'巴黎'},
    {'action':'cancel','task_ids':['science','natural']},
])
def test_batch_read_does_not_hide_missing_extra_duplicate_or_nonread_operations(arguments):
    spec,row,aliases=batch_task_fixture(arguments)
    assert not evaluate_record(spec,row,aliases)['pass']


@pytest.mark.parametrize('kwargs',[
    {'result_ids':('science',)},
    {'result_ids':('science','science')},
    {'result_ids':('science','natural','other')},
    {'completed':False},
    {'ok':False},
])
def test_batch_get_needs_actual_complete_success_for_each_requested_task(kwargs):
    spec,row,aliases=batch_task_fixture({'action':'get','task_ids':['science','natural']},**kwargs)
    assert not evaluate_record(spec,row,aliases)['pass']


def task_read_fixture(kind='temperature_ranges'):
    if kind=='temperature_ranges':
        spec=task_operation('read','已查到的巴黎明天温度是多少', [{'action':'get','task_id':'$task'}],
                            result_consistency='task',task_read_contract={'kind':kind,'place':'巴黎','relative_date':'明天'})
        original='巴黎明天2026年10月14日的温度，A.org为9度至17度，B.cn为11度至21度，两个来源的范围存在差异。'
        task={'id':'task','status':'completed','revision':1,'result':{'spoken_summary':original,'summary':original}}
        response='巴黎明天温度，A.org显示9到17度，B.cn显示11到21度，两个来源范围略有不同。'
    else:
        spec=task_operation('read','后台任务还缺什么信息', [{'action':'get','task_id':'$task'}],
                            question_consistency='task',task_read_contract={'kind':'missing_slot','slot':'city'})
        task={'id':'task','status':'waiting_clarification','revision':1,'question':'请问你想查哪个城市明天的温度？'}
        response='还缺少具体城市名称，请告诉我想查询哪个城市的温度。'
    row=evidence(spec);row['text']=response
    row['before']['tasks']=[deepcopy(task)];row['after']['tasks']=[deepcopy(task)]
    return spec,row,{'task':'task'}


def test_direct_snapshot_task_read_accepts_bound_actual_facts_not_literal_full_sentence():
    for kind in ('temperature_ranges','missing_slot'):
        spec,row,aliases=task_read_fixture(kind)
        result=evaluate_record(spec,row,aliases)
        assert result['pass'],result['assertion_failures']
        assert result['task_snapshot_read_without_tool']
        assert result['observed_tool_calls']==[]


@pytest.mark.parametrize('response',[
    '巴黎明天温度，A.org显示11到21度，B.cn显示9到17度，两个来源范围略有不同。',
    '巴黎明天温度，A.org显示19到17度，B.cn显示11到21度，两个来源范围略有不同。',
    '巴黎明天温度，A.org显示9到17度，两个来源范围略有不同。',
    '巴黎明天2026年10月15日温度，A.org显示9到17度，B.cn显示11到21度，两个来源范围略有不同。',
    '巴黎今天温度，A.org显示9到17度，B.cn显示11到21度，两个来源范围略有不同。',
    '巴黎明天温度，A.org显示9到17度，B.cn显示11到21度，两个来源范围略有不同。另有30度高温。',
])
def test_direct_task_read_rejects_wrong_source_range_missing_fact_date_or_extra_value(response):
    spec,row,aliases=task_read_fixture();row['text']=response
    assert not evaluate_record(spec,row,aliases)['pass']


def test_direct_task_read_preserves_full_report_restrictions_even_if_stored_speech_omits_them():
    spec,row,aliases=task_read_fixture()
    row['before']['tasks'][0]['result']['summary']+='以上来自历史平均气温，尚未核实明天预报。'
    assert not evaluate_record(spec,deepcopy(row),aliases)['pass']
    row['text']+='这些是历史平均气温，尚未核实明天预报。'
    assert evaluate_record(spec,deepcopy(row),aliases)['pass']
    row['text']=row['text'].replace('这些是历史平均气温','这些并非历史平均气温')
    assert not evaluate_record(spec,row,aliases)['pass']


@pytest.mark.parametrize('response',['不缺少城市，城市已经确定了。','已经查到巴黎明天的温度。','请告诉我你想查哪一天。'])
def test_direct_clarification_read_cannot_invent_resolution_or_change_missing_slot(response):
    spec,row,aliases=task_read_fixture('missing_slot');row['text']=response
    assert not evaluate_record(spec,row,aliases)['pass']


def test_direct_task_read_does_not_allow_new_delegation_or_vehicle_action():
    spec,row,aliases=task_read_fixture()
    row['traces']=[{'trace_id':1,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker',
                    'name':'delegate_task','arguments':{'goal':'再查巴黎天气'}}}]
    assert not evaluate_record(spec,row,aliases)['pass']


def forecast_fixture():
    forecast={'ok':True,'date':'2026-10-14','data_type':'forecast','timezone':'Europe/Paris','unit':'°C',
              'location':{'name':'Paris','country_code':'FR','timezone':'Europe/Paris'},
              'temperature_min':12.3,'temperature_max':20.1,'source_url':'https://weather.example/forecast',
              'queried_at':'2026-10-13T05:00:00Z',
              'raw':{'forecast':{'timezone':'Europe/Paris',
                                 'daily':{'time':['2026-10-14'],'temperature_2m_min':[12.3],'temperature_2m_max':[20.1]},
                                 'daily_units':{'temperature_2m_min':'°C','temperature_2m_max':'°C'}}}}
    keys=('date','location','timezone','temperature_min','temperature_max','unit','source_url','queried_at')
    task={'id':'weather','context':{'captured_at':'2026-10-13T05:00:00Z'},'result':{
        'summary':'巴黎2026年10月14日的温度预计12.3度至20.1度。',
        'spoken_summary':'巴黎明天预计最低12.3度，最高20.1度。',
        'forecast_records':[{key:deepcopy(forecast[key]) for key in keys}], 'web_evidence':'forecast',
        'sources':[{'url':forecast['source_url'],'kind':'forecast','verified':True,'extracted':False}]}}
    events=[{'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':'get_weather_forecast','result':forecast}},
            {'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':'finish_task','result':{'ok':True}}}]
    condition={'require_web':True,'evidence_contract':deepcopy(PARIS_FORECAST_CONTRACT)}
    return task,events,condition


def test_weather_contract_accepts_real_dated_forecast_without_forcing_search_extract():
    task,events,condition=forecast_fixture()
    result=task_web_evidence(task,events,condition)
    assert result['pass'],result['assertion_failures']
    assert result['expected_forecast_date']=='2026-10-14'
    # Explicitly requested official-page research still requires page extraction.
    assert not task_web_evidence(task,events,{'require_web':True})['pass']


@pytest.mark.parametrize('mutation',[
    'date','location','timezone','unit','raw_date','raw_bounds','raw_unit','retained','source_extracted',
    'report_bounds','report_date','report_historical','wrong_task','no_finish','no_actual_tool',
])
def test_forecast_cannot_be_fabricated_misdated_relabelled_or_disagree_with_report(mutation):
    task,events,condition=forecast_fixture();forecast=events[0]['data']['result']
    if mutation=='date':forecast['date']='2026-10-15'
    elif mutation=='location':forecast['location']['country_code']='US'
    elif mutation=='timezone':forecast['timezone']='UTC'
    elif mutation=='unit':forecast['unit']='°F'
    elif mutation=='raw_date':forecast['raw']['forecast']['daily']['time']=['2026-10-15']
    elif mutation=='raw_bounds':forecast['raw']['forecast']['daily']['temperature_2m_min']=[22.3]
    elif mutation=='raw_unit':forecast['raw']['forecast']['daily_units']['temperature_2m_max']='°F'
    elif mutation=='retained':task['result']['forecast_records'][0]['temperature_max']=30
    elif mutation=='source_extracted':task['result']['sources'][0]['extracted']=True
    elif mutation=='report_bounds':task['result']['spoken_summary']='巴黎明天12度至20度。'
    elif mutation=='report_date':task['result']['summary']='巴黎2026年10月15日预计12.3度至20.1度。'
    elif mutation=='report_historical':task['result']['summary']+='这是历史平均气温。'
    elif mutation=='wrong_task':events[0]['task_id']='other'
    elif mutation=='no_finish':events.pop()
    elif mutation=='no_actual_tool':events.pop(0)
    assert not task_web_evidence(task,events,condition)['pass'],mutation


def test_normal_delegate_cannot_enable_detailed_reading_without_user_request():
    spec=deepcopy(SCENARIOS[9]['steps'][0]);row=evidence(spec)
    args={'goal':spec['query'],'query_mode':'research','read_details':True}
    row['traces']=[{'trace_id':1,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker','name':'delegate_task','arguments':args}}]
    row['after']['tasks']=[{'id':'registered','goal':args['goal']}]
    assert not evaluate_record(spec,deepcopy(row),{})['pass']
    spec['read_details']=True
    assert evaluate_record(spec,row,{})['pass']


def test_forecast_date_contract_distinguishes_query_and_page_update_dates():
    text='根据2026年10月13日查询的预报，巴黎2026年10月14日最低11.2度、最高19.3度。查询时间为2026年10月13日05:30。'
    roles=forecast_date_evidence(text,'2026-10-14')
    assert roles['pass'] and roles['forecast_dates']==[(2026,10,14)]
    assert roles['acquisition_or_update_dates']==[(2026,10,13),(2026,10,13)]
    assert forecast_date_evidence('巴黎2026年10月14日温度11至22度，页面标注更新于巴黎时间2026-10-11。','2026-10-14')['pass']
    # A correct query timestamp cannot excuse a wrong forecast date.
    assert not forecast_date_evidence(text.replace('巴黎2026年10月14日','巴黎2026年10月15日'),'2026-10-14')['pass']
    assert not forecast_date_evidence('查询时间为2026年10月13日，巴黎温度11到19度。','2026-10-14')['pass']


def test_heldout_plan_is_separate_and_preserves_exclusion_clarification_and_relative_targets():
    assert len(HELDOUT_SCENARIOS)==4 and sum(len(group['steps']) for group in HELDOUT_SCENARIOS)==8
    assert not {group['id'] for group in HELDOUT_SCENARIOS}&{group['id'] for group in selected_scenarios(None)}
    windows=HELDOUT_SCENARIOS[0]['steps'][1]
    assert {call['arguments']['window'] for call in windows['actions']}=={'driver','rear_left','rear_right'}
    assert 'close_all_windows' not in {call['name'] for call in windows['actions']}
    assert windows['target_state']['windows.positions.front_passenger']==50
    bare=HELDOUT_SCENARIOS[1]['steps'][0]
    assert bare['query']=='巴黎' and bare['task_operations'][0]['action']=='reply'
    attributes=HELDOUT_SCENARIOS[3]['steps'][1]
    assert attributes['target_state']['lights.ambient'] is True
    assert {call['name'] for call in attributes['actions']}=={'set_ambient_color','set_ambient_brightness'}


@pytest.mark.parametrize('relative',[False,True])
def test_heldout_relative_rear_accepts_only_declared_equivalent_tools_and_real_target(relative):
    spec=deepcopy(HELDOUT_SCENARIOS[2]['steps'][1])
    fixture_spec={**spec,**spec['equivalent_action_sets'][0]} if relative else spec
    row=evidence(fixture_spec)
    row['before']['vehicle']['climate']['temperatures']={'driver':23,'front_passenger':23,'rear':23}
    row['before']['vehicle']['climate']['power']=True
    assert evaluate_record(spec,deepcopy(row),{})['pass']
    row['commands'][0]['arguments']['zone']='all'
    assert not evaluate_record(spec,row,{})['pass']


def test_heldout_relative_action_requires_actual_initial_rear_value_not_coincidental_final_value():
    spec=deepcopy(HELDOUT_SCENARIOS[2]['steps'][1]);row=evidence(spec)
    # Fake evidence with the desired final state cannot excuse a different start.
    assert not evaluate_record(spec,row,{})['pass']


def test_offline_date_reassessment_keeps_original_decision_and_every_raw_event_unchanged():
    task,events,condition=forecast_fixture();task.update(status='completed',goal=SCENARIOS[11]['steps'][0]['query'])
    task['result']['summary']='根据2026年10月13日查询的预报，巴黎2026年10月14日最低12.3度，最高20.1度。'
    spec=deepcopy(SCENARIOS[11]['steps'][0]);row=evidence(spec)
    args={'goal':task['goal'],'query_mode':'quick','read_details':False}
    row['traces']=[{'trace_id':1,'turn_id':'turn','kind':'tool.started','data':{'agent':'talker','name':'delegate_task','arguments':args}}]
    row['after']['tasks']=[task]
    row['step_id']=spec['id'];row['query']=spec['query'];row['expectation']=spec
    row['pass']=False;row['assertion_failures']=[{'wait_after_failed':['forecast_report_changed_date']}]
    row['wait_after']={'task':task,'pass':False,'assertion_failures':['forecast_report_changed_date']}
    source={'oracle_version':'interaction-oracle-v3','finished_at':'2026-10-13T05:00:00Z','run_status':'completed',
            'planned_user_turns':1,'planned_group_runs':1,'groups':[{'scenario_id':'simple_and_complex_web',
            'spec':{'steps':[spec]},'aliases':{'weather':'weather'},'records':[row],
            'complete_traces':deepcopy(events),'complete_ws_events':[]}]}
    untouched=deepcopy(source);updated=reassess_report(source)
    assert source==untouched
    revised=updated['groups'][0]['records'][0]
    assert revised['pass'],revised['assertion_failures']
    assert revised['original_oracle_decision']['pass'] is False
    assert revised['wait_after']['original_oracle_decision']['pass'] is False
    assert updated['groups'][0]['complete_traces']==source['groups'][0]['complete_traces']
    assert updated['reassessment_only_no_requests']


def extracted_weather_fixture():
    task,events,condition=forecast_fixture()
    url='https://www.weather.example/paris/forecast'
    body='2 Week Extended Forecast in Paris, France\n| Day | Temperature | Weather |\n| Wed Oct 14 | 71 / 52°F | Sunny |\n'
    task['result'].update(summary='巴黎2026年10月14日气温52至71°F，约合11到22摄氏度。页面标注更新于巴黎时间2026-10-11。',
                          spoken_summary='巴黎明天大约11到22摄氏度，预报可能更新。',
                          sources=[{'url':url,'kind':'extract','extracted':True}],
                          forecast_records=[],web_evidence='extracted')
    events.extend([
        {'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':'search_web','result':{'ok':True,'results':[{'url':url}]}}},
        {'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':'extract_web','result':{'ok':True,'queried_at':'2026-10-13T05:00:00Z','results':[{'url':url,'raw_content':body}]}}},
    ])
    return task,events,condition


def test_adopted_real_web_forecast_can_replace_unused_successful_api_observation():
    task,events,condition=extracted_weather_fixture()
    result=task_web_evidence(task,events,condition)
    assert result['pass'],result['assertion_failures']
    record=result['actual_extracted_forecast_records'][0]
    assert record['date']=='2026-10-14' and record['native_unit']=='°F'
    assert record['native_min']==52 and record['native_max']==71
    assert abs(record['temperature_min']-11.111111)<.00001
    assert result['unused_successful_forecast_urls']==['https://weather.example/forecast']


@pytest.mark.parametrize('mutation',[
    'historical','wrong_day','wrong_year','wrong_place','unknown_unit','missing_extract',
    'unadopted_url','search_only','wrong_report_bounds','wrong_report_date','missing_approximation',
])
def test_extracted_forecast_requires_actual_future_date_location_units_and_consistent_report(mutation):
    task,events,condition=extracted_weather_fixture();page=events[-1]['data']['result']['results'][0]
    if mutation=='historical':page['raw_content']=page['raw_content'].replace('2 Week Extended Forecast in Paris, France','Forecast\nClimate Averages in Paris, France')
    elif mutation=='wrong_day':page['raw_content']=page['raw_content'].replace('Oct 14','Oct 15')
    elif mutation=='wrong_year':page['raw_content']=page['raw_content'].replace('Oct 14','Oct 14 2025')
    elif mutation=='wrong_place':page['raw_content']=page['raw_content'].replace('Paris','Tokyo')
    elif mutation=='unknown_unit':page['raw_content']=page['raw_content'].replace('°F','°X')
    elif mutation=='missing_extract':events.pop()
    elif mutation=='unadopted_url':page['url']='https://unadopted.example/forecast'
    elif mutation=='search_only':task['result']['sources'][0]['extracted']=False
    elif mutation=='wrong_report_bounds':task['result']['spoken_summary']='巴黎明天大约13到22度。'
    elif mutation=='wrong_report_date':task['result']['summary']=task['result']['summary'].replace('10月14日','10月15日')
    elif mutation=='missing_approximation':task['result']['spoken_summary']='巴黎明天11到22度。'
    assert not task_web_evidence(task,events,condition)['pass'],mutation


def test_declared_extracted_source_metadata_without_actual_page_ledger_is_not_official_evidence():
    task,events,condition=forecast_fixture()
    task['result']['web_evidence']='extracted'
    task['result']['sources']=[{'url':'https://www.cite-sciences.fr/fr/infos-pratiques/horaires','extracted':True}]
    events.extend([{'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':name,'result':{'ok':True}}} for name in ('search_web','extract_web')])
    assert not task_web_evidence(task,events,{'required_source_domains':['cite-sciences.fr']})['pass']


def ambient_guard_fixture():
    spec=deepcopy(next(group for group in SCENARIOS if group['id']=='lights')['steps'][1])
    row=evidence(spec);row['query']=spec['query'];row['text']='氛围灯颜色蓝色、亮度60，电源保持原状，没有调整。'
    row['before']['vehicle']['lights']={'ambient':False,'ambient_color':'warm','ambient_brightness':40}
    phrase='先不要打开氛围灯';start=spec['query'].index(phrase)
    guard={'kind':'literal_keep_state','executed':False,'needs_clarification':False,
           'state_path':'lights.ambient','current_value_known':True,'current_value':False,'requested_value':False,
           'evidence':[{'start':start,'end':start+len(phrase),'text':phrase,'meaning':'keep_current_power'}]}
    row['traces'].extend([
        {'trace_id':900,'turn_id':'turn','call_id':'blocked','kind':'tool.started',
         'data':{'agent':'talker','name':'set_ambient_light','arguments':{'enabled':False}}},
        {'trace_id':901,'turn_id':'turn','call_id':'blocked','kind':'tool.error',
         'data':{'agent':'talker','name':'set_ambient_light','result':{'ok':False,'executed':False,
                 'authorization_guard':guard,'needs_clarification':False}}},
    ])
    return spec,row


def test_real_literal_keep_guard_is_audited_without_relaxing_positive_commands_or_raw_attempts():
    spec,row=ambient_guard_fixture();result=evaluate_record(spec,row,{})
    assert result['pass'],result['assertion_failures']
    assert len(result['guarded_model_attempts'])==1
    assert result['raw_model_attempts']==result['observed_tool_calls']
    assert 'set_ambient_light' in [call['name'] for call in result['raw_model_attempts']]
    assert 'set_ambient_light' not in [call['name'] for call in result['effective_tool_calls']]
    assert len(result['commands'])==len(result['receipts'])==2


@pytest.mark.parametrize('mutation',[
    'no_trace','wrong_call','wrong_turn','wrong_tool','completed_not_error','dispatched','command',
    'changed_state','fake_span','fake_text','fake_current','unknown_current','fake_requested',
    'conflict','executed','missing_positive_command',
])
def test_guard_exemption_rejects_forgery_execution_conflict_or_missing_requested_action(mutation):
    spec,row=ambient_guard_fixture();failure=row['traces'][-1];guard=failure['data']['result']['authorization_guard']
    if mutation=='no_trace':row['traces'].pop()
    elif mutation=='wrong_call':failure['call_id']='other'
    elif mutation=='wrong_turn':failure['turn_id']='old'
    elif mutation=='wrong_tool':failure['data']['name']='set_volume'
    elif mutation=='completed_not_error':failure['kind']='tool.completed'
    elif mutation=='dispatched':row['traces'].append({'trace_id':902,'turn_id':'turn','call_id':'blocked','kind':'vehicle.dispatched'})
    elif mutation=='command':row['commands'].append({'name':'set_ambient_light','arguments':{'enabled':False}})
    elif mutation=='changed_state':row['after']['vehicle']['lights']['ambient']=True
    elif mutation=='fake_span':guard['evidence'][0]['start']+=1
    elif mutation=='fake_text':guard['evidence'][0]['text']='保持当前状态'
    elif mutation=='fake_current':guard['current_value']=True
    elif mutation=='unknown_current':guard['current_value_known']=False
    elif mutation=='fake_requested':guard['requested_value']=True
    elif mutation=='conflict':guard['kind']='literal_authorization_conflict'
    elif mutation=='executed':failure['data']['result']['executed']=True
    elif mutation=='missing_positive_command':row['commands'].pop()
    assert not evaluate_record(spec,row,{})['pass'],mutation


def no_task_recovery_fixture():
    spec=volume('volume','音量调到35',35);row=evidence(spec)
    row['traces'].extend([
        {'trace_id':900,'turn_id':'turn','call_id':'retry','kind':'tool.started',
         'data':{'agent':'talker','name':'manage_task','arguments':{'action':'reply','text':spec['query']}}},
        {'trace_id':901,'turn_id':'turn','call_id':'retry','kind':'tool.error',
         'data':{'agent':'talker','name':'manage_task','result':{'ok':False,'code':'no_task','retry_model':True,'executed':False}}},
    ])
    return spec,row


def test_exact_no_task_failure_can_recover_to_real_requested_control_and_stays_visible():
    spec,row=no_task_recovery_fixture();result=evaluate_record(spec,row,{})
    assert result['pass'],result['assertion_failures']
    assert len(result['recovered_tool_attempts'])==1
    assert result['recovered_tool_attempts'][0]['call']['name']=='manage_task'
    assert len(result['raw_model_attempts'])==2 and len(result['effective_tool_calls'])==1


@pytest.mark.parametrize('mutation',[
    'existing_task','new_task','wrong_code','not_retry','executed','no_trace','wrong_call',
    'wrong_agent','wrong_tool','missing_requested_receipt',
])
def test_no_task_recovery_is_not_a_general_tool_error_whitelist(mutation):
    spec,row=no_task_recovery_fixture();failure=row['traces'][-1]
    if mutation=='existing_task':row['before']['tasks']=[{'id':'existing'}]
    elif mutation=='new_task':row['after']['tasks']=[{'id':'new'}]
    elif mutation=='wrong_code':failure['data']['result']['code']='unknown_task'
    elif mutation=='not_retry':failure['data']['result']['retry_model']=False
    elif mutation=='executed':failure['data']['result']['executed']=True
    elif mutation=='no_trace':row['traces'].pop()
    elif mutation=='wrong_call':failure['call_id']='other'
    elif mutation=='wrong_agent':failure['data']['agent']='thinker'
    elif mutation=='wrong_tool':failure['data']['name']='set_volume'
    elif mutation=='missing_requested_receipt':row['receipts']=[]
    assert not evaluate_record(spec,row,{})['pass'],mutation


def typed_retry_fixture(kind):
    if kind=='literal_target_mismatch':
        spec=deepcopy(SCENARIOS[2]['steps'][1]);row=evidence(spec)
        call=action('set_steering_heating',enabled=False);phrase='后视镜加热';start=spec['query'].index(phrase)
        guard={'selected_feature':'steering_heating','evidence':[{'start':start,'end':start+len(phrase),
                'text':phrase,'meaning':'requested_feature','feature':'mirror_heating'}]}
    elif kind=='literal_window_hold':
        spec=deepcopy(HELDOUT_SCENARIOS[0]['steps'][1]);row=evidence(spec)
        row['before']['vehicle']['windows']['positions']['front_passenger']=50
        call=action('close_all_windows');phrase='副驾车窗别动';start=0
        guard={'preserved_windows':['front_passenger'],'affected_windows':['front_passenger'],
               'current_positions':{'front_passenger':50},'unknown_windows':[],
               'evidence':[{'start':start,'end':len(phrase),'text':phrase,
                            'meaning':'keep_current_window_position','window':'front_passenger'}]}
    else:
        spec=deepcopy(SCENARIOS[8]['steps'][0]);row=evidence(spec);row['text']='你现在在哪个城市或地址？'
        call=action('delegate_task',goal='查询最近的机场',read_details=False)
        guard={'required_condition':'出发城市或地址','evidence':[{'start':0,'end':len(spec['query']),
               'text':spec['query'],'meaning':'nearest_airport_requires_origin'}]}
    row['query']=spec['query']
    guard.update(kind=kind,executed=False,retry_model=True,needs_clarification=False)
    row['traces'].extend([
        {'trace_id':900,'turn_id':'turn','call_id':'refused','kind':'tool.started',
         'data':{'agent':'talker',**deepcopy(call)}},
        {'trace_id':901,'turn_id':'turn','call_id':'refused','kind':'tool.error','status':'failed',
         'data':{'agent':'talker',**deepcopy(call),'result':{'ok':False,'executed':False,'retry_model':True,
                  'authorization_guard':guard,'needs_clarification':False}}},
        {'trace_id':902,'turn_id':'turn','call_id':'next-model','kind':'model.started','status':'running','data':{'agent':'talker'}},
        {'trace_id':903,'turn_id':'turn','call_id':'next-model','kind':'model.completed','status':'completed','data':{'agent':'talker'}},
    ])
    return spec,row


@pytest.mark.parametrize('kind',['literal_target_mismatch','literal_window_hold','missing_required_origin'])
def test_typed_refusal_requires_real_same_call_and_model_retry_without_hiding_raw_attempt(kind):
    spec,row=typed_retry_fixture(kind);result=evaluate_record(spec,row,{})
    assert result['pass'],result['assertion_failures']
    assert len(result['guarded_model_attempts'])==1
    guard=result['guarded_model_attempts'][0]
    assert guard['retry_model_verified'] is True
    assert guard['verified_followup_models'][0]['model_call_id']=='next-model'
    assert len(result['raw_model_attempts'])==len(result['effective_tool_calls'])+1
    assert len(result['commands'])==len(spec['actions'])


@pytest.mark.parametrize('mutation',[
    'no_error','wrong_call','wrong_args','stale_error','fake_span','executed','no_retry',
    'no_followup','wrong_followup_call','old_followup_turn','failed_followup','wrong_followup_agent','dispatched',
    'missing_positive_receipt',
])
def test_typed_refusal_cannot_hide_forged_proof_or_incomplete_positive_actions(mutation):
    spec,row=typed_retry_fixture('literal_target_mismatch');failure=row['traces'][-3]
    if mutation=='no_error':row['traces'].remove(failure)
    elif mutation=='wrong_call':failure['call_id']='other'
    elif mutation=='wrong_args':failure['data']['arguments']={'enabled':True}
    elif mutation=='stale_error':failure['status']='stale'
    elif mutation=='fake_span':failure['data']['result']['authorization_guard']['evidence'][0]['start']+=1
    elif mutation=='executed':failure['data']['result']['executed']=True
    elif mutation=='no_retry':failure['data']['result']['retry_model']=False
    elif mutation=='no_followup':row['traces']=row['traces'][:-2]
    elif mutation=='wrong_followup_call':row['traces'][-1]['call_id']='other'
    elif mutation=='old_followup_turn':row['traces'][-1]['turn_id']='old'
    elif mutation=='failed_followup':row['traces'][-1]['status']='failed'
    elif mutation=='wrong_followup_agent':row['traces'][-1]['data']['agent']='thinker'
    elif mutation=='dispatched':row['traces'].append({'trace_id':904,'turn_id':'turn','call_id':'refused','kind':'vehicle.dispatched'})
    elif mutation=='missing_positive_receipt':row['receipts'].pop()
    assert not evaluate_record(spec,row,{})['pass'],mutation


@pytest.mark.parametrize('mutation',['current','changed_preserved','unknown','affected','gold_target','wrong_valid_span','selected_feature','new_task','origin_history'])
def test_typed_refusal_also_binds_snapshot_and_independent_frozen_target(mutation):
    kind='literal_window_hold' if mutation in {'current','changed_preserved','unknown','affected','gold_target','wrong_valid_span'} else (
        'literal_target_mismatch' if mutation=='selected_feature' else 'missing_required_origin')
    spec,row=typed_retry_fixture(kind);guard=row['traces'][-3]['data']['result']['authorization_guard']
    if mutation=='current':guard['current_positions']['front_passenger']=0
    elif mutation=='changed_preserved':row['after']['vehicle']['windows']['positions']['front_passenger']=0
    elif mutation=='unknown':guard['unknown_windows']=['front_passenger']
    elif mutation=='affected':guard['affected_windows']=[]
    elif mutation=='gold_target':guard['evidence'][0]['window']='driver'
    elif mutation=='wrong_valid_span':
        phrase='其余三扇窗都关上';start=spec['query'].index(phrase)
        guard['evidence'][0].update(start=start,end=start+len(phrase),text=phrase)
    elif mutation=='selected_feature':guard['selected_feature']='mirror_heating'
    elif mutation=='new_task':row['after']['tasks']=[{'id':'unasked'}]
    elif mutation=='origin_history':row['before']['history']=[{'role':'user','content':'我在巴黎。'}]
    assert not evaluate_record(spec,row,{})['pass'],mutation


def test_bounded_v6_plan_preserves_all_original_69_gold_and_adds_only_four_weather_turns():
    from pathlib import Path
    old=json.loads(Path('docs/interactions-oracle-v5-plan-20261013.json').read_text())
    assert SCENARIOS+HELDOUT_SCENARIOS==old['scenarios']
    cases=selected_scenarios(BOUNDED_V6_SCENARIO_NAMES)
    assert len(cases)==6 and sum(len(case['steps']) for case in cases)==19
    assert len(ADDITIONAL_SCENARIOS)==1 and len(ADDITIONAL_SCENARIOS[0]['steps'])==4
    for spec in ADDITIONAL_SCENARIOS[0]['steps'][::2]:
        assert spec['query_mode']=='quick' and spec['query_domain']=='weather'
        assert '法国巴黎' in spec['query']


def official_weather_fixture(*,api=True,page=True,official_forecast=False):
    task,events,condition=forecast_fixture();url='https://meteofrance.com/previsions-meteo-france/paris/75000'
    condition.update(required_source_domains=['meteofrance.com'],official_temperature_required=True)
    if page:
        task['result']['sources'].append({'url':url,'kind':'extract','extracted':True})
        task['result']['web_evidence']='verified' if api else 'extracted'
        content=('Forecast in Paris, France\n| 2026-10-14 | 12.3 / 20.1°C |\n' if official_forecast
                 else 'Prévisions météo Paris. Le détail du jour nécessite une page interactive.')
        events.extend([{'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':'search_web','result':{'ok':True}}},
                       {'task_id':'weather','kind':'tool.completed','data':{'agent':'thinker','name':'extract_web',
                        'result':{'ok':True,'results':[{'url':url,'raw_content':content}]}}}])
    if api:
        for field in ('summary','spoken_summary'):
            task['result'][field]='法国气象局官网的巴黎目标日温度尚未核实。Open-Meteo预报巴黎明天12.3到20.1摄氏度。'
    else:
        events=[event for event in events if event['data']['name']!='get_weather_forecast']
        task['result']['forecast_records']=[]
        task['result']['sources']=[source for source in task['result']['sources'] if source['url']==url]
        for field in ('summary','spoken_summary'):
            task['result'][field]='法国气象局官网的巴黎明天温度尚未核实，暂时无法给出数值。'
    return task,events,condition


@pytest.mark.parametrize('api',[False,True])
def test_real_official_extract_with_honest_limitation_is_partial_never_complete_temperature_pass(api):
    task,events,condition=official_weather_fixture(api=api)
    result=task_web_evidence(task,events,condition)
    assert not result['pass'] and result['honest_partial']
    assert not result['official_target_temperature_verified']
    assert 'requested_official_target_temperature_not_verified' in result['assertion_failures']


@pytest.mark.parametrize('mutation',['no_extract','no_limit','no_provider','falsely_official','cross_sentence_official','invented_number'])
def test_official_partial_does_not_excuse_missing_real_page_or_false_temperature_attribution(mutation):
    task,events,condition=official_weather_fixture(api=mutation!='invented_number')
    if mutation=='no_extract':events=[event for event in events if event['data']['name']!='extract_web']
    elif mutation=='no_limit':task['result']['spoken_summary']='Open-Meteo预报巴黎明天12.3到20.1摄氏度。'
    elif mutation=='no_provider':task['result']['spoken_summary']=task['result']['spoken_summary'].replace('Open-Meteo','另一来源')
    elif mutation=='falsely_official':task['result']['spoken_summary']+='法国气象局官网预报12.3到20.1摄氏度。'
    elif mutation=='cross_sentence_official':task['result']['spoken_summary']+='这些数值来自法国气象局官网。'
    elif mutation=='invented_number':task['result']['spoken_summary']+='预计20度。'
    result=task_web_evidence(task,events,condition)
    assert not result['pass'] and not result['honest_partial'],result


def test_official_temperature_complete_pass_requires_actual_adopted_official_target_day_record():
    task,events,condition=official_weather_fixture(official_forecast=True)
    for field in ('summary','spoken_summary'):task['result'][field]='巴黎明天12.3到20.1摄氏度。'
    result=task_web_evidence(task,events,condition)
    assert result['pass'] and result['official_target_temperature_verified'],result


def test_official_partial_recall_preserves_stored_limitation_and_adds_no_temperature():
    spec=deepcopy(ADDITIONAL_SCENARIOS[0]['steps'][3]);row=evidence(spec)
    task={'id':'official','status':'completed','result':{
        'spoken_summary':'法国气象局官网的巴黎明天温度尚未核实，暂时无法给出数值。',
        'summary':'已读取官网页面，法国气象局官网的巴黎明天温度尚未核实。'}}
    row['before']['tasks']=[deepcopy(task)];row['after']['tasks']=[deepcopy(task)]
    row['text']='巴黎明天的温度无法确认，法国气象局官网目标日的温度尚未核实。'
    assert evaluate_record(spec,deepcopy(row),{'official_weather':'official'})['pass']
    for text in ['巴黎明天最高20度，官网尚未核实。','巴黎明天尚未核实。','法国气象局官网已核实，巴黎明天无温度。']:
        bad=deepcopy(row);bad['text']=text
        assert not evaluate_record(spec,bad,{'official_weather':'official'})['pass'],text


def test_official_recall_keeps_actual_api_provider_and_official_limitation_separate():
    spec=deepcopy(ADDITIONAL_SCENARIOS[0]['steps'][3]);row=evidence(spec)
    body='法国气象局官网的巴黎明天温度尚未核实。Open-Meteo预报巴黎明天12.3到20.1摄氏度。'
    task={'id':'official','status':'completed','result':{'spoken_summary':body,'summary':body}}
    row['before']['tasks']=[deepcopy(task)];row['after']['tasks']=[deepcopy(task)];row['text']=body
    assert evaluate_record(spec,deepcopy(row),{'official_weather':'official'})['pass']
    for text in [body.replace('Open-Meteo','另一来源'),body.replace('法国气象局官网的巴黎明天温度','部分信息'),
                 body+'这些温度来自法国气象局官网。']:
        bad=deepcopy(row);bad['text']=text
        assert not evaluate_record(spec,bad,{'official_weather':'official'})['pass']


def test_declared_weather_domain_is_retained_in_real_registration_not_only_model_arguments():
    spec=deepcopy(ADDITIONAL_SCENARIOS[0]['steps'][0]);row=evidence(spec)
    call=action('delegate_task',goal='查询法国巴黎明天温度',query_mode='quick',query_domain='weather',read_details=False)
    row['traces']=[{'trace_id':1,'turn_id':'turn','call_id':'delegate','kind':'tool.started','data':{'agent':'talker',**call}}]
    row['after']['tasks']=[{'id':'weather','goal':call['arguments']['goal'],'context':{'query_domain':'weather'}}]
    assert evaluate_record(spec,deepcopy(row),{})['pass']
    for mutation in ('args','context'):
        bad=deepcopy(row)
        if mutation=='args':bad['traces'][0]['data']['arguments']['query_domain']='general'
        else:bad['after']['tasks'][0]['context']['query_domain']='general'
        assert not evaluate_record(spec,bad,{})['pass']


def test_summary_keeps_honest_partial_in_failed_denominator_and_handles_nonweb_wait():
    report={'groups':[{'spec':{'steps':[{},{}]},'records':[
        {'pass':False,'wait_after':{'actual_web_fact_evidence':{'honest_partial':True}}},
        {'pass':True,'wait_after':{'actual_web_fact_evidence':None}}]}]}
    result=report_summary(report)
    assert result['honest_partial_results_not_full_pass']==1
    assert result['failed_user_turns']==1 and result['passed_user_turns']==1
