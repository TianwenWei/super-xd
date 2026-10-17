"""Live entry points are exercised only with explicit offline service fixtures."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import evaluate_engineering, evaluate_maps_fc, probe_apis, probe_local_vad


class ProbeConfig:
    source = 'project-specific-services.toml'

    def __init__(self, **profiles):
        self.profiles = {'places': {'provider': 'google', 'key_env': 'PROJECT_MAPS_KEY'},
                         'weather': {'provider': 'google', 'coverage_fallback': 'open_meteo'},
                         'asr': {'provider': 'volcengine', 'key_env': 'PROJECT_ASR_KEY'},
                         'tts': {'provider': 'volcengine', 'key_env': 'PROJECT_TTS_KEY'}, **profiles}

    def profile(self, name):
        return deepcopy(self.profiles[name])


class ServiceFixture:
    def __init__(self, *, configured=True):
        self.closed = 0
        self.configured = configured
        self.calls = []

    def info(self):
        return {'provider': 'explicit-offline-fixture', 'configured': self.configured}

    def request(self, messages, *, role):
        return SimpleNamespace(model='project-selected-' + role)

    async def stream(self, messages, tools=None, **options):
        self.calls.append(('stream', options))
        yield {'type': 'delta', 'text': '离线测试文本'}
        yield {'type': 'message', 'message': {'content': '离线测试文本'}}

    async def complete(self, messages, tools=None, **options):
        self.calls.append(('complete', options))
        return {'tool_calls': [{'function': {'name': 'probe_temperature', 'arguments': '{"value":23}'}}]}

    async def search(self, query, max_results):
        self.calls.append(('search', query))
        return {'queried_at': '2026-10-17T00:00:00Z', 'results': [{'url': 'https://example.test/evidence'}]}

    async def extract(self, urls):
        return {'results': [{'url': url} for url in urls]}

    async def aclose(self):
        self.closed += 1


def factory(monkeypatch, module, name, config, service, seen):
    def build(actual):
        assert actual is config
        seen.append(name)
        return service
    monkeypatch.setattr(module, name, build)


async def test_capability_probe_uses_selected_roles_and_search_configuration(monkeypatch, tmp_path):
    config, llm, search, seen = ProbeConfig(), ServiceFixture(), ServiceFixture(), []
    monkeypatch.setattr(probe_apis, 'load_services_config', lambda: config)
    factory(monkeypatch, probe_apis, 'create_llm', config, llm, seen)
    factory(monkeypatch, probe_apis, 'create_search', config, search, seen)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'docs').mkdir()
    report = await probe_apis.main()
    assert report['talker_model'] == 'project-selected-talker'
    assert report['thinker_model'] == 'project-selected-thinker'
    assert report['services_config'] == config.source
    assert all(item['ok'] for item in report['checks'].values())
    assert 'web_search_extract' in report['checks']
    assert [options['thinker'] for operation, options in llm.calls if operation == 'complete'] == [False, True]
    assert seen == ['create_llm', 'create_search'] and llm.closed == search.closed == 1


async def test_google_acceptance_uses_both_factories_and_closes_on_failure(monkeypatch):
    config, llm, maps, seen = ProbeConfig(), ServiceFixture(), ServiceFixture(), []
    monkeypatch.setattr(evaluate_maps_fc, 'load_services_config', lambda: config)
    factory(monkeypatch, evaluate_maps_fc, 'create_llm', config, llm, seen)
    factory(monkeypatch, evaluate_maps_fc, 'create_maps', config, maps, seen)
    async def fail(actual_maps, actual_llm, actual_config):
        assert (actual_maps, actual_llm, actual_config) == (maps, llm, config)
        raise RuntimeError('offline interruption')
    monkeypatch.setattr(evaluate_maps_fc, 'evaluate', fail)
    with pytest.raises(RuntimeError, match='offline interruption'):
        await evaluate_maps_fc.main()
    assert seen == ['create_maps', 'create_llm'] and llm.closed == maps.closed == 1


@pytest.mark.parametrize('profiles', [{'places': {'provider': 'device_gateway'}},
                                      {'weather': {'provider': 'google', 'coverage_fallback': 'none'}}])
async def test_google_specific_acceptance_refuses_to_probe_a_different_selected_stack(monkeypatch, profiles):
    monkeypatch.setattr(evaluate_maps_fc, 'load_services_config', lambda: ProbeConfig(**profiles))
    monkeypatch.setattr(evaluate_maps_fc, 'create_maps', lambda _: pytest.fail('must reject before building clients'))
    with pytest.raises(ValueError):
        await evaluate_maps_fc.main()


@pytest.mark.parametrize('configured', [True, False])
async def test_live_vad_uses_selected_factories_and_closes_all_clients(monkeypatch, tmp_path, configured):
    from backend.services import audio, config as configuration, factory as services
    config, llm, asr, tts, seen = ProbeConfig(), ServiceFixture(), ServiceFixture(configured=configured), ServiceFixture(), []
    monkeypatch.setattr(configuration, 'load_services_config', lambda: config)
    factory(monkeypatch, services, 'create_llm', config, llm, seen)
    monkeypatch.setattr(audio, 'build_asr', lambda profile: asr if profile == config.profile('asr') else pytest.fail('wrong ASR profile'))
    monkeypatch.setattr(audio, 'build_tts', lambda profile: tts if profile == config.profile('tts') else pytest.fail('wrong TTS profile'))
    async def probe(pcm, output, actual_llm, actual_asr, actual_tts, actual_config):
        assert (actual_llm, actual_asr, actual_tts, actual_config) == (llm, asr, tts, config)
        return {'ok': True}
    monkeypatch.setattr(probe_local_vad, 'probe_live', probe)
    if configured:
        assert (await probe_local_vad.live(b'fixture', tmp_path / 'probe.json'))['ok']
    else:
        with pytest.raises(RuntimeError):
            await probe_local_vad.live(b'fixture', tmp_path / 'probe.json')
    assert seen == ['create_llm'] and llm.closed == asr.closed == tts.closed == 1


async def test_live_vad_keeps_its_explicit_volcengine_scope(monkeypatch, tmp_path):
    from backend.services import config as configuration, factory as services
    monkeypatch.setattr(configuration, 'load_services_config', lambda: ProbeConfig(asr={'provider': 'vllm'}))
    monkeypatch.setattr(services, 'create_llm', lambda _: pytest.fail('must reject before building clients'))
    with pytest.raises(ValueError):
        await probe_local_vad.live(b'fixture', tmp_path / 'probe.json')


@pytest.mark.parametrize('model_calls,passed', [(1, True), (2, False)])
async def test_engineering_chat_acceptance_uses_single_call_without_legacy_turn_field(monkeypatch, tmp_path, model_calls, passed):
    config, llm, tts, seen = ProbeConfig(), ServiceFixture(), ServiceFixture(), []
    factory(monkeypatch, evaluate_engineering, 'create_llm', config, llm, seen)
    monkeypatch.setattr(evaluate_engineering, 'build_tts', lambda profile: tts if profile == config.profile('tts') else pytest.fail('wrong TTS profile'))
    monkeypatch.setattr(evaluate_engineering, 'Database', lambda _: SimpleNamespace(traces_after=lambda *args, **kwargs: []))
    class Actor:
        def __init__(self, name, db, actual_llm, actual_tts):
            assert (actual_llm, actual_tts) == (llm, tts)
            self.id, self.db = name, db
            self.current = SimpleNamespace(model_calls=model_calls, messages=[{'role': 'assistant', 'content': '实际测试答复'}])
            self.trace_queue, self.control, self.audio = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
        async def attach(self):
            self.control.put_nowait({'type': 'ready'})
            return self.control
        async def attach_audio(self):
            return self.audio
        async def post(self, event):
            if event['type'] == 'input':
                self.audio.put_nowait(bytes(32) + b'nonzero-pcm')
                self.audio.put_nowait({'type': 'audio.ended'})
                self.control.put_nowait({'type': 'stream.done', 'text': '实际测试答复', 'metrics': {}})
        async def close(self):
            pass
    monkeypatch.setattr(evaluate_engineering, 'SessionActor', Actor)
    result = await evaluate_engineering.run_case(tmp_path, 'chat', '聊聊秋天', config=config)
    assert result['passed'] is passed and result['first_nonzero_pcm_ms'] is not None
    assert result['services_config'] == config.source and seen == ['create_llm']
    assert llm.closed == tts.closed == 1
