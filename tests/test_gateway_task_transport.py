"""The NLP ledger client enforces remote scope, leases and honest write outcomes."""
import json

import httpx
import pytest

from backend.harness.diagnostics import safe_diagnostic
from backend.harness.task_transport import GatewayTaskStore, GatewayTransportError, GatewayConflict


def task(**changes):
    return {'id': 'task', 'session_id': 'conversation', 'goal': '核实资料', 'revision': 1,
            'status': 'running', 'deadline_at': '2026-10-17T12:01:00+00:00', **changes}


@pytest.fixture
def gateway_client():
    clients = []
    def build(handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return GatewayTaskStore('https://gateway.example', token='integration-secret', client=client)
    yield build


async def test_real_remote_create_and_mutation_preserve_scope_and_idempotency(gateway_client):
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        assert request.headers['Authorization'] == 'Bearer integration-secret'
        revision = 2 if request.url.path.endswith('/mutate') else 1
        return httpx.Response(200, json={'task': task(revision=revision)})
    store = gateway_client(handler).for_scope('conversation')
    created = await store.create_task('task', '核实资料', {}, 'invocation-a')
    changed = await store.mutate_task('task', 1, 'update', '补充范围', 'invocation-b')
    assert created['revision'] == 1 and changed['revision'] == 2
    assert seen[0][1]['idempotency_key'] == 'invocation-a'
    assert seen[1][1]['expected_revision'] == 1
    assert all(row[1]['conversation_id'] == 'conversation' for row in seen)


async def test_execution_writes_require_lease_and_cas_conflicts_are_not_success(gateway_client):
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(409, json={'private_detail': 'must not leak'})
    owner = gateway_client(handler)
    with pytest.raises(ValueError):
        await owner.for_scope('conversation').save('task', 1, status='completed')
    assert not seen
    store = owner.for_scope('conversation', execution_id='execution', lease_token='lease-secret')
    assert await store.save('task', 1, status='completed') is False
    assert seen[0]['lease_token'] == 'lease-secret' and seen[0]['execution_id'] == 'execution'
    with pytest.raises(GatewayConflict):
        await store.refresh('task')


@pytest.mark.parametrize('payload', [
    {'task': task(session_id='other')}, {'task': task(id='other')},
    {'task': task(revision='1')}, {'task': task(deadline_at='2026-10-17T12:00:00')},
    {'task': task(running_seconds=float('inf'))},
])
async def test_malformed_or_cross_conversation_task_never_enters_runner(gateway_client, payload):
    # Explicit raw encoding allows the test to exercise invalid JSON numbers.
    store = gateway_client(lambda request: httpx.Response(200, content=json.dumps(payload))).for_scope('conversation')
    with pytest.raises(GatewayTransportError):
        await store.read('task')


async def test_ambiguous_write_failure_is_never_retried_or_reported_created(gateway_client):
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout('response lost after commit; integration-secret', request=request)
    store = gateway_client(handler).for_scope('conversation')
    with pytest.raises(GatewayTransportError) as failure:
        await store.create_task('task', '目标', {}, 'stable-key')
    assert failure.value.uncertain is True
    assert 'integration-secret' not in str(failure.value)
    assert len(requests) == 1


async def test_usage_and_diagnostics_are_remote_acknowledged_and_redacted(gateway_client):
    seen = []
    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith('/usage'):
            return httpx.Response(200, json={'execution_seconds': 3.5, 'lookup_count': 2})
        return httpx.Response(200, json={'accepted': True})
    store = gateway_client(handler).for_scope('conversation', execution_id='execution', lease_token='lease-secret')
    await store.emit('conversation', 'tool.completed', {'Authorization': 'Bearer private',
                     'nested': 'integration-secret lease-secret', 'source_url': 'https://research.example/source'})
    assert await store.usage('task', 'conversation') == (3.5, 2)
    record = seen[0][1]['record']
    assert 'integration-secret' not in json.dumps(record) and 'lease-secret' not in json.dumps(record)
    assert record['data']['source_url'] == 'https://research.example/source'
    assert seen[0][1]['lease_token'] == 'lease-secret'  # Transport scope is not a diagnostic field.


async def test_redirects_and_large_or_unacknowledged_responses_do_not_count_as_success(gateway_client):
    owner = gateway_client(lambda request: httpx.Response(302, headers={'Location': 'https://another.example'}))
    with pytest.raises(GatewayTransportError):
        await owner.for_scope('conversation').read('task')
    owner = gateway_client(lambda request: httpx.Response(200, content='x' * 50))
    owner.max_response_bytes = 20
    with pytest.raises(GatewayTransportError) as failure:
        await owner.for_scope('conversation').read('task')
    assert failure.value.code == 'gateway_response_too_large'


def test_nested_media_capabilities_are_removed_without_losing_research_sources():
    value = {'sources': [{'source_url': 'https://research.example/evidence'}],
             'content': json.dumps({'items': [{'content_id': 'song', 'source_url': 'https://media.example/signed',
                                              'kind': 'music'}]}),
             'X-Goog-Api-Key': 'private-key'}
    safe = safe_diagnostic(value)
    assert 'media.example' not in json.dumps(safe)
    assert safe['sources'] == value['sources']
    assert 'private-key' not in json.dumps(safe)
