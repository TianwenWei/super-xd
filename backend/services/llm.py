"""Role routing and normalized model results; no vendor options enter the actor."""
from copy import deepcopy
from contextlib import aclosing
import asyncio
import os

import httpx

from backend.models import ArkClient, utcnow
from backend.prompt_templates import render_prompt
from backend.settings import TALKER_MODEL, THINKER_MODEL, VEHICLE_CONTROL_MODEL
from .config import api_key, google_vertex_url, resolved, validate_url, validate_vertex_model
from .contracts import LLMRequest, LLMResult, ProviderCapabilities, ProviderError, ProviderStatus, redact


class CompatibleTransport(ArkClient):
    """Reuse the tested SSE assembler, with an explicitly compatible request body."""
    def __init__(self, profile, client=None):
        self.profile = profile
        self.key_env = profile.get('key_env', '')
        self.request_timeout = profile.get('timeout_seconds', 45)
        self.key = api_key(profile)
        self.secrets = (self.key,)
        self.base_url = validate_url(resolved(profile, 'base_url'))
        self.client = client or httpx.AsyncClient(
            base_url=(self.base_url or 'http://127.0.0.1/') .rstrip('/') + '/',
            headers={'Authorization': 'Bearer ' + self.key} if self.key else {},
            timeout=httpx.Timeout(profile.get('timeout_seconds', 45), connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120))

    def payload(self, messages, tools, thinker=False, stream=False, *, tool_choice=None,
                max_tokens=None, model=None, thinking=None):
        if not self.base_url or self.profile.get('require_api_key', True) and not self.key:
            raise ProviderError('not_configured', 'Compatible LLM endpoint/credential is missing', provider='openai_compatible')
        body = {'model': model, 'messages': messages, 'stream': stream,
                'max_tokens': max_tokens or 900, 'temperature': 0}
        if not model:
            raise ProviderError('not_configured', 'Compatible LLM model is missing', provider='openai_compatible')
        if stream:
            body['stream_options'] = {'include_usage': True}
        if tools:
            if not self.profile.get('supports_tools', True):
                raise ProviderError('unsupported_capability', 'Model profile does not support native tools', provider='openai_compatible')
            body.update(tools=tools, tool_choice=tool_choice or 'auto', parallel_tool_calls=True)
        if tool_choice is not None and not self.profile.get('supports_tool_choice', True):
            raise ProviderError('unsupported_capability', 'Model profile does not support tool choice', provider='openai_compatible')
        reasoning_field = self.profile.get('reasoning_field')
        if reasoning_field:
            body[reasoning_field] = {'type': 'enabled' if thinking else 'disabled'}
        return body


class GoogleVertexTransport(ArkClient):
    """Vertex's OpenAI endpoint, with request-scoped ADC inside Ark's budget."""
    def __init__(self, profile, *, auth=None, transport=None):
        from .google_auth import GoogleADCAuth
        self.profile = deepcopy(profile)
        self.key_env = ''
        self.request_timeout = profile.get('timeout_seconds', 45)
        self.base_url = google_vertex_url(profile)
        self.auth = auth or GoogleADCAuth(resolved(profile, 'project'))
        self.client = httpx.AsyncClient(
            base_url=(self.base_url or 'http://127.0.0.1').rstrip('/') + '/',
            auth=self.auth, transport=transport,
            timeout=httpx.Timeout(self.request_timeout, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120))

    @property
    def secrets(self):
        return self.auth.secrets

    def payload(self, messages, tools, thinker=False, stream=False, *, tool_choice=None,
                max_tokens=None, model=None, thinking=None):
        if not self.base_url:
            raise ProviderError('not_configured', 'Google Vertex project is missing', provider='google_vertex')
        try:
            validate_vertex_model(self.profile, model)
        except ValueError as error:
            raise ProviderError('invalid_request', str(error), provider='google_vertex') from None
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ProviderError('invalid_request', 'max_tokens must be a positive integer', provider='google_vertex')
        body = {'model': model, 'messages': messages, 'stream': stream,
                'max_tokens': max_tokens if max_tokens is not None else 900}
        # Gemini 3.8 has no thinking-off mode; the profile explicitly selects its
        # tier independently of the legacy Ark role boolean.
        if 'reasoning_effort' in self.profile:
            body['reasoning_effort'] = self.profile['reasoning_effort']
        if stream:
            body['stream_options'] = {'include_usage': True}
        if tools:
            if not self.profile.get('supports_tools', True):
                raise ProviderError('unsupported_capability', 'Model profile does not support native tools', provider='google_vertex')
            body.update(tools=tools, tool_choice='auto' if tool_choice is None else tool_choice, parallel_tool_calls=True)
        if tool_choice is not None and not self.profile.get('supports_tool_choice', True):
            raise ProviderError('unsupported_capability', 'Model profile does not support tool choice', provider='google_vertex')
        return body


