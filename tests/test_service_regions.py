"""Regional choices are explicit, startup-only overlays; no provider calls."""
from copy import deepcopy
import tomllib

import pytest

from backend.harness.assembly import FrozenNLPConfig, TaskBudget, model_policy_id
from backend.harness.run import RunProtocolError
from backend.services.config import DOMAINS, ServiceConfig, load_services_config
from backend.services.llm import LLMService
from backend.services.regions import REGIONS_PATH, apply_region
from backend.settings import ROOT
from tests.test_gateway_contract import ScriptedModel, call, collect, resume_request, service, start_request


ROLES = ('talker', 'planner', 'thinker', 'finalizer')
SELECTORS = {'profile', 'model', 'model_env'}


def presets():
    with REGIONS_PATH.open('rb') as source:
        return tomllib.load(source)


def document():
    with (ROOT / 'config/services.toml').open('rb') as source:
        value = tomllib.load(source)
    return value


@pytest.fixture(autouse=True)
def isolated_deployment(monkeypatch):
    for domain in (*DOMAINS, 'media_music', 'media_video', 'media_radio'):
        monkeypatch.delenv(domain.upper() + '_PROFILE', raising=False)
    monkeypatch.delenv('SERVICE_REGION', raising=False)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'regional-offline-fixture')
    monkeypatch.setenv('TALKER_MODEL', 'ark-talker-fixture')
    monkeypatch.setenv('VEHICLE_CONTROL_MODEL', 'ark-planner-fixture')
    monkeypatch.setenv('THINKER_MODEL', 'ark-thinker-fixture')
    load_services_config.cache_clear()
    yield
    load_services_config.cache_clear()


@pytest.mark.parametrize('region', ['europe', 'china'])
def test_region_binds_all_six_services_without_changing_budgets_or_other_domains(region):
    source = document()
    before = deepcopy(source)
    selected = presets()['regions'][region]
    result = apply_region(source, region)
    assert source == before
    for role in ROLES:
        assert {key: result['llm']['roles'][role][key] for key in selected['llm'][role]} == selected['llm'][role]
        assert {key: value for key, value in result['llm']['roles'][role].items() if key not in SELECTORS} == {
            key: value for key, value in source['llm']['roles'][role].items() if key not in SELECTORS}
    assert result['llm']['profiles'] == source['llm']['profiles']
    for domain in ('asr', 'tts'):
        assert result[domain]['profile'] == selected[domain]
        assert result[domain]['profiles'] == source[domain]['profiles']
    for domain in set(DOMAINS) - {'asr', 'tts'}:
        assert result[domain] == source[domain]
    result['llm']['roles']['talker']['max_tokens'] = 1
    assert source == before


def test_china_clears_google_literal_and_restores_existing_ark_model_selectors():
    europe = apply_region(document(), 'europe')
    assert all('model_env' not in europe['llm']['roles'][role] for role in ROLES)
    china = apply_region(europe, 'china')
    frozen = FrozenNLPConfig(ServiceConfig(china, region='china'))
    expected = ('ark-talker-fixture', 'ark-planner-fixture', 'ark-thinker-fixture', 'ark-talker-fixture')
    for role, model in zip(ROLES, expected, strict=True):
        assert china['llm']['roles'][role]['profile'] == 'ark'
        assert 'model' not in china['llm']['roles'][role]
        assert frozen.role(role)['model'] == model
    assert all(europe['llm']['roles'][role]['model'].startswith('google/gemini-') for role in ROLES)


def test_europe_keeps_gemini_and_elevenlabs_asr_but_reuses_original_volcengine_voice():
    source = document()
    europe = apply_region(source, 'europe')
    china = apply_region(source, 'china')
    config = ServiceConfig(europe, region='europe')
    assert europe['tts']['profile'] == china['tts']['profile'] == 'volcengine'
    assert config.profile('tts') == source['tts']['profiles']['volcengine']
    assert config.profile('tts')['voices'] == {
        'default': 'zh_female_xiaohe_uranus_bigtts', 'assistant': 'zh_female_xiaohe_uranus_bigtts'}
    assert config.profile('asr')['provider'] == 'elevenlabs'
    assert config.profile('asr')['model'] == 'scribe_v2_realtime'
    assert all(config.role(role)['profile'] == 'google_vertex'
               and config.role(role)['model'] == 'google/gemini-3.8-flash' for role in ROLES)
    assert europe['llm']['profiles']['google_vertex']['location'] == 'eu'


def test_unset_region_keeps_explicit_bindings_and_returns_an_independent_document():
    source = apply_region(document(), 'europe')
    result = apply_region(source, '')
    assert result == source
    result['tts']['profile'] = 'elevenlabs'
    assert source['tts']['profile'] == presets()['regions']['europe']['tts']


