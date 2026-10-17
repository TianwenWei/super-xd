"""Small OpenAI-compatible Ark transport; secrets stay in this process."""
import asyncio
import json
import os
import time
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from backend.prompt_templates import render_prompt
from backend.settings import TALKER_MODEL, THINKER_MODEL, MODEL_TIMEOUT
from backend.services.contracts import redact
from backend.textutil import SECRET_ENV, is_domain, redact as redact_text, remove_urls


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _merge_tool_metadata(target, incoming):
    """Opaque provider metadata is atomic, unlike streamed function arguments."""
    for key, value in incoming.items():
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_tool_metadata(target[key], value)
        elif target[key] != value:
            raise ValueError('Conflicting provider tool metadata in stream')


def sanitized_error(exc, secrets=()):
    if isinstance(exc, httpx.HTTPStatusError):
        code = ''
        try:
            error = exc.response.json().get('error', {})
            code = error.get('code', '') if isinstance(error, dict) else ''
        except Exception:
            pass
        value = f'HTTP {exc.response.status_code}' + (f' ({code})' if code else '')
    elif isinstance(exc, httpx.TimeoutException):
        return 'network timeout'
    else:
        value = str(exc)
    for key in ('ARK_API_KEY', 'ARK_EMBEDDING_API_KEY', 'TAVILY_API_KEY', 'OPENAI_API_KEY', 'GOOGLE_MAPS_API_KEY',
                'VOLCENGINE_SPEECH_API_KEY', 'VOLCENGINE_SPEECH_ACCESS_TOKEN'):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, '[redacted]')
    for secret in secrets:
        if secret:
            value = value.replace(secret, '[redacted]')
    value = remove_urls(redact_text(value, secrets))[0]
    return value[:240] or type(exc).__name__


async def _raise_with_error_body(response, secrets=(), wait=None):
    if response.is_success:
        return
    read_error = None
    try:
        await (wait(response.aread()) if wait else response.aread())
    except (httpx.HTTPError, TimeoutError) as exc:
        # A failed diagnostic read must not turn an already known 403 into a
        # retryable connection failure. Cancellation still propagates normally.
        read_error = sanitized_error(exc, secrets)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        exc.diagnostic_read_error = read_error
        raise


def _http_error_details(exc, secrets=()):
    """Preserve the provider's cause, without credentials or endpoint URLs."""
    response = exc.response
    secrets = (*secrets, *(os.getenv(name, '') for name in SECRET_ENV))
    try:
        body = response.json()
    except ValueError:
        body = response.text
    except httpx.ResponseNotRead:
        body = None
    def safe(value):
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe(item) for item in value]
        return remove_urls(redact_text(value, secrets))[0] if isinstance(value, str) else value
    body = safe(redact(body, secrets))
    provider_error = body.get('error', {}) if isinstance(body, dict) else {}
    code = provider_error.get('code') if isinstance(provider_error, dict) else None
    request_id = safe(response.headers.get('x-request-id') or response.headers.get('x-tt-logid'))
    diagnostic = f'HTTP {response.status_code}: ' + json.dumps(body, ensure_ascii=False)
    if request_id:
        diagnostic += f' (request_id={request_id})'
    details = {'raw_response': body, 'provider_request_id': request_id}
    if getattr(exc, 'diagnostic_read_error', None):
        details['diagnostic_read_error'] = safe(exc.diagnostic_read_error)
        diagnostic += '; diagnostic body unavailable: ' + details['diagnostic_read_error']
    return code[:240] if isinstance(code, str) and code else None, diagnostic, details


class _ModelDeadline(TimeoutError):
    def __init__(self, phase):
        self.phase = phase
        super().__init__(f'Model {phase} deadline exceeded')


_TRANSPORT_PHASES = frozenset({
    'connection.connect_tcp', 'connection.connect_unix_socket', 'connection.start_tls',
    'proxy.start_tls', 'http2.send_connection_init', 'http2.receive_remote_settings',
    *(f'{protocol}.{operation}' for protocol in ('http11', 'http2') for operation in (
        'send_request_headers', 'send_request_body', 'receive_response_headers',
        'receive_response_body', 'response_closed')),
})
_TRANSPORT_EVENTS = frozenset(f'{phase}.{state}' for phase in _TRANSPORT_PHASES
                              for state in ('started', 'complete', 'failed'))