class ChatProvider:
    def __init__(self, profile, *, transport=None):
        self.profile = deepcopy(profile)
        self._configured_key = api_key(profile)
        self.provider = profile['provider']
        if transport is not None:
            self.transport = transport
        elif self.provider == 'ark':
            base_url = validate_url(resolved(profile, 'base_url', 'https://ark.cn-beijing.volces.com/api/v3'))
            key = api_key(profile)
            client = httpx.AsyncClient(base_url=base_url.rstrip('/') + '/',
                headers={'Authorization': 'Bearer ' + key}, timeout=httpx.Timeout(profile.get('timeout_seconds', 45), connect=10),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120))
            self.transport = ArkClient(client, key_env=profile.get('key_env', 'ARK_API_KEY'),
                                       request_timeout=profile.get('timeout_seconds', 45))
        elif self.provider == 'google_vertex':
            self.transport = GoogleVertexTransport(profile)
        elif self.provider == 'openai_compatible':
            self.transport = CompatibleTransport(profile)
        else:
            raise ValueError('Unknown LLM provider')

    @property
    def secrets(self):
        # HTTP clients retain their initial Bearer even if the process environment
        # changes; ADC transports additionally expose newly refreshed tokens.
        return (self._configured_key, api_key(self.profile), *getattr(self.transport, 'secrets', ()))

    def info(self):
        if self.provider == 'google_vertex':
            return ProviderStatus(self.provider, bool(google_vertex_url(self.profile)), ProviderCapabilities(
                streaming=True, tools=self.profile.get('supports_tools', True),
                tool_choice=self.profile.get('supports_tool_choice', True)),
                details={'authentication': 'application_default_credentials', 'credentials_probed': False,
                         'project': resolved(self.profile, 'project'), 'location': self.profile['location'],
                         'reasoning_effort': self.profile.get('reasoning_effort')}).to_dict()
        configured = bool(api_key(self.profile)) if self.provider == 'ark' else bool(resolved(self.profile, 'base_url')) and (
            not self.profile.get('require_api_key', True) or bool(api_key(self.profile)))
        return ProviderStatus(self.provider, configured, ProviderCapabilities(
            streaming=True, tools=self.profile.get('supports_tools', True),
            tool_choice=self.profile.get('supports_tool_choice', True))).to_dict()

    def _options(self, request):
        options = {'model': request.model, 'thinking': request.reasoning, 'max_tokens': request.max_tokens,
                   'tool_choice': request.tool_choice}
        for name in ('first_output_timeout_seconds', 'total_timeout_seconds'):
            if getattr(request, name, None) is not None:
                options[name] = getattr(request, name)
        return options

    def _error(self, error):
        status = getattr(error, 'status_code', None)
        code = getattr(error, 'provider_code', None) or {401: 'auth', 403: 'permission_denied', 429: 'rate_limit'}.get(status, 'upstream_error')
        diagnostic = redact(getattr(error, 'full_diagnostic', str(error)), self.secrets)
        result = ProviderError(code, diagnostic[:500], provider=self.provider,
            retryable=getattr(error, 'retryable', False), details={'status_code': status, 'diagnostic': diagnostic})
        if hasattr(error, 'trace_partial'):
            result.trace_partial = redact(error.trace_partial, self.secrets)
        return result

    async def complete(self, request: LLMRequest):
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'LLM endpoint or credential is missing', provider=self.provider)
        try:
            message = await self.transport.complete(request.messages, request.tools,
                thinker=request.role == 'thinker', **self._options(request))
        except asyncio.CancelledError as error:
            if hasattr(error, 'trace_partial'):
                error.trace_partial = redact(error.trace_partial, self.secrets)
            raise
        except ProviderError:
            raise
        except RuntimeError as error:
            raise self._error(error) from None
        message = deepcopy(message)
        meta = message.pop('_meta', {})
        meta['provider'] = self.provider
        return LLMResult(redact(message, self.secrets), redact(meta, self.secrets))

    async def stream(self, request: LLMRequest):
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'LLM endpoint or credential is missing', provider=self.provider)
        try:
            async with aclosing(self.transport.stream(request.messages, request.tools, **self._options(request))) as source:
                async for item in source:
                    item = redact(deepcopy(item), self.secrets)
                    if item.get('type') == 'message':
                        item.setdefault('meta', {})['provider'] = self.provider
                    yield item
        except asyncio.CancelledError as error:
            if hasattr(error, 'trace_partial'):
                error.trace_partial = redact(error.trace_partial, self.secrets)
            raise
        except ProviderError:
            raise
        except RuntimeError as error:
            raise self._error(error) from None

    async def aclose(self):
        await self.transport.aclose()


