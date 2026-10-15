"""Independent gold fixtures and adversarial evaluator checks; no network."""
from copy import deepcopy

import pytest

from backend.catalog import HOT_TOOL_NAMES, execute_tool, initial_vehicle_state
from scripts.evaluate_progressive_control import SCENARIOS, evaluate


def observed(spec, state):
    before = deepcopy(state)
    commands, receipts = [], []
    for index, action in enumerate(spec['actions']):
        command = {**deepcopy(action), 'command_id':f'command-{index}',
                   'action_id':f'action-{index}', 'intent_id':'intent-1', 'parent_call_id':'delegate-1'}
        result = execute_tool(action['name'], action['arguments'], state)
        assert result['ok'], (spec['id'], result)
        commands.append(command)
        receipts.append({'command_id':command['command_id'],'result':deepcopy(result)})
    tools = [{'function':{'name':name}} for name in [*HOT_TOOL_NAMES,'delegate_vehicle_control','delegate_task','manage_task']]
    traces = [
        {'turn_id':'turn-1','kind':'model.started','data':{'agent':'talker','tools':tools}},
        {'turn_id':'turn-1','kind':'turn.completed','data':{}},
    ]
    if spec['route']=='pro':
        ledger = {'ok':True,'status':'completed','intents':[{'id':'intent-1','status':'completed'}],
                  'action_receipts':[{'action_id':command['action_id'],'intent_id':command['intent_id'],
                     'command_id':command['command_id'],'name':command['name'],
                     'arguments':command['arguments'],'status':'completed','result':receipt['result']}
                    for command,receipt in zip(commands,receipts)]}
        traces.extend([
            {'turn_id':'turn-1','kind':'model.started','data':{'agent':'vehicle_planner','messages':[{'content':spec['text']}]}},
            {'turn_id':'turn-1','kind':'model.completed','data':{'agent':'vehicle_planner','meta':{'model':'pro-observation'}}},
            {'turn_id':'turn-1','kind':'vehicle.plan.accepted','data':{}},
            {'turn_id':'turn-1','call_id':'delegate-1','kind':'vehicle.plan.completed','data':{'ledger':ledger}},
        ])
    answers = {'window_state':'主驾车窗50%。','heat_state':'后视镜加热已开启。','airport':'出发位置在哪里？'}
    return {'turn_id':'turn-1','commands':commands,'receipts':receipts,'before':{'vehicle':before},
            'after':{'vehicle':deepcopy(state)},'traces':traces,'text':answers.get(spec['id'],''),'metrics':{}}


@pytest.mark.parametrize('scenario',SCENARIOS,ids=lambda group:group['id'])
def test_predeclared_gold_actions_match_executor_side_effects(scenario):
    # This only detects faulty gold expectations; it is never a live result.
    state = initial_vehicle_state()
    for spec in scenario['steps']:
        row = observed(spec,state)
        evaluate(spec,row,'pro-observation')
        assert row['pass'], row['assertion_failures']


@pytest.mark.parametrize('fault',[
    'extra_command','missing_receipt','fabricated_ledger','unresolved_intent',
    'missing_action_binding','cold_talker_schema','missing_turn_trace','recording_error',
    'rewritten_original','wrong_pro_model','wrong_state',
])
def test_oracle_rejects_false_success(fault):
    spec = SCENARIOS[1]['steps'][0]
    row = observed(spec,initial_vehicle_state())
    ledger = row['traces'][-1]['data']['ledger']
    if fault=='extra_command': row['commands'].append(deepcopy(row['commands'][0]))
    elif fault=='missing_receipt': row['receipts'].clear()
    elif fault=='fabricated_ledger': ledger['action_receipts'][0]['command_id']='simulated-only'
    elif fault=='unresolved_intent': ledger['intents'][0]['status']='planned'
    elif fault=='missing_action_binding': ledger['action_receipts'].clear()
    elif fault=='cold_talker_schema': row['traces'][0]['data']['tools'].append({'function':{'name':'set_mirror_heating'}})
    elif fault=='missing_turn_trace': row['traces']=[t for t in row['traces'] if t['kind']!='turn.completed']
    elif fault=='recording_error': row['trace_errors']=['database-write-failed']
    elif fault=='rewritten_original': row['traces'][2]['data']['messages']=[{'content':'打开加热。'}]
    elif fault=='wrong_pro_model': row['traces'][3]['data']['meta']['model']='mini-observation'
    elif fault=='wrong_state': row['after']['vehicle']['mirrors']['heating']=False
    evaluate(spec,row,'pro-observation')
    assert not row['pass'], fault
    assert row['assertion_failures']