_MAX_TRANSPORT_EVENTS = 64


class _TransportTrace:
    """Portable timings only: callback info may contain credentials or sockets.

    connect_tcp includes DNS; the gap before the first event can include auth
    and pool wait. A body write completion is not a peer acknowledgement.
    """
    def __init__(self, started):
        self.started = started
        self.events = []
        self.dropped_events = 0
        self.active = {}
        self.last_event = None
        self.failed_phase = None
        self.http_version = None

    async def __call__(self, name, info):
        # Never inspect/stringify info, including exceptions and response headers.
        if not isinstance(name, str) or name not in _TRANSPORT_EVENTS:
            return
        elapsed = (time.perf_counter() - self.started) * 1000
        phase, _, state = name.rpartition('.')
        self.last_event = name
        if state == 'started':
            self.active[phase] = elapsed
        else:
            self.active.pop(phase, None)
            if state == 'failed' and self.failed_phase is None:
                self.failed_phase = phase
        if len(self.events) < _MAX_TRANSPORT_EVENTS:
            self.events.append({'phase': name, 'elapsed_ms': elapsed})
        else:
            self.dropped_events += 1

    def received(self, response):
        version = response.http_version
        if version in {'HTTP/1.0', 'HTTP/1.1', 'HTTP/2', 'HTTP/3'}:
            self.http_version = version

    def snapshot(self):
        phase = next(reversed(self.active), None)
        return {'events': deepcopy(self.events), 'dropped_events': self.dropped_events,
                'last_event': self.last_event, 'failed_phase': self.failed_phase,
                'active_phase': phase,
                'active_phase_elapsed_ms': ((time.perf_counter() - self.started) * 1000 -
                                            self.active[phase]) if phase else None,
                'http_version': self.http_version}


class _CallBudget:
    """One wall-clock budget; timers exist only while awaiting the provider."""
    def __init__(self, total, first_output=None):
        self.started = time.perf_counter()
        self.total = total
        self.deadline = self.started + total
        self.first_deadline = self.started + first_output if first_output is not None else None
        self.has_output = False

    def remaining(self):
        deadline = self.deadline
        if not self.has_output and self.first_deadline is not None:
            deadline = min(deadline, self.first_deadline)
        return deadline - time.perf_counter()

    async def wait(self, awaitable):
        first = not self.has_output and self.first_deadline is not None and self.first_deadline < self.deadline
        deadline = self.first_deadline if first else self.deadline
        phase = 'first_output' if first else 'total'
        if time.perf_counter() >= deadline:
            close = getattr(awaitable, 'close', None)
            if close is not None:
                close()
            raise _ModelDeadline(phase)
        try:
            async with asyncio.timeout(max(0, deadline - time.perf_counter())):
                result = await awaitable
            if time.perf_counter() >= deadline:
                raise _ModelDeadline(phase)
            return result
        except TimeoutError as error:
            # httpx timeouts retain their concrete transport failure type.
            if isinstance(error, httpx.TimeoutException):
                raise
            raise _ModelDeadline(phase) from None


_RESPONSE_CLOSE_GRACE_SECONDS = 0.1
_RESPONSE_CLEANUPS = set()


def _cleanup_finished(task):
    _RESPONSE_CLEANUPS.discard(task)
    if not task.cancelled():
        task.exception()


@asynccontextmanager
async def _bounded_response(client, budget, *, secrets=(), **kwargs):
    context = client.stream('POST', 'chat/completions', **kwargs)
    async def close(original=None):
        cleanup = asyncio.create_task(context.__aexit__(None, None, None), name='model-response-cleanup')
        _RESPONSE_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_cleanup_finished)
        try:
            # Never let a transport's close defeat a deadline or cancellation.
            # After expiry, allow a small independent grace for socket release.
            remaining = budget.remaining()
            limit = min(_RESPONSE_CLOSE_GRACE_SECONDS, remaining) if remaining > 0 else _RESPONSE_CLOSE_GRACE_SECONDS
            done, _ = await asyncio.wait({cleanup}, timeout=limit)
            if not done:
                cleanup.cancel()
                raise TimeoutError('Model response cleanup exceeded its bounded grace')
            cleanup.result()
        except BaseException as error:
            if not cleanup.done():
                cleanup.cancel()
            if original is None:
                raise
            original.stream_close_error = sanitized_error(error, secrets() if callable(secrets) else secrets)
    try:
        response = await budget.wait(context.__aenter__())
    except BaseException as error:
        await close(error)
        raise
    try:
        yield response
    except BaseException as error:
        await close(error)
        raise
    else:
        await close()


