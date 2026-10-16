"""Load runtime service choices once; unknown configuration fails at startup."""
from copy import deepcopy
from functools import lru_cache
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlparse

from backend.settings import ROOT

DOMAINS = ('asr', 'tts', 'web_search', 'web_content', 'places', 'weather', 'embedding', 'manual', 'media', 'routes')
PROVIDERS = {
    'llm': {'ark', 'openai_compatible'}, 'asr': {'volcengine', 'vllm'},
    'tts': {'volcengine'}, 'web_search': {'tavily', 'http_gateway'},
    'web_content': {'tavily', 'http_gateway'}, 'places': {'google', 'http_gateway', 'device_gateway'},
    'weather': {'google', 'open_meteo', 'http_gateway'}, 'embedding': {'ark', 'openai_compatible'},
    'manual': {'local_rag', 'http_gateway'},
    'media': {'tencent_gateway', 'media_gateway', 'local_catalog', 'device_catalog'}, 'routes': {'google', 'http_gateway', 'none'},
}
COMMON = {'provider', 'base_url', 'base_url_env', 'key_env', 'require_api_key', 'timeout_seconds'}
FIELDS = {
    'llm': COMMON | {'supports_tools', 'supports_tool_choice', 'reasoning_field'},
    'asr': COMMON | {'model', 'model_env', 'language', 'url', 'url_env', 'max_audio_seconds',
                     'max_record_seconds', 'app_id_env', 'access_token_env', 'resource_id'},
    'tts': COMMON | {'voices', 'app_id_env', 'access_token_env'}, 'web_search': COMMON, 'web_content': COMMON,
    'places': COMMON | {'coordinate_system', 'provider_name'}, 'weather': COMMON | {'coverage_fallback', 'task_forecast'},
    'embedding': COMMON | {'model', 'model_env', 'dimensions'},
    'manual': COMMON | {'index_path', 'index_path_env'},
    'media': COMMON | {'catalog_path', 'provider_name'}, 'routes': COMMON | {'coordinate_system'},
}


def resolved(profile, name, default=''):
    variable = profile.get(name + '_env')
    return os.getenv(variable, profile.get(name, default)) if variable else profile.get(name, default)


def api_key(profile):
    return os.getenv(profile.get('key_env', ''), '')


def validate_url(value, *, websocket=False):
    if not value:
        return ''
    parsed = urlparse(value)
    schemes = {'ws', 'wss'} if websocket else {'http', 'https'}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provider endpoint must be an absolute URL without credentials, query or fragment')
    return value.rstrip('/')


class ServiceConfig:
    def __init__(self, document, *, source=None):
        self.document = deepcopy(document)
        self.source = str(source) if source else '<injected>'
        if set(document) != {'version', 'llm', *DOMAINS} or document['version'] != 1:
            raise ValueError('services.toml must declare version=1 and all service domains')
        for domain in ('llm', *DOMAINS):
            section = document[domain]
            expected = {'roles', 'profiles'} if domain == 'llm' else {'bindings', 'profiles'} if domain == 'media' else {'profile', 'profiles'}
            if not isinstance(section, dict) or set(section) != expected or not isinstance(section['profiles'], dict) or not section['profiles']:
                raise ValueError(f'Invalid {domain} service section')
            for name, profile in section['profiles'].items():
                if not isinstance(name, str) or not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in name):
                    raise ValueError(f'Invalid {domain} profile name')
                if not isinstance(profile, dict) or profile.keys() - FIELDS[domain] or profile.get('provider') not in PROVIDERS[domain]:
                    raise ValueError(f'Unknown fields/provider in {domain}.profiles.{name}')
                for key, value in profile.items():
                    if key in {'require_api_key', 'supports_tools', 'supports_tool_choice'}:
                        if type(value) is not bool:
                            raise ValueError(f'{domain}.{name}.{key} must be boolean')
                    elif key in {'timeout_seconds', 'max_audio_seconds', 'max_record_seconds'}:
                        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                            raise ValueError(f'{domain}.{name}.{key} must be between 0 and 300')
                    elif key == 'dimensions':
                        if type(value) is not int or not 1 <= value <= 65536:
                            raise ValueError('Embedding dimensions must be a positive integer')
                    elif key == 'voices':
                        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in value.items()):
                            raise ValueError('Voice aliases must map to nonempty provider voice IDs')
                    elif not isinstance(value, str):
                        raise ValueError(f'{domain}.{name}.{key} must be text')
                validate_url(resolved(profile, 'base_url'))
                validate_url(resolved(profile, 'url'), websocket=True)
            if domain == 'media':
                self.media_profiles()
            elif domain != 'llm':
                self.profile(domain)
        roles = document['llm']['roles']
        if set(roles) != {'talker', 'planner', 'thinker', 'finalizer'}:
            raise ValueError('LLM roles must include talker/planner/thinker/finalizer')
        for name, role in roles.items():
            if not isinstance(role, dict) or role.keys() - {'profile', 'model', 'model_env', 'reasoning', 'max_tokens'}:
                raise ValueError(f'Invalid LLM role {name}')
            if role.get('profile') not in document['llm']['profiles'] or type(role.get('reasoning')) is not bool:
                raise ValueError(f'Invalid LLM profile/reasoning for {name}')
            if type(role.get('max_tokens')) is not int or role['max_tokens'] <= 0:
                raise ValueError(f'Invalid token budget for {name}')
            for key in ('model', 'model_env'):
                if key in role and (not isinstance(role[key], str) or not role[key]):
                    raise ValueError(f'Invalid model field for {name}')

    def profile(self, domain):
        if domain == 'media':
            return self.media_profiles()['music']
        section = self.document[domain]
        name = os.getenv(domain.upper() + '_PROFILE', section['profile'])
        if name not in section['profiles']:
            raise ValueError(f'Unknown selected {domain} profile {name!r}')
        return deepcopy(section['profiles'][name])

    def media_profiles(self):
        section = self.document['media']
        if not isinstance(section['bindings'], dict) or set(section['bindings']) != {'music', 'video', 'radio'}:
            raise ValueError('Media must bind music/video/radio independently')
        result = {}
        for kind, binding in section['bindings'].items():
            name = os.getenv('MEDIA_' + kind.upper() + '_PROFILE', os.getenv('MEDIA_PROFILE', binding))
            if not isinstance(name, str) or name not in section['profiles']:
                raise ValueError(f'Unknown selected {kind} content provider {name!r}')
            result[kind] = deepcopy(section['profiles'][name])
        return result

    def media_bindings(self):
        bindings = self.document['media']['bindings']
        return {kind: os.getenv('MEDIA_' + kind.upper() + '_PROFILE', os.getenv('MEDIA_PROFILE', name))
                for kind, name in bindings.items()}

    def role(self, name):
        return deepcopy(self.document['llm']['roles'][name])


@lru_cache(maxsize=None)
def load_services_config(path=None):
    path = Path(path or os.getenv('SERVICES_CONFIG_PATH', str(ROOT / 'config/services.toml')))
    with path.open('rb') as source:
        return ServiceConfig(tomllib.load(source), source=path)
