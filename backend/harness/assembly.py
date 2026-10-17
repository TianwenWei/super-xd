"""Production NLP composition: no demo actor, SQLite, audio or device clients."""
from __future__ import annotations

from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json

from backend.catalog import HOT_TOOL_NAMES
from backend.nlp_settings import NLPSettings
from backend.prompt_config import validate_all
from backend.services.config import api_key, load_services_config, resolved
from backend.services.factory import create_llm, create_maps, create_search
from backend.services.reads import verify_forecast_record
from backend.settings import TALKER_MODEL, THINKER_MODEL, VEHICLE_CONTROL_MODEL
from .background import TaskService
from .diagnostics import GatewayDiagnostics
from .gateway import NLPService
from .gateway_contracts import ToolDispatch
from .observability import MeasuredLLM, Readiness, UsageMetrics
from .product_policy import MEDIA_ACTIONS, NAVIGATION_ACTIONS, ProductTalkerPolicy
from .task_runner import TaskRunner
from .task_transport import GatewayTaskStore
from .vehicle_planner import VehicleControlPlanner


def product_bindings(config):
    """Deployment-owned routes; incoming capabilities can narrow, never expand."""
    def route(operation, location, domain):
        return ToolDispatch(kind='tool_execution', operation=operation,
                            execution_location=location, domain=domain)
    bindings = {name: route('control', 'device', 'vehicle') for name in HOT_TOOL_NAMES}
    bindings.update({name: route('query', 'cloud', domain) for name, domain in
                     [('get_weather', 'weather'), ('web_search', 'web'), ('search_manual', 'manual')]})
    bindings['search_places'] = route('query', 'device' if config.profile('places')['provider'] == 'device_gateway' else 'cloud', 'places')
    bindings['start_poi_navigation'] = route('control', 'device', 'navigation')
    for kind, profile in config.media_profiles().items():
        bindings['search_media.' + kind] = route('query', 'device' if profile['provider'] == 'device_catalog' else 'cloud', 'media')
    for action in MEDIA_ACTIONS:
        bindings['control_media.' + action] = route('query' if action == 'get_status' else 'control', 'device', 'media')
    for action in NAVIGATION_ACTIONS:
        query = action in {'get_status', 'route'}
        bindings['manage_navigation.' + action] = route('query' if query else 'control', 'cloud' if query else 'device', 'navigation')
    return bindings


class FrozenNLPConfig:
    """Resolve deployment selectors once; inference cannot reread model choices."""

    def __init__(self, config):
        self.document, self.source = deepcopy(config.document), config.source
        self._profiles = {domain: self._resolved(config.profile(domain))
                          for domain in ('web_search', 'web_content', 'weather', 'places')}
        self._media = {kind: self._resolved(profile) for kind, profile in config.media_profiles().items()}
        defaults = {'talker': TALKER_MODEL, 'thinker': THINKER_MODEL,
                    'planner': VEHICLE_CONTROL_MODEL, 'finalizer': TALKER_MODEL}
        for name, role in self.document['llm']['roles'].items():
            role['model'] = resolved(role, 'model', defaults[name])
            role.pop('model_env', None)
        for name, profile in self.document['llm']['profiles'].items():
            self.document['llm']['profiles'][name] = self._resolved(profile,
                default_url='https://ark.cn-beijing.volces.com/api/v3' if profile['provider'] == 'ark' else '')

    @staticmethod
    def _resolved(profile, *, default_url=''):
        profile = deepcopy(profile)
        profile['base_url'] = resolved(profile, 'base_url', default_url)
        profile.pop('base_url_env', None)
        return profile

    def role(self, name):
        return deepcopy(self.document['llm']['roles'][name])

    def profile(self, domain):
        return deepcopy(self._profiles[domain])

    def media_profiles(self):
        return deepcopy(self._media)


