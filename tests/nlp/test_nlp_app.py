"""Exercise the actual HTTP boundary with explicit offline models."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from super_xd.nlp.api.app import create_app
from tests.support.gateway_contract import ScriptedModel, call, service, start_request
from super_xd.demo.devices.simulator import execute_tool


async def post(app, path, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://nlp.test') as client:
        return await client.post(path, json=payload)


@pytest.mark.asyncio
async def test_http_start_receipt_resume_and_policy_version_conflict():
    model = ScriptedModel([{'content': '这就调整音量。', 'tool_calls': [call()]}, {'content': '已收到结果。'}])
    app = create_app(service(model))
    started = await post(app, '/v1/runs/start', start_request().model_dump())
    assert started.status_code == 200
    assert started.headers['content-type'].startswith('application/x-ndjson')
    events = [json.loads(line) for line in started.text.splitlines()]
    handoff = events[-1]
    assert handoff['type'] == 'run.suspended'
    request = handoff['requests'][0]['request']
    vehicle = deepcopy(handoff['checkpoint']['start']['context']['vehicle']['state'])
    assert execute_tool('set_volume', {'volume': 40}, vehicle)['ok']
    payload = {'identity': handoff['identity'], 'checkpoint': handoff['checkpoint'], 'observations': [
        {'identity': handoff['identity'], 'invocation_id': request['invocation_id'], 'status': 'completed',
         'result': {'ok': True, 'executed': True, 'vehicle_state': vehicle}}]}
    resumed = await post(app, '/v1/runs/resume', payload)
    assert resumed.status_code == 200
    terminal = json.loads(resumed.text.splitlines()[-1])
    assert terminal['type'] == 'run.completed'
    assert (await post(app, '/v1/runs/resume', {'identity': terminal['identity'],
                                              'checkpoint': terminal['checkpoint']})).status_code == 409
    payload['checkpoint']['model_policy_id'] = 'other-release'
    assert (await post(app, '/v1/runs/resume', payload)).status_code == 409


@pytest.mark.asyncio
async def test_validation_never_echoes_input_and_body_is_bounded():
    app = create_app(service(ScriptedModel([])))
    value = start_request().model_dump()
    value['secret'] = 'DO-NOT-ECHO'
    value['PRIVATE-DYNAMIC-FIELD-NAME'] = 'value'
    response = await post(app, '/v1/runs/start', value)
    assert response.status_code == 422 and 'DO-NOT-ECHO' not in response.text
    assert 'PRIVATE-DYNAMIC-FIELD-NAME' not in response.text
    assert response.json() == {'error_code': 'invalid_request', 'error_count': 2}
    limited = create_app(service(ScriptedModel([])), max_body_bytes=20)
    assert (await post(limited, '/v1/runs/start', value)).status_code == 413


@pytest.mark.asyncio
async def test_chunked_body_limit_applies_without_content_length():
    app = create_app(service(ScriptedModel([])), max_body_bytes=20)

    async def chunks():
        yield b'{"text":"'
        yield b'x' * 30
        yield b'"}'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://nlp.test') as client:
        response = await client.post('/v1/runs/start', content=chunks())
    assert response.status_code == 413


class BlockingModel:
    def __init__(self):
        self.started, self.closed = asyncio.Event(), asyncio.Event()

    async def stream(self, *args, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            self.closed.set()


@pytest.mark.asyncio
async def test_live_cancel_checks_full_identity_and_stops_upstream():
    model = BlockingModel()
    app = create_app(service(model), max_inflight=1)
    identity = start_request().identity.model_dump()
    running = asyncio.create_task(post(app, '/v1/runs/start', start_request().model_dump()))
    await asyncio.wait_for(model.started.wait(), 1)
    other = start_request().model_dump()
    other['identity']['run_id'] = 'second'
    assert (await post(app, '/v1/runs/start', other)).status_code == 429
    wrong = {**identity, 'context_revision': 8}
    assert (await post(app, '/v1/runs/cancel', {'identity': wrong})).status_code == 409
    assert (await post(app, '/v1/runs/cancel', {'identity': identity})).status_code == 200
    response = await asyncio.wait_for(running, 1)
    assert json.loads(response.text.splitlines()[-1])['type'] == 'run.cancelled'
    assert model.closed.is_set()
    assert (await post(app, '/v1/runs/cancel', {'identity': identity})).status_code == 404


@pytest.mark.asyncio
async def test_transport_deadline_is_explicit_and_closes_model():
    model = BlockingModel()
    app = create_app(service(model), request_timeout_seconds=0.02)
    response = await post(app, '/v1/runs/start', start_request().model_dump())
    error = json.loads(response.text.splitlines()[-1])
    assert error['type'] == 'transport.error' and error['outcome_unknown']
    assert error['error_code'] == 'request_deadline_exceeded'
    assert model.closed.is_set()


@pytest.mark.asyncio
async def test_disconnect_cancels_model_and_releases_capacity():
    model = BlockingModel()
    app = create_app(service(model), max_inflight=1)
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': start_request().model_dump_json().encode(), 'more_body': False})
    output = []

    async def send(message):
        output.append(message)

    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.0'},
             'method': 'POST', 'path': '/v1/runs/start', 'raw_path': b'/v1/runs/start',
             'query_string': b'', 'headers': [(b'content-type', b'application/json')],
             'scheme': 'http', 'server': ('test', 80), 'client': ('gateway', 1000), 'root_path': ''}
    running = asyncio.create_task(app(scope, messages.get, send))
    await asyncio.wait_for(model.started.wait(), 1)
    await messages.put({'type': 'http.disconnect'})
    await asyncio.wait_for(running, 1)
    assert model.closed.is_set()
    response = await post(app, '/v1/runs/cancel', {'identity': start_request().identity.model_dump()})
    assert response.status_code == 404


@pytest.mark.parametrize('failure_at', ['http.response.start', 'http.response.body'])
async def test_asgi_send_failure_closes_suspended_or_unstarted_stream(failure_at):
    from starlette.requests import ClientDisconnect
    from tests.support.gateway_diagnostics import BatchDiagnostics, ChunkModel

    model, diagnostics = ChunkModel(['部分答复'], block=True), BatchDiagnostics()
    app = create_app(service(model, diagnostics=diagnostics), max_inflight=1)
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': start_request().model_dump_json().encode(), 'more_body': False})

    async def send(message):
        if message['type'] == failure_at:
            raise OSError('socket closed')

    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
             'method': 'POST', 'path': '/v1/runs/start', 'raw_path': b'/v1/runs/start',
             'query_string': b'', 'headers': [(b'content-type', b'application/json')],
             'scheme': 'http', 'server': ('test', 80), 'client': ('gateway', 1000), 'root_path': ''}
    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(app(scope, messages.get, send), 1)
    response = await post(app, '/v1/runs/cancel', {'identity': start_request().identity.model_dump()})
    assert response.status_code == 404
    if failure_at == 'http.response.body':
        assert model.closed.is_set()
        assert [record.event.type for record in diagnostics.records] == ['model.started', 'text.delta', 'run.cancelled']


async def test_task_presentation_endpoint_preserves_stale_event_suppression():
    from super_xd.nlp.orchestration.tasks.service import TaskService

    class Store:
        def for_scope(self, conversation):
            assert conversation == 'conversation'
            return self

        async def read(self, task_id):
            return {'id': task_id, 'session_id': 'conversation', 'revision': 2, 'status': 'completed',
                    'goal': '核验资料', 'result': {'summary': '已有可靠结论'},
                    'context': {'release_id': 'release', 'model_policy_id': 'models'}}

        async def emit(self, *args, **kwargs):
            pass

    task_service = TaskService(Store(), lambda store: None, release_id='release', model_policy_id='models')
    app = create_app(service(ScriptedModel([]), task_service=task_service))
    payload = {'conversation_id': 'conversation', 'task_id': 'task', 'revision': 1, 'expected_status': 'completed'}
    stale = await post(app, '/v1/tasks/present', payload)
    assert stale.status_code == 200 and stale.json()['stale']
    assert stale.json()['speech'] == '' and stale.json()['card'] is None
    current = await post(app, '/v1/tasks/present', {**payload, 'revision': 2})
    assert current.status_code == 200 and not current.json()['stale']
    assert '已有可靠结论' in current.json()['speech']