@pytest.mark.parametrize('region', ['europe', 'china'])
def test_selected_region_ignores_individual_audio_selectors_only(monkeypatch, region):
    monkeypatch.setenv('ASR_PROFILE', 'not_a_profile')
    monkeypatch.setenv('TTS_PROFILE', 'not_a_profile')
    monkeypatch.setenv('WEATHER_PROFILE', 'open_meteo')
    selected = apply_region(document(), region)
    config = ServiceConfig(selected, region=region)
    assert config.profile('asr') == selected['asr']['profiles'][selected['asr']['profile']]
    assert config.profile('tts') == selected['tts']['profiles'][selected['tts']['profile']]
    assert config.profile('weather')['provider'] == 'open_meteo'
    with pytest.raises(ValueError):
        ServiceConfig(selected)


def test_unset_region_preserves_explicit_audio_environment_overrides(monkeypatch):
    monkeypatch.setenv('ASR_PROFILE', 'vllm')
    monkeypatch.setenv('TTS_PROFILE', 'google')
    config = ServiceConfig(document())
    assert config.profile('asr')['provider'] == 'vllm'
    assert config.profile('tts')['provider'] == 'google'


def test_elevenlabs_tts_remains_an_explicit_option_outside_regional_binding(monkeypatch):
    monkeypatch.setenv('TTS_PROFILE', 'elevenlabs')
    config = ServiceConfig(document())
    assert config.profile('tts')['provider'] == 'elevenlabs'
    assert config.profile('tts')['model'] == 'eleven_flash_v2_5'
    assert ServiceConfig(apply_region(document(), 'europe'), region='europe').profile('tts')['provider'] == 'volcengine'


def test_unknown_region_is_rejected_before_service_construction(monkeypatch):
    import backend.services.config as module

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid region must fail before constructing a service configuration')

    monkeypatch.setattr(module, 'ServiceConfig', forbidden)
    monkeypatch.setenv('SERVICE_REGION', 'unknown')
    with pytest.raises(ValueError):
        load_services_config(ROOT / 'config/services.toml')


@pytest.mark.parametrize('change', [
    lambda value: value.update(version=True),
    lambda value: value.update(version=2),
    lambda value: value.update(regions=[]),
    lambda value: value['regions'].update(europe=None),
    lambda value: value['regions']['europe'].update(llm=None),
    lambda value: value['regions']['europe']['llm'].pop('planner'),
    lambda value: value['regions']['europe']['llm']['talker'].update(model_env='STALE_MODEL'),
    lambda value: value['regions']['europe']['llm']['talker'].update(max_tokens=1),
    lambda value: value['regions']['europe']['llm']['talker'].update(profile=''),
    lambda value: value['regions']['europe'].update(asr=None),
])
def test_malformed_selected_presets_raise_configuration_errors(change):
    value = presets()
    change(value)
    with pytest.raises(ValueError):
        apply_region(document(), 'europe', presets=value)


def test_startup_load_and_frozen_models_do_not_reread_region_or_model_environment(monkeypatch, tmp_path):
    # Select China first so this loader test needs no pending audio profile.
    source = tmp_path / 'services.toml'
    source.write_bytes((ROOT / 'config/services.toml').read_bytes())
    monkeypatch.setenv('SERVICE_REGION', 'china')
    loaded = load_services_config(source)
    frozen = FrozenNLPConfig(loaded)
    original = frozen.role('talker')
    monkeypatch.setenv('SERVICE_REGION', 'invalid-after-start')
    monkeypatch.setenv('TALKER_MODEL', 'changed-after-start')
    monkeypatch.setenv('ASR_PROFILE', 'invalid-after-start')
    assert load_services_config(source) is loaded
    assert loaded.region == frozen.region == 'china'
    assert loaded.profile('asr')['provider'] == 'volcengine'
    assert frozen.role('talker') == original
    load_services_config.cache_clear()
    with pytest.raises(ValueError):
        load_services_config(source)


class OfflineProvider:
    def info(self):
        return {'capabilities': {'tools': True, 'tool_choice': True}}


def policy_identity(region):
    config = FrozenNLPConfig(ServiceConfig(apply_region(document(), region), region=region))
    llm = LLMService(config, providers={name: OfflineProvider() for name in config.document['llm']['profiles']})
    return model_policy_id(llm, config, TaskBudget(20, 60, 12, 4))


async def test_changed_regional_model_policy_requires_a_new_run():
    china_id, europe_id = policy_identity('china'), policy_identity('europe')
    assert china_id != europe_id
    previous_model = ScriptedModel([{'tool_calls': [call()]}])
    handoff = (await collect(service(previous_model, model_policy_id=china_id).start(start_request())))[-1]
    assert handoff.type == 'run.suspended'
    next_model = ScriptedModel([])
    with pytest.raises(RunProtocolError):
        service(next_model, model_policy_id=europe_id).resume(resume_request(handoff.checkpoint))
    assert next_model.requests == []
