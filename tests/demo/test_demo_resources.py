"""Demo capability and command preparation must describe the actual local ports."""
from super_xd.nlp.contracts.providers import ModelRequest
from copy import deepcopy
from types import SimpleNamespace

import pytest

from super_xd.demo.devices.cockpit import initial_cockpit_state
from super_xd.nlp.policy.cockpit import prepare_media_action
from super_xd.demo.devices.catalog import DeviceRequest
from super_xd.demo.devices.executor import CockpitDevice
from super_xd.demo.resources.runtime import LocalResources, freeze_local_model, local_model_policy_id, local_task_budget
from super_xd.nlp.config.services import load_services_config
from super_xd.nlp.adapters.models.service import ModelService


def test_weather_and_places_declare_their_own_provider_availability():
    maps = SimpleNamespace(places=SimpleNamespace(info=lambda: {'configured': False}),
                           weather=SimpleNamespace(info=lambda: {'configured': True}))
    rows, _ = LocalResources(maps=maps, web=None, manual=None).declarations()
    assert next(row for row in rows if row.name == 'get_weather').available is True
    assert next(row for row in rows if row.name == 'search_places').available is False


def test_freeze_retains_selected_role_and_resolves_environment_once(monkeypatch):
    config = load_services_config()
    config.document['llm']['roles']['finalizer']['model_env'] = 'OFFLINE_MODEL_SELECTOR'
    monkeypatch.setenv('OFFLINE_MODEL_SELECTOR', 'frozen-finalizer')
    provider = SimpleNamespace(info=lambda: {'capabilities': {'tools': True, 'tool_choice': True}})
    model = ModelService(config, providers={name: provider for name in config.document['llm']['profiles']},
                       owner=False)
    frozen = freeze_local_model(model)
    monkeypatch.setenv('OFFLINE_MODEL_SELECTOR', 'changed-finalizer')
    assert frozen.resolve(ModelRequest([], role='finalizer')).role == 'finalizer'
    assert frozen.resolve(ModelRequest([], role='finalizer')).model == 'frozen-finalizer'
    assert frozen.providers is model.providers and frozen.owner is False


def test_local_policy_covers_frozen_reasoning_tokens_and_task_budget():
    config = load_services_config()
    provider = SimpleNamespace(info=lambda: {'capabilities': {'tools': True, 'tool_choice': True}})
    clients = {name: provider for name in config.document['llm']['profiles']}
    model = freeze_local_model(ModelService(config, providers=clients, owner=False))
    budget = local_task_budget()
    identity = local_model_policy_id(model, budget)
    config.document['llm']['roles']['thinker']['reasoning'] = not config.role('thinker')['reasoning']
    config.document['llm']['roles']['thinker']['max_tokens'] += 1
    changed = freeze_local_model(ModelService(config, providers=clients, owner=False))
    assert local_model_policy_id(model, budget) == identity
    assert local_model_policy_id(changed, budget) != identity
    assert local_model_policy_id(model, local_task_budget(max_steps=1)) != identity


async def test_session_registration_and_independent_worker_share_deployment_identity(tmp_path):
    from super_xd.demo.tasks.database import Database
    from super_xd.demo.session.actor import SessionActor
    from super_xd.demo.tasks.worker import ThinkerWorker
    from tests.support.worker import FakeArk, FakeTavily
    model, search = FakeArk(), FakeTavily()
    actor = SessionActor('fixture', Database(tmp_path / 'same.sqlite'), model, web=search)
    try:
        worker = ThinkerWorker(actor.db, model, search, close_clients=False)
        assert worker.release_id == actor.nlp.task_service.release_id
        assert worker.model_policy_id == actor.nlp.task_service.model_policy_id
    finally:
        await actor.close()


@pytest.mark.parametrize('change', [{'source_url': 'javascript:bad'}, {'kind': 'video'}])
async def test_resolve_rejects_invalid_playback_source_or_changed_kind(change):
    item = {'content_id': 'one', 'kind': 'music', 'title': 'One', 'provider': 'fixture',
            'source_url': 'https://media.test/one'}
    class Media:
        def info(self): return {'execution_location': 'cloud'}
        async def resolve(self, identifier): return {**item, **change}
    command = {'domain': 'media', 'action': 'play', 'arguments': item, 'expected_state_version': 0,
               'requires_content_resolution': True}
    dispatch = SimpleNamespace(prepared_command=command)
    resources = LocalResources(maps=None, web=None, manual=None, cockpit=SimpleNamespace(media=Media()))
    with pytest.raises(ValueError):
        await resources.resolve_command(SimpleNamespace(dispatch=dispatch))
    assert command['arguments'] == item


def test_device_queue_is_explicit_command_data_and_survives_next_turn():
    choices = {name: {'content_id': name, 'kind': 'music', 'title': name, 'provider': 'fixture'}
               for name in ('one', 'two')}
    device = CockpitDevice()
    initial = deepcopy(device.state)
    prepared = prepare_media_action('play', {'content_id': 'one'}, initial['media'], choices)
    assert prepared['arguments']['queue'] == list(choices.values())
    wire = DeviceRequest('one', 's', 't', 'media', 'play', prepared['arguments'], 'control', 'test', 1000).to_wire()
    wire['expected_state_version'] = 0
    receipt = device.execute(wire)
    assert receipt['state']['queue'] == list(choices.values())
    assert initial == initial_cockpit_state()
    next_command = prepare_media_action('next', {}, device.state['media'], {})
    assert next_command['arguments']['content_id'] == 'two'
    # Mutating NLP's predicted state cannot mutate the independent device.
    prepared['expected']['queue'].clear()
    assert device.state['media']['queue'] == list(choices.values())
