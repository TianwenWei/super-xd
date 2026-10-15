"""Small OpenAI-compatible Ark transport; secrets stay in this process."""
import asyncio
import json
import os
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
import httpx
from backend.prompt_templates import render_prompt
from backend.settings import TALKER_MODEL, THINKER_MODEL, MODEL_TIMEOUT


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def sanitized_error(exc):
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
    for key in ('ARK_API_KEY', 'TAVILY_API_KEY', 'OPENAI_API_KEY', 'GOOGLE_MAPS_API_KEY',
                'VOLCENGINE_SPEECH_API_KEY', 'VOLCENGINE_SPEECH_ACCESS_TOKEN'):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, '[redacted]')
    return value[:240] or type(exc).__name__


class ArkClient:
    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(
            base_url=os.getenv('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3').rstrip('/') + '/',
            headers={'Authorization': f'Bearer {os.getenv("ARK_API_KEY", "")}'},
            timeout=httpx.Timeout(MODEL_TIMEOUT, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120),
        )

    def payload(self, messages, tools, thinker=False, stream=False, *, tool_choice=None, max_tokens=None,
                model=None, thinking=None):
        if not os.getenv('ARK_API_KEY'):
            raise RuntimeError('ARK_API_KEY 未配置；没有调用模型，也不会使用假回复。')
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
                       model=None, thinking=None):
        started = time.perf_counter()
        try:
            async with self.client.stream('POST', 'chat/completions', json=self.payload(messages, tools, thinker,
                                          tool_choice=tool_choice, max_tokens=max_tokens, model=model, thinking=thinking),
                                          timeout=150 if thinker else MODEL_TIMEOUT) as response:
                # Includes pool/connect/HTTP wait; this does not isolate server
                # inference or TCP/TLS timing, which we have not observed here.
                response_headers_ms = (time.perf_counter() - started) * 1000
                await response.aread()
                response.raise_for_status()
                body = response.json()
            message = deepcopy(body['choices'][0]['message'])
            message['_meta'] = {'provider_id': body.get('id'), 'model': body.get('model'),
                                'created': body.get('created'), 'finish_reason': body['choices'][0].get('finish_reason'),
                                'usage': body.get('usage'), 'received_at': utcnow(),
                                'response_headers_ms': response_headers_ms,
                                'first_tool_delta_ms': None,
                                'service_tier': body.get('service_tier'),
                                'elapsed_ms': (time.perf_counter() - started) * 1000,
                                'raw_response': body}
            return message
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RuntimeError(f'Ark 调用失败：{sanitized_error(exc)}') from None

    async def stream(self, messages, tools=None):
        started = time.perf_counter()
        content, reasoning, calls, raw_chunks, extra = '', '', {}, [], {}
        meta = {'provider_id': None, 'model': None, 'created': None, 'finish_reason': None,
                'usage': None, 'service_tier': None, 'response_headers_ms': None,
                'first_delta_ms': None, 'first_tool_delta_ms': None,
                'first_chunk_ms': None, 'done_received': False}
        def observed():
            message = {'role': 'assistant', 'content': content or None, **extra}
            if reasoning:
                message['reasoning_content'] = reasoning
            if calls:
                message['tool_calls'] = [deepcopy(calls[i]) for i in sorted(calls)]
            return {'message': message, 'meta': {**meta, 'received_at': utcnow(),
                    'elapsed_ms': (time.perf_counter() - started) * 1000, 'raw_chunks': deepcopy(raw_chunks)}}
        try:
            async with self.client.stream('POST', 'chat/completions', json=self.payload(messages, tools, stream=True)) as response:
                meta['response_headers_ms'] = (time.perf_counter() - started) * 1000
                response.raise_for_status()
                async for line in response.aiter_lines():
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
                    if choices[0].get('finish_reason') is not None:
                        meta['finish_reason'] = choices[0]['finish_reason']
                    if delta.get('content'):
                        if meta['first_delta_ms'] is None:
                            meta['first_delta_ms'] = (time.perf_counter() - started) * 1000
                        content += delta['content']
                        yield {'type': 'delta', 'text': delta['content']}
                    if delta.get('reasoning_content'):
                        reasoning += delta['reasoning_content']
                    if delta.get('tool_calls') and meta['first_tool_delta_ms'] is None:
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
            yield {'type': 'message', **observed()}
        except asyncio.CancelledError as exc:
            exc.trace_partial = observed()
            raise
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            error = RuntimeError(f'Ark 流式调用失败：{sanitized_error(exc)}')
            error.trace_partial = observed()
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
    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(base_url='https://api.tavily.com/', timeout=35)

    async def _request(self, endpoint, body):
        key = os.getenv('TAVILY_API_KEY')
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
            raise RuntimeError(f'Tavily {endpoint} 失败：{sanitized_error(exc)}') from None

    async def search(self, query, max_results=5, *, include_domains=None):
        body = {'query': query, 'max_results': max_results, 'search_depth': 'basic',
                'include_answer': False, 'include_raw_content': False}
        if include_domains is None:
            return await self._request('search', body)
        invalid = 'include_domains 必须是1至10个有效域名组成的列表，不含协议、路径、端口或空元素。'
        if not isinstance(include_domains, list) or not 1 <= len(include_domains) <= 10:
            raise ValueError(invalid)
        label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
        domains = []
        for domain in include_domains:
            if (not isinstance(domain, str) or len(domain) > 253 or
                    not re.fullmatch(rf'{label}(?:\.{label})+', domain.lower()) or
                    domain.rsplit('.', 1)[-1].isdigit()):
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
