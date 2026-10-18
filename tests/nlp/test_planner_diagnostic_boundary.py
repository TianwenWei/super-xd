"""Provider audit payloads never become vehicle plans or resumable business state."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from super_xd.demo.devices.simulator import execute_tool
from super_xd.nlp.contracts.gateway_v1 import MAX_CONTINUATION_BYTES, wire_bytes
from super_xd.nlp.contracts.providers import ModelResult
from super_xd.nlp.orchestration.vehicle_planner import VehicleControlPlanner
from tests.support.gateway_diagnostics import BatchDiagnostics
from tests.support.orchestration import StreamModel, kernel, message, outputs, request, resume
from tests.support.vehicle_planner import action, intent, plan, response


DIAGNOSTIC_FIELDS = {'model_observations', 'load_evidence', 'trace_events'}
AUDIT_MARKER = 'provider-request-audit-only'


class AuditedPlannerModel:
    """Mirror provider metadata carrying the complete request at observed sizes."""
    def __init__(self, submission):
        self.submission, self.requests = submission, []

    async def complete(self, request):
        actual = asdict(request)
        # Real provider request metadata includes its deployed prompt wrapper.
        # About 60 KiB per call reproduces the real two-call clarification trace.
        actual['messages'].insert(0, {'role': 'system', 'content': (AUDIT_MARKER + '\n') * 2300})
        self.requests.append(deepcopy(actual))
        if self.submission is None:
            result = {'role': 'assistant', 'content': '需要按工具协议提交。'}
        elif len(self.requests) == 1:
            result = response({'skill_id': 'mirrors'}, name='load_skill', identity='load')
        else:
            result = response(self.submission)
        result.pop('_meta', None)
        return ModelResult(result, {'model': 'fixture-planner', 'actual_request': actual,
                                    'usage': {'input_tokens': 16000, 'output_tokens': 100}})


@pytest.mark.parametrize('status', ['planned', 'clarification', 'refused', 'empty_refusal'])
async def test_large_planner_diagnostics_stay_outside_checkpoint_and_recovery(status):
    query = '调整后视镜，然后把音量设为20。'
    source = '调整后视镜'
    question = '后视镜要折叠还是展开？' if status == 'clarification' else ''
    reason = '当前不能安全调整。' if status == 'refused' else ''
    actions = [action('fold', 'mirror', 'set_mirror_fold', {'folded': True})] if status == 'planned' else []
    submission = None if status == 'empty_refusal' else plan([
        intent('mirror', query, source, status=status, actions=['fold'] if actions else [],
               question=question, reason=reason)], actions, status=status, question=question, reason=reason)
    input_ = request(query, tools=[('set_mirror_fold', {}, 'control', 'device'),
                                   ('set_volume', {}, 'control', 'device')])
    input_.budget.max_model_calls = 10
    talker = StreamModel(message('delegate_vehicle_control', {'skill_ids': ['mirrors']}),
                         message('set_volume', {'volume': 20}, identity='volume'))
    model, diagnostics = AuditedPlannerModel(submission), BatchDiagnostics()
    service = kernel(talker, input_, planner=VehicleControlPlanner(model), diagnostics=diagnostics)

    first = (await outputs(service.start(input_)))[-1]
    assert first.type == 'run.suspended'
    checkpoint = first.checkpoint
    encoded = checkpoint.model_dump_json()
    assert AUDIT_MARKER not in encoded
    assert not any(f'"{key}"' in encoded for key in DIAGNOSTIC_FIELDS)
    assert wire_bytes(checkpoint.model_dump(exclude={'start'})) < MAX_CONTINUATION_BYTES // 8
    assert checkpoint.orchestration.model_calls == len(model.requests)
    assert all(wire_bytes(actual) > 60_000 for actual in model.requests)
    assert checkpoint.run.model_calls + checkpoint.orchestration.model_calls <= input_.budget.max_model_calls
    assert checkpoint.start.identity == input_.identity

    group = checkpoint.orchestration.groups[0]
    if status == 'planned':
        public_plan = group.plan.model_dump()
        assert group.plan.actions[0].arguments == {'folded': True}
        vehicle = deepcopy(input_.context.vehicle.state)
        execute_tool('set_mirror_fold', {'folded': True}, vehicle)
        receipt = {'ok': True, 'executed': True, 'vehicle_state': vehicle}
    else:
        result = next(iter(group.results.values())).result
        public_plan = result['plan']
        assert public_plan['status'] == ('refused' if status == 'empty_refusal' else status)
        assert result.get('question', '') == question
        assert len(talker.inputs) == 2
        assert AUDIT_MARKER not in json.dumps(talker.inputs[1], ensure_ascii=False)
        command = first.requests[0].dispatch.prepared_command
        receipt = {'ok': True, 'executed': True, 'state': {**command['expected'], 'version': 1}}
    assert public_plan['model_calls'] == len(model.requests)
    assert public_plan['loaded_skills'] == ['mirrors']
    assert public_plan['provenance']['plan_call_id'] == public_plan['plan_call_id']
    assert public_plan['provenance']['model_call_id'] == public_plan['model_call_id']
    if status != 'empty_refusal':
        assert public_plan['intents'][0]['source_text'] == source
        assert query[public_plan['intents'][0]['source_start']:public_plan['intents'][0]['source_end']] == source
        assert public_plan['validation']

    complete = (await outputs(resume(service, first, receipt)))[-1]
    assert complete.type == 'run.completed'
    assert complete.checkpoint.orchestration.model_calls == len(model.requests)
    assert complete.checkpoint.start.identity == input_.identity
    assert len(model.requests) == (4 if status == 'empty_refusal' else 2)
    assert AUDIT_MARKER not in complete.checkpoint.model_dump_json()
    planner_traces = [record.data for record in diagnostics.records
                      if record.kind == 'orchestration.event' and record.data['kind'] == 'model.completed']
    assert [event['data']['meta']['actual_request'] for event in planner_traces] == model.requests
    assert any(record.kind == 'orchestration.event' and record.data['kind'] == 'vehicle.skill.loaded'
               for record in diagnostics.records)
