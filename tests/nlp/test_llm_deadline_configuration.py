"""Role deadlines are validated, frozen and routed without provider calls."""
from super_xd.nlp.contracts.providers import ModelRequest
from tests.support.models import scripted_result
from copy import deepcopy
import tomllib

import pytest

from super_xd.nlp.assembly import FrozenNLPConfig, TaskBudget, model_policy_id
from super_xd.nlp.config.services import ServiceConfig
from super_xd.nlp.contracts.providers import ModelResult
from super_xd.nlp.adapters.models.service import ChatProvider, ModelService
from super_xd.nlp.defaults import ROOT


FIELDS = ('first_output_timeout_seconds', 'total_timeout_seconds')
ROLES = ('talker', 'planner', 'thinker', 'finalizer')


def document():
    with (ROOT / 'config/services.toml').open('rb') as source:
        return tomllib.load(source)


class RecordingProvider:
    def __init__(self):
        self.requests = []

    def info(self):
        return {'capabilities': {'tools': True, 'tool_choice': True}}

    async def complete(self, request):
        self.requests.append(request)
        return ModelResult({'role': 'assistant', 'content': 'offline response'})

    async def stream(self, request):
        self.requests.append(request)
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'offline response'}}


def service(config):
    provider = RecordingProvider()
    return ModelService(config, providers={name: provider for name in config.document['llm']['profiles']}), provider


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', [False, True, 0, -1, 301, float('nan'), float('inf'), -float('inf'), '10', None])
def test_invalid_role_deadline_is_rejected_before_provider_creation(field, value):
    config = document()
    config['llm']['roles']['talker'][field] = value
    with pytest.raises(ValueError, match=field):
        ServiceConfig(config)


@pytest.mark.parametrize('explicit_total', [True, False])
def test_first_output_deadline_cannot_exceed_effective_total(explicit_total):
    config = document()
    role = config['llm']['roles']['talker']
    role['first_output_timeout_seconds'] = 21
    if explicit_total:
        role['total_timeout_seconds'] = 20
    else:
        role.pop('total_timeout_seconds', None)
        config['llm']['profiles'][role['profile']]['timeout_seconds'] = 20
    with pytest.raises(ValueError, match='first output timeout'):
        ServiceConfig(config)


def test_equal_deadlines_and_fractional_positive_boundaries_are_valid():
    for value in (0.01, 300):
        config = document()
        config['llm']['roles']['talker'].update(dict.fromkeys(FIELDS, value))
        assert ServiceConfig(config).role('talker')['total_timeout_seconds'] == value


async def test_configured_deadlines_follow_role_selection_without_leaking_to_other_roles():
    config = FrozenNLPConfig(ServiceConfig(document()))
    llm, recorder = service(config)
    messages = [{'role': 'user', 'content': 'unchanged request'}]
    await llm.complete(ModelRequest(messages))
    await llm.complete(ModelRequest(messages, max_tokens=120, reasoning=False, role='planner'))
    await llm.complete(ModelRequest(messages, role='thinker' if True else 'talker', reasoning=True))
    _ = [event async for event in llm.stream(ModelRequest(messages, role='finalizer'))]
    assert [request.role for request in recorder.requests] == list(ROLES)
    assert [(request.first_output_timeout_seconds, request.total_timeout_seconds)
            for request in recorder.requests] == [(10, 15), (None, 30), (None, None), (None, None)]
    assert all(request.messages is messages for request in recorder.requests)
    assert recorder.requests[1].max_tokens == 120
    assert recorder.requests[1].reasoning is False


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('field', FIELDS)
def test_deadline_change_requires_a_new_frozen_model_policy(role, field):
    source = document()
    source['llm']['roles'][role].update(first_output_timeout_seconds=10, total_timeout_seconds=15)
    config = ServiceConfig(source)
    frozen = FrozenNLPConfig(config)
    llm, _ = service(frozen)
    budget = TaskBudget(7, 31, 9, 4)
    before = model_policy_id(llm, frozen, budget)
    original_request = llm.resolve(ModelRequest([], role=role))

    config.document['llm']['roles'][role][field] += 1
    returned = frozen.role(role)
    returned[field] = 99
    assert llm.resolve(ModelRequest([], role=role)) == original_request
    assert model_policy_id(llm, frozen, budget) == before

    replacement = FrozenNLPConfig(config)
    next_llm, _ = service(replacement)
    assert getattr(next_llm.resolve(ModelRequest([], role=role)), field) == getattr(original_request, field) + 1
    assert model_policy_id(next_llm, replacement, budget) != before
    assert all(next_llm.resolve(ModelRequest([], role=other)) == llm.resolve(ModelRequest([], role=other))
               for other in ROLES if other != role)


@pytest.mark.parametrize('provider_name', ['ark', 'openai_compatible'])
async def test_deadlines_reach_stream_and_complete_transport_without_entering_model_messages(monkeypatch, provider_name):
    class Transport:
        def __init__(self):
            self.calls = []

        async def complete(self, request):
            messages = request.messages
            options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
            self.calls.append(('complete', deepcopy(messages), options))
            return scripted_result({'role': 'assistant', 'content': 'actual fixture response'})

        async def stream(self, request):
            messages = request.messages
            options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
            self.calls.append(('stream', deepcopy(messages), options))
            yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'actual fixture response'}}

    monkeypatch.setenv('TEST_DEADLINE_KEY', 'offline-transport-fixture')
    profile = {'provider': provider_name, 'base_url': 'https://llm.test/v1',
               'key_env': 'TEST_DEADLINE_KEY', 'timeout_seconds': 45}
    transport = Transport()
    provider = ChatProvider(profile, transport=transport)
    config = ServiceConfig(document())
    llm = ModelService(config, providers={name: provider for name in config.document['llm']['profiles']})
    messages = [{'role': 'user', 'content': 'original query'}]
    await llm.complete(ModelRequest(messages, role='planner'))
    _ = [event async for event in llm.stream(ModelRequest(messages))]
    assert [mode for mode, _, _ in transport.calls] == ['complete', 'stream']
    assert all(payload == messages for _, payload, _ in transport.calls)
    assert 'first_output_timeout_seconds' not in transport.calls[0][2]
    assert transport.calls[0][2]['total_timeout_seconds'] == 30
    assert transport.calls[1][2]['first_output_timeout_seconds'] == 10
    assert transport.calls[1][2]['total_timeout_seconds'] == 15


@pytest.mark.parametrize('value', ['0', '-1', '31', 'nan', 'inf'])
def test_foreground_wait_configuration_rejects_invalid_time_budget(value):
    from super_xd.nlp.settings import NLPSettings
    with pytest.raises(ValueError):
        NLPSettings.from_environment({'NLP_FOREGROUND_WAIT_SECONDS': value})


def test_foreground_delivery_wait_is_frozen_in_production_and_demo_model_policy():
    from super_xd.demo.resources.runtime import local_model_policy_id
    from super_xd.nlp.settings import NLPSettings
    settings = NLPSettings.from_environment({'NLP_FOREGROUND_WAIT_SECONDS': '6.5'})
    assert settings.foreground_wait_seconds == 6.5
    llm, _ = service(FrozenNLPConfig(ServiceConfig(document())))
    budget = TaskBudget.from_settings(settings)
    production = model_policy_id(llm, llm.config, budget, foreground_wait_seconds=settings.foreground_wait_seconds)
    assert local_model_policy_id(llm, budget, foreground_wait_seconds=6.5) == production
    assert model_policy_id(llm, llm.config, budget, foreground_wait_seconds=8) != production