class ArkClient:
    def _secrets(self):
        return (os.getenv(self.key_env, ''), *getattr(self, 'secrets', ()))

    def __init__(self, client=None, *, key_env='ARK_API_KEY', request_timeout=None):
        self.key_env = key_env
        self.secrets = (os.getenv(key_env, ''),)
        self.request_timeout = request_timeout
        self.client = client or httpx.AsyncClient(
            base_url=os.getenv('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3').rstrip('/') + '/',
            headers={'Authorization': f'Bearer {os.getenv(key_env, "")}'},
            timeout=httpx.Timeout(MODEL_TIMEOUT, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120),
        )

    def payload(self, messages, tools, thinker=False, stream=False, *, tool_choice=None, max_tokens=None,
                model=None, thinking=None):
        if not os.getenv(self.key_env):
            raise RuntimeError(f'{self.key_env} 未配置；没有调用模型，也不会使用假回复。')
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError('model must be a nonblank model identifier')
        if thinking is not None and type(thinking) is not bool:
            raise ValueError('thinking must be a boolean')
        use_thinking = thinker if thinking is None else thinking
        body = {'model': model if model is not None else THINKER_MODEL if thinker else TALKER_MODEL,
                'messages': messages, 'stream': stream,
                'thinking': {'type': 'enabled' if use_thinking else 'disabled'},
                'max_tokens': 6000 if thinker else 900, 'temperature': 0}
        if stream:
            body['stream_options'] = {'include_usage': True}
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto' if tool_choice is None else tool_choice
            body['parallel_tool_calls'] = True
        if max_tokens is not None:
            if type(max_tokens) is not int or max_tokens <= 0:
                raise ValueError('max_tokens must be a positive integer')
            body['max_tokens'] = max_tokens
        return body

    async def complete(self, messages, tools=None, thinker=False, *, tool_choice=None, max_tokens=None,
                       model=None, thinking=None, first_output_timeout_seconds=None, total_timeout_seconds=None):
        started = time.perf_counter()
        transport_trace = _TransportTrace(started)
        timeout = self.request_timeout if self.request_timeout is not None else 150 if thinker else MODEL_TIMEOUT
        budget = _CallBudget(total_timeout_seconds if total_timeout_seconds is not None else timeout)
        body = None
        payload = None
        try:
            payload = self.payload(messages, tools, thinker, tool_choice=tool_choice,
                                   max_tokens=max_tokens, model=model, thinking=thinking)
            async with _bounded_response(self.client, budget, secrets=self._secrets, json=payload,
                                         timeout=timeout, extensions={'trace': transport_trace}) as response:
                transport_trace.received(response)
                # Overall wait includes auth/pool/connect/HTTP. The separate
                # transport timeline records observed stages, not inference time.
                response_headers_ms = (time.perf_counter() - started) * 1000
                await _raise_with_error_body(response, self._secrets(), budget.wait)
                await budget.wait(response.aread())
                body = response.json()
            message = deepcopy(body['choices'][0]['message'])
            message['_meta'] = {'provider_id': body.get('id'), 'model': body.get('model'),
                                'created': body.get('created'), 'finish_reason': body['choices'][0].get('finish_reason'),
                                'usage': body.get('usage'), 'received_at': utcnow(),
                                'response_headers_ms': response_headers_ms,
                                'first_tool_delta_ms': None,
                                'service_tier': body.get('service_tier'),
                                'elapsed_ms': (time.perf_counter() - started) * 1000,
                                'raw_response': body, 'actual_request': deepcopy(payload),
                                'transport': transport_trace.snapshot()}
            return message
        except asyncio.CancelledError as exc:
            exc.trace_partial = {'message': None, 'meta': {'raw_response': body,
                'actual_request': deepcopy(payload), 'elapsed_ms': (time.perf_counter() - started) * 1000,
                'transport': transport_trace.snapshot()}}
            if getattr(exc, 'stream_close_error', None):
                exc.trace_partial['meta']['stream_close_error'] = exc.stream_close_error
            raise
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            error = RuntimeError(f'Model 调用失败：{sanitized_error(exc, self._secrets())}')
            error.status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            error.provider_code = 'timeout' if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else 'unavailable' if isinstance(exc, httpx.TransportError) else 'invalid_response' if not isinstance(exc, httpx.HTTPError) else None
            error.full_diagnostic = str(exc) or type(exc).__name__
            if payload is not None:
                error.trace_partial = {'message': None, 'meta': {'raw_response': body,
                    'actual_request': deepcopy(payload), 'elapsed_ms': (time.perf_counter() - started) * 1000,
                    'transport': transport_trace.snapshot()}}
                if getattr(exc, 'stream_close_error', None):
                    error.trace_partial['meta']['stream_close_error'] = exc.stream_close_error
            if isinstance(exc, httpx.HTTPStatusError):
                error.provider_code, error.full_diagnostic, details = _http_error_details(
                    exc, self._secrets())
                error.trace_partial['meta'].update(details)
            if isinstance(exc, _ModelDeadline):
                error.trace_partial['meta']['deadline_phase'] = exc.phase
            raise error from None

    async def stream(self, messages, tools=None, *, first_output_timeout_seconds=None,
                     total_timeout_seconds=None, **options):
        """Preserve every attempt while sharing first-output and total deadlines."""
        total = total_timeout_seconds if total_timeout_seconds is not None else self.request_timeout or MODEL_TIMEOUT
        budget = _CallBudget(total, first_output_timeout_seconds)
        attempts = []
        for attempt in (1, 2):
            started, started_at = time.perf_counter(), utcnow()
            offset = (started - budget.started) * 1000
            def finish(partial, status, error=None):
                meta = partial.setdefault('meta', {})
                ended = time.perf_counter()
                summary = {'attempt': attempt, 'started_at': started_at, 'ended_at': utcnow(),
                    'start_offset_ms': offset, 'end_offset_ms': (ended - budget.started) * 1000,
                    'elapsed_ms': (ended - started) * 1000, 'status': status,
                    'code': (getattr(error, 'provider_code', None) or
                             ('cancelled' if status == 'cancelled' else
                              f'http_{error.status_code}' if getattr(error, 'status_code', None) else type(error).__name__)) if error else None,
                    'status_code': getattr(error, 'status_code', None) if error else 200}
                for key in ('provider_id', 'provider_request_id', 'response_headers_ms', 'first_delta_ms',
                            'first_tool_delta_ms', 'first_chunk_ms', 'first_output_ms', 'deadline_phase', 'transport'):
                    if key in meta:
                        summary[key] = meta[key]
                if error is not None:
                    summary['diagnostic'] = sanitized_error(error, self._secrets())
                attempts.append(summary)
                for key in ('response_headers_ms', 'first_delta_ms', 'first_tool_delta_ms', 'first_chunk_ms', 'first_output_ms'):
                    if meta.get(key) is not None:
                        meta[key] += offset
                meta.update(elapsed_ms=(ended - budget.started) * 1000, attempts=deepcopy(attempts))
                return partial
            source = self._stream_once(messages, tools, _budget=budget, **options)
            try:
                async for item in source:
                    if item['type'] == 'message':
                        finish(item, 'completed')
                    yield item
                return
            except asyncio.CancelledError as exc:
                exc.trace_partial = finish(getattr(exc, 'trace_partial', {'message': None, 'meta': {}}), 'cancelled', exc)
                raise
            except RuntimeError as exc:
                exc.trace_partial = finish(getattr(exc, 'trace_partial', {'message': None, 'meta': {}}), 'failed', exc)
                # Only quick, output-free failures may consume a second attempt.
                # Slow timeouts never silently add another full provider wait.
                quick = time.perf_counter() - started <= min(2, total / 4)
                room = budget.remaining() >= min(1, total / 4)
                if budget.has_output or attempt == 2 or not getattr(exc, 'retryable', False) or not quick or not room:
                    raise
            finally:
                await source.aclose()

    async def _stream_once(self, messages, tools=None, *, _budget, **options):
        started = time.perf_counter()
        transport_trace = _TransportTrace(started)
        content, reasoning, calls, raw_chunks, extra = '', '', {}, [], {}
        meta = {'provider_id': None, 'model': None, 'created': None, 'finish_reason': None,
                'usage': None, 'service_tier': None, 'response_headers_ms': None,
                'first_delta_ms': None, 'first_tool_delta_ms': None,
                'first_chunk_ms': None, 'first_output_ms': None, 'done_received': False, 'actual_request': None}
        def observed():
            message = {'role': 'assistant', 'content': content or None, **extra}
            if reasoning:
                message['reasoning_content'] = reasoning
            if calls:
                message['tool_calls'] = [deepcopy(calls[i]) for i in sorted(calls)]
            return {'message': message, 'meta': {**meta, 'received_at': utcnow(),
                    'elapsed_ms': (time.perf_counter() - started) * 1000, 'raw_chunks': deepcopy(raw_chunks),
                    'transport': transport_trace.snapshot()}}
        try:
            payload = self.payload(messages, tools, stream=True, **options)
            meta['actual_request'] = deepcopy(payload)
            async with _bounded_response(self.client, _budget, secrets=self._secrets, json=payload,
                                         extensions={'trace': transport_trace}) as response:
                transport_trace.received(response)
                meta['response_headers_ms'] = (time.perf_counter() - started) * 1000
                meta['provider_request_id'] = remove_urls(redact_text(
                    response.headers.get('x-request-id') or response.headers.get('x-tt-logid') or '',
                    self._secrets()))[0]
                await _raise_with_error_body(response, self._secrets(), _budget.wait)
                lines = response.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await _budget.wait(anext(lines))
                    except StopAsyncIteration:
                        break
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        meta['done_received'] = True
                        # Reaching the SSE terminal marker does not consume the
                        # HTTP/1.1 EndOfMessage. Drain to EOF before returning so
                        # httpcore can put this connection back into its pool.
                        continue
                    if meta['done_received']:
                        continue
                    try:
                        item = json.loads(data)
                    except ValueError:
                        raw_chunks.append({'unparsed_data': data})
                        raise
                    raw_chunks.append(item)
                    if isinstance(item, dict) and isinstance(item.get('error'), dict):
                        error = RuntimeError('Model provider returned a structured stream error')
                        error.provider_code = 'upstream_error'
                        error.full_diagnostic = json.dumps(item['error'], ensure_ascii=False)
                        error.trace_partial = observed()
                        raise error
                    if meta['first_chunk_ms'] is None:
                        meta['first_chunk_ms'] = (time.perf_counter() - started) * 1000
                    for source, target in (('id','provider_id'), ('model','model'), ('created','created'),
                                           ('usage','usage'), ('service_tier','service_tier')):
                        if item.get(source) is not None:
                            meta[target] = item[source]
                    choices = item.get('choices', [])
                    if not choices:
                        continue
                    delta = choices[0].get('delta', {})
                    tool_output = any(part.get('id') or part.get('function', {}).get('name') or
                                      part.get('function', {}).get('arguments') for part in delta.get('tool_calls', []))
                    if delta.get('content') or tool_output:
                        _budget.has_output = True
                        if meta['first_output_ms'] is None:
                            meta['first_output_ms'] = (time.perf_counter() - started) * 1000
                    if choices[0].get('finish_reason') is not None:
                        meta['finish_reason'] = choices[0]['finish_reason']
                    if delta.get('content'):
                        if meta['first_delta_ms'] is None:
                            meta['first_delta_ms'] = (time.perf_counter() - started) * 1000
                        content += delta['content']
                        yield {'type': 'delta', 'text': delta['content']}
                    if delta.get('reasoning_content'):
                        reasoning += delta['reasoning_content']
                    if tool_output and meta['first_tool_delta_ms'] is None:
                        meta['first_tool_delta_ms'] = (time.perf_counter() - started) * 1000
                    for key, value in delta.items():
                        if key not in {'role', 'content', 'reasoning_content', 'tool_calls'}:
                            extra[key] = extra.get(key, '') + value if isinstance(value, str) else value
                    for part in delta.get('tool_calls', []):
                        index = part.get('index', 0)
                        call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        if part.get('id'):
                            call['id'] = part['id']
                        fn = part.get('function', {})
                        call['function']['name'] += fn.get('name', '')
                        call['function']['arguments'] += fn.get('arguments', '')
                        if 'extra_content' in part:
                            if not isinstance(part['extra_content'], dict):
                                raise ValueError('Provider tool metadata must be an object')
                            _merge_tool_metadata(call.setdefault('extra_content', {}), part['extra_content'])
            if not meta['done_received'] and meta['finish_reason'] is None:
                raise ValueError('Model stream ended without a terminal marker or finish reason')
            yield {'type': 'message', **observed()}
        except asyncio.CancelledError as exc:
            exc.trace_partial = observed()
            if getattr(exc, 'stream_close_error', None):
                exc.trace_partial['meta']['stream_close_error'] = exc.stream_close_error
            raise
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            error = RuntimeError(f'Model 流式调用失败：{sanitized_error(exc, self._secrets())}')
            error.status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            error.provider_code = 'timeout' if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else 'unavailable' if isinstance(exc, httpx.TransportError) else 'invalid_response' if not isinstance(exc, httpx.HTTPError) else None
            error.full_diagnostic = str(exc) or type(exc).__name__
            error.trace_partial = observed()
            if getattr(exc, 'stream_close_error', None):
                error.trace_partial['meta']['stream_close_error'] = exc.stream_close_error
            if isinstance(exc, httpx.HTTPStatusError):
                error.provider_code, error.full_diagnostic, details = _http_error_details(
                    exc, self._secrets())
                error.trace_partial['meta'].update(details)
            if isinstance(exc, _ModelDeadline):
                error.trace_partial['meta']['deadline_phase'] = exc.phase
            error.retryable = not raw_chunks and not isinstance(exc, (httpx.TimeoutException, TimeoutError)) and (
                isinstance(exc, httpx.TransportError) or
                isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {502, 503, 504})
            raise error from None

    async def prewarm(self):
        # Establish TLS and an inference connection once. Failure is observable, never fatal to local UI.
        try:
            await self.complete([{'role': 'user', 'content': render_prompt('prewarm')}])
            return {'ok': True, 'model': TALKER_MODEL, 'at': utcnow()}
        except RuntimeError as exc:
            return {'ok': False, 'model': TALKER_MODEL, 'error': str(exc), 'at': utcnow()}

    async def aclose(self):
        await self.client.aclose()


