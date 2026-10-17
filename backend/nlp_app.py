"""Internal Gateway-facing NLP transport. Conversation state stays at Gateway."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, aclosing
import json
import math
import os
import time

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from backend.harness.background import TaskExecutionRequest, TaskMutationRequest, TaskPresentationRequest
from backend.harness.contracts import Contract, RunIdentity
from backend.harness.gateway import ResumeRequest, StartRequest
from backend.harness.run import RunProtocolError
from backend.harness.task_transport import GatewayConflict, GatewayTransportError


class CancelRequest(Contract):
    identity: RunIdentity


class ClosingStreamingResponse(StreamingResponse):
    """Close producers even when ASGI send fails while they are at a yield."""

    def __init__(self, content, *, on_close, **kwargs):
        super().__init__(content, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                # Starlette's disconnect cancellation scope may still be active.
                with anyio.CancelScope(shield=True):
                    await self.body_iterator.aclose()
            finally:
                # An unstarted async generator does not execute its finally.
                self.on_close()


class BodyLimit:
    """Bound chunked bodies before JSON parsing, retaining disconnect delivery."""

    def __init__(self, app, *, limit, timeout=30):
        self.app, self.limit, self.timeout = app, limit, timeout

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    size += len(chunk)
                    if size > self.limit:
                        return await JSONResponse({'error_code': 'request_too_large'}, 413)(scope, receive, send)
                    chunks.append(chunk)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            return await JSONResponse({'error_code': 'request_body_timeout'}, 408)(scope, receive, send)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(service=None, *, max_body_bytes=8 * 1024 * 1024, max_inflight=32,
               request_timeout_seconds=120, bundle_factory=None):
    """Injected services support offline integration tests; normal startup is real."""
    if type(max_body_bytes) is not int or max_body_bytes < 1 or type(max_inflight) is not int or max_inflight < 1:
        raise ValueError('Positive body and concurrency limits are required')
    if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
        raise ValueError('A positive finite request deadline is required')
    active, slots = {}, set()

    @asynccontextmanager
    async def lifespan(app):
        bundle = None
        if service is None:
            if bundle_factory is None:
                from backend.harness.assembly import create_nlp_services
                factory = create_nlp_services
            else:
                factory = bundle_factory
            bundle = await factory()
            app.state.nlp = bundle.service
        else:
            app.state.nlp = service
        try:
            yield
        finally:
            for run in tuple(active.values()):
                run.cancel()
            if bundle is not None:
                await bundle.aclose()

    app = FastAPI(title='Super XD NLP', version='1', lifespan=lifespan)
    if service is not None:
        app.state.nlp = service
    app.add_middleware(BodyLimit, limit=max_body_bytes)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Validation input can contain conversations, signed media URLs and leases.
        return JSONResponse({'error_code': 'invalid_request',
                             'error_count': len(error.errors())}, 422)

    @app.exception_handler(RunProtocolError)
    async def incompatible_request(request, error):
        return JSONResponse({'error_code': 'run_protocol_conflict'}, 409)

    @app.exception_handler(GatewayTransportError)
    async def gateway_error(request, error):
        return JSONResponse({'error_code': error.code, 'outcome_unknown': error.uncertain},
                            409 if isinstance(error, GatewayConflict) else 503)

    @app.get('/health/live')
    async def health():
        nlp = app.state.nlp
        return {'status': 'ready', 'release_id': nlp.release_id,
                'model_policy_id': nlp.model_policy_id,
                'semantic_policy_id': nlp.semantic_policy.policy_id}

    def reserve():
        if len(slots) >= max_inflight:
            return None
        token = object()
        slots.add(token)
        return token

    def stream(run, identity):
        if identity.run_id in active:
            raise RunProtocolError('This run already has an active stream on this instance')
        token = reserve()
        if token is None:
            return JSONResponse({'error_code': 'capacity_exceeded'}, 429, headers={'Retry-After': '1'})
        active[identity.run_id] = run
        released = False

        def release():
            nonlocal released
            if released:
                return
            released = True
            run.cancel()
            active.pop(identity.run_id, None)
            slots.discard(token)

        async def lines():
            deadline = time.monotonic() + request_timeout_seconds
            try:
                async with aclosing(run.events()) as events:
                    while True:
                        try:
                            event = await asyncio.wait_for(anext(events), max(0, deadline - time.monotonic()))
                        except StopAsyncIteration:
                            break
                        yield event.model_dump_json() + '\n'
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Headers have already been sent. A transport failure is not a
                # synthetic agent completion and must never authorize execution.
                code = 'request_deadline_exceeded' if isinstance(error, TimeoutError) else 'stream_interrupted'
                yield json.dumps({'protocol_version': 'gateway-v1', 'type': 'transport.error',
                                  'identity': identity.model_dump(), 'error_code': code,
                                  'outcome_unknown': True}) + '\n'
            finally:
                release()

        return ClosingStreamingResponse(lines(), on_close=release, media_type='application/x-ndjson',
                                        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.post('/v1/runs/start')
    async def start(body: StartRequest):
        return stream(app.state.nlp.start(body), body.identity)

    @app.post('/v1/runs/resume')
    async def resume(body: ResumeRequest):
        return stream(app.state.nlp.resume(body), body.identity)

    @app.post('/v1/runs/cancel')
    async def cancel(body: CancelRequest):
        run = active.get(body.identity.run_id)
        if run is None:
            return JSONResponse({'error_code': 'run_not_active_here'}, 404)
        if run.start.identity != body.identity:
            raise RunProtocolError('Cancellation belongs to another run/context')
        run.cancel()
        return {'accepted': True}

    async def task_operation(request, operation):
        token = reserve()
        if token is None:
            return JSONResponse({'error_code': 'capacity_exceeded'}, 429)
        task = asyncio.create_task(operation())

        async def disconnected():
            while True:
                message = await request.receive()
                if message['type'] == 'http.disconnect':
                    return

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((task, watcher), timeout=request_timeout_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            return JSONResponse({'error_code': 'client_disconnected' if watcher in done else 'request_deadline_exceeded',
                                 'outcome_unknown': True}, 504)
        finally:
            task.cancel()
            watcher.cancel()
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(task, watcher, return_exceptions=True)
            finally:
                slots.discard(token)

    @app.post('/v1/tasks/execute')
    async def execute_task(body: TaskExecutionRequest, request: Request):
        return await task_operation(request, lambda: app.state.nlp.execute_task(body))

    @app.post('/v1/tasks/mutate')
    async def mutate_task(body: TaskMutationRequest, request: Request):
        return await task_operation(request, lambda: app.state.nlp.mutate_task(body))

    @app.post('/v1/tasks/present')
    async def present_task(body: TaskPresentationRequest, request: Request):
        return await task_operation(request, lambda: app.state.nlp.present_task(body))

    return app


def configured_app():
    return create_app(max_body_bytes=int(os.getenv('NLP_MAX_BODY_BYTES', str(8 * 1024 * 1024))),
                      max_inflight=int(os.getenv('NLP_MAX_INFLIGHT', '32')),
                      request_timeout_seconds=float(os.getenv('NLP_REQUEST_TIMEOUT_SECONDS', '120')))
