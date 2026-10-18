"""Local provider ports and factual capability declarations for NLPService."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from super_xd.nlp.catalog.vehicle import HOT_TOOL_NAMES, TOOLS
from super_xd.nlp.contracts.media import media_item
from super_xd.nlp.contracts.capabilities import ToolCapability
from super_xd.nlp.contracts.gateway_v1 import ToolDispatch
from super_xd.nlp.catalog.actions import MEDIA_DEFINITIONS, NAVIGATION_DEFINITIONS


def freeze_local_model(model):
    """Use production selector freezing while sharing the already-open clients."""
    from super_xd.nlp.adapters.models.service import ModelService
    if isinstance(model, ModelService):
        from super_xd.nlp.assembly import FrozenNLPConfig
        return ModelService(FrozenNLPConfig(model.config), providers=model.providers,
                          owner=False)
    return model


def local_task_budget(**overrides):
    """Registration and the independent worker use the same bounded settings."""
    from super_xd.nlp.assembly import TaskBudget
    values = asdict(TaskBudget.from_environment())
    values.update({key: value for key, value in overrides.items() if value is not None})
    values['max_steps'] = int(max(1, min(20, values['max_steps'])))
    values['timeout_seconds'] = float(max(.01, min(60, values['timeout_seconds'])))
    values['tool_timeout_seconds'] = float(max(.01, values['tool_timeout_seconds']))
    values['quick_max_steps'] = int(max(1, min(4, values['max_steps'], values['quick_max_steps'])))
    values['quick_timeout_seconds'] = float(max(.01, min(60, values['quick_timeout_seconds'])))
    return TaskBudget(**values)


def local_model_policy_id(model, task_budget, *, foreground_wait_seconds=None):
    """Fingerprint the frozen deployment; injected models obey the same gate."""
    from super_xd.nlp.adapters.models.service import ModelService
    from super_xd.nlp.assembly import model_policy_id
    from super_xd.nlp.settings import NLPSettings
    if foreground_wait_seconds is None:
        foreground_wait_seconds = NLPSettings.from_environment().foreground_wait_seconds
    if isinstance(model, ModelService):
        return model_policy_id(model, model.config, task_budget, foreground_wait_seconds=foreground_wait_seconds)
    info = model.info() if callable(getattr(model, 'info', None)) else {'client_type': type(model).__qualname__}
    payload = {'model': info, 'task_budget': asdict(task_budget), 'foreground_wait_seconds': foreground_wait_seconds}
    return 'local-model-' + hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def provider_info(client, method=None):
    domain = {'get_weather': 'weather', 'search_places': 'places'}.get(method)
    if domain and getattr(client, domain, None) is not None:
        return provider_info(getattr(client, domain))
    info = getattr(client, 'info', None)
    if callable(info):
        return info()
    return {'configured': callable(getattr(client, method, None)) if method else client is not None,
            'execution_location': 'cloud'}


class LocalResources:
    def __init__(self, *, maps, web, manual, cockpit=None):
        self.maps, self.web, self.manual, self.cockpit = maps, web, manual, cockpit

    def declarations(self):
        rows, bindings = [], {}

        def declare(name, operation, location, domain, *, action=None, content_kind=None, available=True):
            row = ToolCapability(name=name, operation=operation, execution_location=location, domain=domain,
                                 action=action, content_kind=content_kind, available=available)
            rows.append(row)
            suffix = content_kind if name == 'search_media' else action
            bindings[name + ('.' + suffix if suffix else '')] = ToolDispatch(
                kind='tool_execution', operation=operation, execution_location=location, domain=domain)

        for name, tool in TOOLS.items():
            operation = 'query' if tool.state_mapping.get('operation') == 'read' else 'control'
            declare(name, operation, 'device', tool.skill_id)
        for name, client, domain in [('get_weather', self.maps, 'weather'), ('web_search', self.web, 'web'),
                                     ('search_manual', self.manual, 'manual'), ('search_places', self.maps, 'places')]:
            info = provider_info(client, name)
            declare(name, 'query', info.get('execution_location', 'cloud') if name == 'search_places' else 'cloud',
                    domain, available=info.get('configured') is not False)
        media = self.cockpit.media if self.cockpit else None
        for kind in ('music', 'video', 'radio'):
            info = media.provider_info(kind) if hasattr(media, 'provider_info') else provider_info(media)
            declare('search_media', 'query', info.get('execution_location', 'cloud'), 'media',
                    content_kind=kind, available=media is not None and info.get('configured') is not False)
        for action in MEDIA_DEFINITIONS:
            declare('control_media', action.operation, action.execution_location, 'media',
                    action=action.name, available=self.cockpit is not None)
        for action in NAVIGATION_DEFINITIONS:
            available = self.cockpit is not None and (action.name != 'route' or
                provider_info(self.cockpit.routes).get('configured') is not False)
            declare('manage_navigation', action.operation, action.execution_location,
                    'navigation', action=action.name, available=available)
        declare('start_poi_navigation', 'control', 'device', 'navigation', available=self.cockpit is not None)
        return rows, {key: value for key, value in bindings.items()
                      if key in HOT_TOOL_NAMES or key not in TOOLS}

    async def query(self, item):
        name, dispatch = item.request.name, item.dispatch
        arguments = deepcopy(dispatch.execution_arguments if dispatch.execution_arguments is not None else item.request.arguments)
        if name == 'search_media':
            return await self.cockpit.media.search(**arguments)
        if name == 'manage_navigation':
            if arguments.get('action') == 'get_status':
                raise ValueError('Navigation status is read from the local device snapshot')
            arguments.pop('action', None)
            return await self.cockpit.route(**arguments)
        client = self.manual if name == 'search_manual' else self.web if name == 'web_search' else self.maps
        return await getattr(client, name)(**arguments)

    async def resolve_command(self, item):
        """Resolve playback capabilities without treating preparation as execution."""
        command = deepcopy(item.dispatch.prepared_command)
        if command is None:
            return None
        command['expected_state_version'] = item.dispatch.prepared_command['expected_state_version']
        if command['domain'] == 'media' and command.get('requires_content_resolution'):
            identifier = command['arguments']['content_id']
            media = self.cockpit.media
            info = media.content_provider_info(identifier) if hasattr(media, 'content_provider_info') else media.info()
            if info.get('execution_location') == 'device':
                if hasattr(media, 'device_content_id'):
                    command['arguments']['device_content_id'] = media.device_content_id(identifier)
            else:
                raw = await media.resolve(identifier)
                resolved = media_item(raw, playable=True)
                if resolved['content_id'] != identifier or resolved['kind'] != command['arguments']['kind']:
                    raise ValueError('Media provider changed the content identity or kind')
                command['arguments']['source_url'] = resolved['source_url']
                command['provider_trace'] = deepcopy(raw.get('_provider_trace'))
        return command