class TavilyClient:
    def __init__(self, client=None, *, key_env='TAVILY_API_KEY', timeout_seconds=35):
        self.key_env = key_env
        self.client = client or httpx.AsyncClient(base_url='https://api.tavily.com/', timeout=timeout_seconds)

    async def _request(self, endpoint, body):
        key = os.getenv(self.key_env)
        if not key:
            raise RuntimeError('TAVILY_API_KEY 未配置，无法查询实时信息。')
        at = utcnow()
        try:
            response = await self.client.post(endpoint, headers={'Authorization': f'Bearer {key}'}, json=body)
            response.raise_for_status()
            data = response.json()
            data['queried_at'] = at
            return data
        except (httpx.HTTPError, ValueError) as exc:
            error = RuntimeError(f'Tavily {endpoint} 失败：{sanitized_error(exc, (key,))}')
            error.status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            error.provider_code = 'timeout' if isinstance(exc, httpx.TimeoutException) else 'unavailable' if isinstance(exc, httpx.TransportError) else None
            error.full_diagnostic = str(exc).replace(key, '[redacted]')
            raise error from None

    async def search(self, query, max_results=5, *, include_domains=None):
        body = {'query': query, 'max_results': max_results, 'search_depth': 'basic',
                'include_answer': False, 'include_raw_content': False}
        if include_domains is None:
            return await self._request('search', body)
        invalid = 'include_domains 必须是1至10个有效域名组成的列表，不含协议、路径、端口或空元素。'
        if not isinstance(include_domains, list) or not 1 <= len(include_domains) <= 10:
            raise ValueError(invalid)
        domains = []
        for domain in include_domains:
            if not is_domain(domain):
                raise ValueError(invalid)
            domains.append(domain.lower())
        body.update(include_domains=domains, include_domains_mode='restrict')
        result = await self._request('search', body)
        # Records the actual request restriction, not a claim that each
        # returned page was semantically verified or satisfies the goal.
        result['request_filters'] = {'include_domains': domains, 'include_domains_mode': 'restrict'}
        return result

    async def extract(self, urls):
        return await self._request('extract', {'urls': urls, 'extract_depth': 'basic', 'format': 'text'})

    async def aclose(self):
        await self.client.aclose()