class LLMService:
    """Compatibility facade lets injected test clients keep the existing seam."""
    def __init__(self, config, *, providers=None, role='talker', owner=True):
        self.config, self.role_name, self.owner = config, role, owner
        self.providers = providers if providers is not None else {
            name: ChatProvider(profile) for name, profile in config.document['llm']['profiles'].items()}
        for name in ('talker', 'planner', 'thinker', 'finalizer'):
            setting = config.role(name)
            capability = self.providers[setting['profile']].info()['capabilities']
            if not capability['tools'] or not capability['tool_choice']:
                raise ValueError(f'{name} requires native tools and tool_choice support')

    def for_role(self, role):
        return LLMService(self.config, providers=self.providers, role=role, owner=False)

    def request(self, messages, tools=None, thinker=False, *, role=None, model=None, thinking=None,
                max_tokens=None, tool_choice=None):
        role = role or ('thinker' if thinker else self.role_name)
        setting = self.config.role(role)
        defaults = {'talker': TALKER_MODEL, 'planner': VEHICLE_CONTROL_MODEL,
                    'thinker': THINKER_MODEL, 'finalizer': TALKER_MODEL}
        # A role's explicit configuration wins over the historical model hint.
        chosen_model = resolved(setting, 'model', model or defaults[role])
        # Role policy is a ceiling; bounded finalization may lower it for a call.
        token_budget = min(setting['max_tokens'], max_tokens) if max_tokens is not None else setting['max_tokens']
        return LLMRequest(messages, tools, role, chosen_model,
            setting['reasoning'] and thinking is not False, token_budget, tool_choice,
            first_output_timeout_seconds=setting.get('first_output_timeout_seconds'),
            total_timeout_seconds=setting.get('total_timeout_seconds'))

    async def complete(self, messages, tools=None, thinker=False, **options):
        request = self.request(messages, tools, thinker, **options)
        result = await self.providers[self.config.role(request.role)['profile']].complete(request)
        return {**result.message, '_meta': result.metadata}

    async def stream(self, messages, tools=None, **options):
        request = self.request(messages, tools, **options)
        async with aclosing(self.providers[self.config.role(request.role)['profile']].stream(request)) as source:
            async for item in source:
                yield item

    async def prewarm(self):
        request = self.request([{'role': 'user', 'content': render_prompt('prewarm')}])
        try:
            await self.providers[self.config.role(request.role)['profile']].complete(request)
            return {'ok': True, 'model': request.model, 'at': utcnow()}
        except RuntimeError as error:
            return {'ok': False, 'model': request.model, 'error': str(error), 'at': utcnow()}

    def info(self):
        roles = {}
        for name in ('talker', 'planner', 'thinker', 'finalizer'):
            setting = self.config.role(name)
            request = self.request([], role=name)
            profile = self.config.document['llm']['profiles'][setting['profile']]
            roles[name] = {'provider': self.providers[setting['profile']].info(), 'model': request.model,
                'first_output_timeout_seconds': request.first_output_timeout_seconds,
                'total_timeout_seconds': (request.total_timeout_seconds if request.total_timeout_seconds is not None
                                          else profile.get('timeout_seconds', 45))}
        return {'roles': roles, 'deployment_region': getattr(self.config, 'region', '') or 'explicit'}

    async def aclose(self):
        if self.owner:
            for provider in self.providers.values():
                await provider.aclose()
