"""A ledger validates execution data without discarding its audit extensions."""
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from super_xd.nlp.adapters.gateway.tasks import GatewayTaskStore
from super_xd.nlp.contracts.task_state import TASK_PATCH, TaskCheckpoint
from super_xd.nlp.contracts.tasks import TaskExecutionResult, TaskSnapshot


def checkpoint():
    return {'checkpoint_version': 1, 'release_id': 'nlp-current', 'goal': '核实公开资料',
            'revision': 2, 'messages': [{'role': 'user', 'content': '公开资料'}],
            'sources': [], 'suggestions': [], 'observations': [], 'tool_count': 0,
            'retrieval': {'version': 1, 'records': []}, 'constraint_count': 1}


def task():
    return {'id': 'task', 'session_id': 'conversation', 'goal': '核实公开资料',
            'revision': 2, 'status': 'running', 'deadline_at': '2026-10-18T12:01:00+00:00',
            'checkpoint': checkpoint(), 'context': {'query_mode': 'quick', 'audit_tag': 'retained'},
            'audit': {'source': 'gateway'}}


@pytest.mark.parametrize('changes', [
    {'tool_count': -1}, {'revision': '2'}, {'messages': 'not-a-transcript'},
    {'unrecognized_resume_field': True},
])
def test_corrupt_checkpoint_is_rejected_at_the_ledger_boundary(changes):
    value = task()
    value['checkpoint'].update(changes)
    with pytest.raises(ValidationError):
        TaskSnapshot.model_validate(value)


def test_empty_checkpoint_means_unstarted_but_partial_checkpoint_is_invalid():
    value = task()
    value['checkpoint'] = {}
    assert TaskSnapshot.model_validate(value).checkpoint == {}
    value['checkpoint'] = {'checkpoint_version': 1, 'release_id': 'nlp-current'}
    with pytest.raises(ValidationError):
        TaskSnapshot.model_validate(value)


def test_checkpoint_roundtrip_preserves_all_continuation_fields():
    original = checkpoint()
    assert TaskCheckpoint.model_validate(original).model_dump() == original


def test_execution_result_preserves_dict_state_and_gateway_audit_extensions():
    original = task()
    result = TaskExecutionResult(task=deepcopy(original), requested_revision=2,
                                 release_id='nlp-current', model_policy_id='models-current')
    assert result.task == original
    assert result.model_dump()['task'] == original
    assert 'TaskState' in TaskExecutionResult.model_json_schema()['$defs']


@pytest.mark.parametrize('fields', [
    {'goal': 'cannot rewrite registration'}, {'steps': '1'},
    {'status': 'finished'}, {'progress': {'read_sources': -1}},
])
def test_task_patch_rejects_unknown_or_mistyped_execution_writes(fields):
    with pytest.raises(ValidationError):
        TASK_PATCH.validate_python(fields)


async def test_invalid_task_write_is_rejected_before_network():
    attempted = []

    def handler(request):
        attempted.append(request)
        return httpx.Response(200, json={'applied': True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = GatewayTaskStore('https://gateway.example', client=client).for_scope(
            'conversation', execution_id='execution', lease_token='lease')
        with pytest.raises(ValidationError):
            await store.save('task', 2, goal='rewrite immutable registration')
    assert not attempted
