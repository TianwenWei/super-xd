"""Credentials cannot be redirected through an audio profile override."""
from copy import deepcopy
import tomllib

import pytest

from backend.services.audio import build_asr, build_tts
from backend.services.config import ServiceConfig
from backend.services.regions import apply_region
from backend.settings import ROOT


def document():
    return tomllib.loads((ROOT / 'config/services.toml').read_text())


@pytest.mark.parametrize('domain', ['asr', 'tts'])
@pytest.mark.parametrize('field,value', [('base_url', 'https://other.example'),
                                       ('url', 'wss://other.example'),
                                       ('model', 'unsupported-model')])
def test_elevenlabs_rejects_endpoint_and_protocol_overrides(domain, field, value):
    source = document()
    source[domain]['profiles']['elevenlabs'][field] = value
    with pytest.raises(ValueError):
        ServiceConfig(source)


def test_europe_builds_declared_audio_adapters_without_opening_connections(monkeypatch):
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'offline-test-project')
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'offline-fixture-key')
    selected = apply_region(document(), 'europe')
    config = ServiceConfig(selected, region='europe')
    for domain, factory in [('asr', build_asr), ('tts', build_tts)]:
        profile = config.profile(domain)
        provider = factory(deepcopy(profile))
        assert provider.info()['provider'] == profile['provider']
        assert provider.info()['configured'] is True


def test_elevenlabs_rejects_recording_limit_beyond_cockpit_protocol():
    source = document()
    source['asr']['profiles']['elevenlabs']['max_record_seconds'] = 61
    with pytest.raises(ValueError, match='60 seconds'):
        ServiceConfig(source)
