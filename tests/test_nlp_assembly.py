import pytest
import subprocess
import sys

from backend.harness import assembly
from backend.harness.product_policy import ProductTalkerPolicy
from backend.services.config import load_services_config


@pytest.mark.asyncio
async def test_production_composition_has_full_policy_and_real_remote_ports(monkeypatch):
    config = load_services_config()
    for profile in config.document['llm']['profiles'].values():
        if profile.get('key_env'):
            monkeypatch.setenv(profile['key_env'], 'offline-configuration-test')
    monkeypatch.setenv('NLP_GATEWAY_URL', 'https://gateway.test')
    bundle = await assembly.create_nlp_services(config)
    try:
        nlp = bundle.service
        assert isinstance(nlp.semantic_policy, ProductTalkerPolicy)
        assert nlp.planner is not None and nlp.task_service is not None
        assert nlp.diagnostics.transport is nlp.task_service.store
        assert nlp.task_service.store.base_url == 'https://gateway.test'
        assert nlp.semantic_policy.enabled_nlp_targets == {'vehicle_planner', 'thinker_task', 'task_management'}
        before = nlp.model_policy_id
        role = config.role('talker')
        if role.get('model_env'):
            monkeypatch.setenv(role['model_env'], 'changed-model')
            assert assembly.model_policy_id(nlp.model, nlp.model.config) == before
            replacement = await assembly.create_nlp_services(config)
            try:
                assert replacement.service.model_policy_id != before
                assert replacement.service.model.request([]).model == 'changed-model'
            finally:
                await replacement.aclose()
        assert 'offline-configuration-test' not in before
    finally:
        await bundle.aclose()
    assert nlp.task_service.store.client.is_closed


@pytest.mark.asyncio
async def test_missing_gateway_configuration_fails_without_fallback(monkeypatch):
    config = load_services_config()
    for profile in config.document['llm']['profiles'].values():
        if profile.get('key_env'):
            monkeypatch.setenv(profile['key_env'], 'offline-configuration-test')
    monkeypatch.delenv('NLP_GATEWAY_URL', raising=False)
    with pytest.raises(ValueError, match='NLP_GATEWAY_URL'):
        await assembly.create_nlp_services(config)


def test_bindings_cover_all_cockpit_actions_with_independent_operation_and_location():
    bindings = assembly.product_bindings(load_services_config())
    for action in assembly.MEDIA_ACTIONS:
        item = bindings['control_media.' + action]
        assert item.execution_location == 'device'
        assert item.operation == ('query' if action == 'get_status' else 'control')
    for action in assembly.NAVIGATION_ACTIONS:
        assert 'manage_navigation.' + action in bindings
    assert len([key for key in bindings if key.startswith('search_media.')]) == 3


def test_production_entrypoint_import_and_validation_do_not_load_demo_runtime():
    code = '''
import sys
import backend.nlp_app
import backend.harness.assembly
from backend.prompt_config import validate_all
validate_all()
forbidden = {'backend.app', 'backend.session', 'backend.worker', 'backend.db',
             'backend.asr', 'backend.tts', 'backend.speech_stream'}
assert not forbidden.intersection(sys.modules), sorted(forbidden.intersection(sys.modules))
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_execution_budgets_and_read_providers_are_frozen_and_fingerprinted(monkeypatch):
    config = load_services_config()
    for profile in config.document['llm']['profiles'].values():
        if profile.get('key_env'):
            monkeypatch.setenv(profile['key_env'], 'offline-configuration-test')
    monkeypatch.setenv('NLP_GATEWAY_URL', 'https://gateway.test')
    monkeypatch.setenv('THINKER_MAX_STEPS', '7')
    monkeypatch.setenv('THINKER_TIMEOUT_SECONDS', '31')
    monkeypatch.setenv('TOOL_TIMEOUT_SECONDS', '9')
    monkeypatch.setenv('WEB_SEARCH_PROFILE', 'gateway')
    monkeypatch.setenv('WEB_SEARCH_GATEWAY_URL', 'https://search-one.test')
    bundle = await assembly.create_nlp_services(config)
    try:
        nlp = bundle.service
        initial = nlp.task_service.runner_factory(object())
        assert (initial.max_steps, initial.timeout_seconds, initial.tool_timeout_seconds) == (7, 31, 9)
        before = nlp.model_policy_id
        monkeypatch.setenv('THINKER_MAX_STEPS', '8')
        monkeypatch.setenv('THINKER_TIMEOUT_SECONDS', '32')
        monkeypatch.setenv('TOOL_TIMEOUT_SECONDS', '10')
        monkeypatch.setenv('WEB_SEARCH_GATEWAY_URL', 'https://search-two.test')
        current = nlp.task_service.runner_factory(object())
        assert (current.max_steps, current.timeout_seconds, current.tool_timeout_seconds) == (7, 31, 9)
        assert current.tavily.search_provider.base_url == 'https://search-one.test'
        frozen_budget = assembly.TaskBudget(7, 31.0, 9.0, 4)
        assert assembly.model_policy_id(nlp.model, nlp.model.config, frozen_budget) == before
        assert assembly.model_policy_id(nlp.model, nlp.model.config) != before
        changed_reads = assembly.FrozenNLPConfig(config)
        assert assembly.model_policy_id(nlp.model, changed_reads, frozen_budget) != before
        monkeypatch.setenv('WEB_SEARCH_GATEWAY_KEY', 'rotated-secret')
        assert assembly.model_policy_id(nlp.model, changed_reads, frozen_budget) == assembly.model_policy_id(
            nlp.model, assembly.FrozenNLPConfig(config), frozen_budget)
    finally:
        await bundle.aclose()


@pytest.mark.parametrize('name,value', [('THINKER_MAX_STEPS', '0'), ('THINKER_TIMEOUT_SECONDS', 'nan'),
                                       ('TOOL_TIMEOUT_SECONDS', 'inf')])
def test_invalid_task_budget_fails_before_service_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        assembly.TaskBudget.from_environment()