@dataclass(frozen=True)
class TaskBudget:
    max_steps: int
    timeout_seconds: float
    tool_timeout_seconds: float
    quick_max_steps: int
    quick_timeout_seconds: float = 60.0

    @classmethod
    def from_settings(cls, settings: NLPSettings):
        steps = min(20, settings.thinker_max_steps)
        return cls(steps, max(0.01, min(60.0, settings.thinker_timeout_seconds)),
                   max(0.01, settings.tool_timeout_seconds), min(4, steps))

    @classmethod
    def from_environment(cls):
        return cls.from_settings(NLPSettings.from_environment())


def model_policy_id(llm, config, task_budget=None):
    roles = {}
    for name in ('talker', 'planner', 'thinker', 'finalizer'):
        setting = config.role(name)
        profile = config.document['llm']['profiles'][setting['profile']]
        roles[name] = {'request': asdict(llm.request([], role=name)),
                       'provider': profile['provider'], 'base_url': resolved(profile, 'base_url'),
                       'reasoning_field': profile.get('reasoning_field'),
                       'timeout_seconds': profile.get('timeout_seconds', 45)}
    reads = {}
    for domain in ('web_search', 'web_content', 'weather'):
        profile = config.profile(domain)
        # Endpoint/behavior fields are non-secret. Credential values and their
        # environment variable names do not affect continuation compatibility.
        reads[domain] = {key: value for key, value in profile.items()
                         if key not in {'key_env', 'base_url_env', 'base_url'}}
        reads[domain]['base_url'] = resolved(profile, 'base_url')
    deployment = {'roles': roles, 'reads': reads,
                  'task_budget': asdict(task_budget or TaskBudget.from_environment())}
    return 'models-' + hashlib.sha256(json.dumps(deployment, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass
class NLPBundle:
    service: NLPService
    resources: AsyncExitStack
    metrics: UsageMetrics
    readiness: Readiness
    closed: bool = False

    @property
    def local_ready(self):
        if self.closed or self.service.task_service.store.client.is_closed:
            return False
        model = self.service.model
        return all(not model.providers[model.config.role(role)['profile']].transport.client.is_closed
                   for role in ('talker', 'planner', 'thinker', 'finalizer'))

    async def aclose(self):
        self.closed = True
        await self.resources.aclose()


async def create_nlp_services(config=None, *, settings: NLPSettings | None = None):
    settings = settings or NLPSettings.from_environment()
    validate_all()
    config = FrozenNLPConfig(config or load_services_config())
    task_budget = TaskBudget.from_settings(settings)
    async with AsyncExitStack() as resources:
        raw_llm = create_llm(config)
        resources.push_async_callback(raw_llm.aclose)
        metrics = UsageMetrics()
        llm = MeasuredLLM(raw_llm, metrics)
        for name in ('talker', 'planner', 'thinker', 'finalizer'):
            if not llm.providers[config.role(name)['profile']].info()['configured']:
                raise ValueError('Selected NLP model endpoint or credential is missing: ' + name)
        secrets = [api_key(profile) for profile in config.document['llm']['profiles'].values()]
        for domain in ('web_search', 'web_content', 'weather', 'places'):
            secrets.append(api_key(config.profile(domain)))
        ledger = GatewayTaskStore(settings.gateway_url, token=settings.gateway_token.get_secret_value(),
                                  timeout_seconds=settings.gateway_timeout_seconds, secrets=secrets)
        resources.push_async_callback(ledger.aclose)
        search = create_search(config)
        resources.push_async_callback(search.aclose)
        maps = create_maps(config)
        resources.push_async_callback(maps.aclose)
        policy_id = model_policy_id(llm, config, task_budget)

        def runner(store):
            return TaskRunner(store, llm, search, trace_sink=store, weather=maps.weather,
                              forecast_verifier=verify_forecast_record, close_clients=False,
                              **asdict(task_budget))

        task_service = TaskService(ledger, runner, model_policy_id=policy_id)
        service = NLPService(llm, model_policy_id=policy_id,
                             semantic_policy=ProductTalkerPolicy(product_bindings(config),
                                 enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management']),
                             diagnostics=GatewayDiagnostics(ledger), planner=VehicleControlPlanner(llm),
                             task_service=task_service)
        return NLPBundle(service, resources.pop_all(), metrics, Readiness(settings, ledger.client))
