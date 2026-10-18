"""Load runtime service choices once; unknown configuration fails at startup."""
from copy import deepcopy
from functools import lru_cache
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlparse

from super_xd.nlp.defaults import ROOT

DOMAINS = ('asr', 'tts', 'web_search', 'web_content', 'places', 'weather', 'embedding', 'manual', 'media', 'routes')
PROVIDERS = {
    'llm': {'ark', 'openai_compatible', 'google_vertex'}, 'asr': {'volcengine', 'vllm', 'elevenlabs'},
    'tts': {'volcengine', 'google', 'elevenlabs'}, 'web_search': {'tavily', 'http_gateway'},
    'web_content': {'tavily', 'http_gateway'}, 'places': {'google', 'http_gateway', 'device_gateway'},
    'weather': {'google', 'open_meteo', 'http_gateway'}, 'embedding': {'ark', 'openai_compatible'},
    'manual': {'local_rag', 'http_gateway'},
    'media': {'tencent_gateway', 'media_gateway', 'local_catalog', 'device_catalog'}, 'routes': {'google', 'http_gateway', 'none'},
}
COMMON = {'provider', 'base_url', 'base_url_env', 'key_env', 'require_api_key', 'timeout_seconds'}
FIELDS = {
    'llm': COMMON | {'supports_tools', 'supports_tool_choice', 'reasoning_field',
                     'project', 'project_env', 'location', 'reasoning_effort'},
    'asr': COMMON | {'model', 'model_env', 'language', 'url', 'url_env', 'max_audio_seconds',
                     'max_record_seconds', 'app_id_env', 'access_token_env', 'resource_id'},
    'tts': COMMON | {'voices', 'app_id_env', 'access_token_env', 'project', 'project_env', 'location', 'language', 'model'}, 'web_search': COMMON, 'web_content': COMMON,
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


def google_vertex_url(profile, *, required=False):
    project, location = resolved(profile, 'project'), profile.get('location', '')
    if not isinstance(location, str) or not location or any(
            character not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for character in location):
        raise ValueError('Google Vertex location must be explicit and valid')
    if location not in {'eu', 'us', 'global'} and (
            len(location.split('-')) < 2 or not location[-1].isdigit()):
        raise ValueError('Google Vertex location must be global, eu, us or a regional location')
    if not project:
        if required:
            raise ValueError('Google Vertex project/project_env must resolve to a project')
        return ''
    if not isinstance(project, str) or any(
            character not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for character in project):
        raise ValueError('Google Vertex project must be a project ID or number')
    host = ('aiplatform.googleapis.com' if location == 'global' else
            f'aiplatform.{location}.rep.googleapis.com' if location in {'eu', 'us'} else
            f'{location}-aiplatform.googleapis.com')
    return f'https://{host}/v1/projects/{project}/locations/{location}/endpoints/openapi'


def validate_vertex_model(profile, model):
    if not isinstance(model, str) or not model.startswith('google/gemini-') or not model[14:] or any(
            character not in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for character in model[14:]):
        raise ValueError('Google Vertex requires an explicit google/gemini-* model')
    effort = profile.get('reasoning_effort')
    if effort is not None and effort not in {'minimal', 'low', 'medium', 'high'}:
        raise ValueError('Google Vertex reasoning_effort must be minimal, low, medium or high')
    if model == 'google/gemini-3.8-flash' and effort == 'minimal':
        raise ValueError('Gemini 3.8 Flash does not support minimal reasoning')
    locations = {
        'google/gemini-3.8-flash': {'global', 'us', 'eu'},
        'google/gemini-3.5-flash-lite': {'global', 'us', 'eu', 'asia-south1'},
    }
    if model in locations and profile.get('location') not in locations[model]:
        raise ValueError('Selected Gemini model does not support this Google Vertex location')


def validate_url(value, *, websocket=False):
    if not value:
        return ''
    parsed = urlparse(value)
    schemes = {'ws', 'wss'} if websocket else {'http', 'https'}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provider endpoint must be an absolute URL without credentials, query or fragment')
    return value.rstrip('/')


class ServiceConfig:
    def __init__(self, document, *, source=None, region=''):
        self.document = deepcopy(document)
        self.source = str(source) if source else '<injected>'
        self.region = region
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
                vertex_fields = {'project', 'project_env', 'location', 'reasoning_effort'}
                if domain == 'llm' and profile['provider'] == 'google_vertex':
                    if profile.keys() - (vertex_fields | {'provider', 'timeout_seconds', 'supports_tools', 'supports_tool_choice'}):
                        raise ValueError('Google Vertex uses ADC and a project/location endpoint, not API keys/base_url')
                    google_vertex_url(profile)
                    if profile.get('reasoning_effort', 'low') not in {'minimal', 'low', 'medium', 'high'}:
                        raise ValueError('Invalid Google Vertex reasoning_effort')
                elif domain == 'llm' and profile.keys() & vertex_fields:
                    raise ValueError('Google Vertex fields require provider=google_vertex')
                if domain == 'asr' and profile['provider'] == 'elevenlabs':
                    if profile.keys() - {'provider', 'key_env', 'model', 'language', 'timeout_seconds', 'max_record_seconds'}:
                        raise ValueError('ElevenLabs ASR uses a fixed official endpoint and manual PCM16K streaming')
                    if profile.get('model') != 'scribe_v2_realtime' or not profile.get('key_env') or not profile.get('language'):
                        raise ValueError('ElevenLabs ASR requires scribe_v2_realtime, key_env and language')
                    if profile.get('max_record_seconds', 60) > 60:
                        raise ValueError('ElevenLabs ASR recordings must not exceed 60 seconds')
                if domain == 'tts' and profile['provider'] == 'elevenlabs':
                    if profile.keys() - {'provider', 'key_env', 'model', 'language', 'voices', 'timeout_seconds'}:
                        raise ValueError('ElevenLabs TTS uses a fixed official endpoint')
                    if profile.get('model') != 'eleven_flash_v2_5' or not profile.get('key_env') or not profile.get('language'):
                        raise ValueError('ElevenLabs TTS requires eleven_flash_v2_5, key_env and language')
                if domain == 'tts' and profile['provider'] == 'google':
                    if profile.keys() - {'provider', 'project', 'project_env', 'location', 'language', 'voices', 'timeout_seconds'}:
                        raise ValueError('Google TTS uses ADC and a fixed location endpoint')
                    if profile.get('location') not in {'eu', 'us', 'global'}:
                        raise ValueError('Google TTS location must be eu, us or global')
                    google_vertex_url(profile)
            if domain == 'media':
                self.media_profiles()
            elif domain != 'llm':
                selected = self.profile(domain)
                if domain == 'tts' and selected['provider'] == 'google':
                    google_vertex_url(selected, required=True)
        roles = document['llm']['roles']
        if set(roles) != {'talker', 'planner', 'thinker', 'finalizer'}:
            raise ValueError('LLM roles must include talker/planner/thinker/finalizer')
        for name, role in roles.items():
            if not isinstance(role, dict) or role.keys() - {
                    'profile', 'model', 'model_env', 'reasoning', 'max_tokens',
                    'first_output_timeout_seconds', 'total_timeout_seconds'}:
                raise ValueError(f'Invalid LLM role {name}')
            if role.get('profile') not in document['llm']['profiles'] or type(role.get('reasoning')) is not bool:
                raise ValueError(f'Invalid LLM profile/reasoning for {name}')
            if type(role.get('max_tokens')) is not int or role['max_tokens'] <= 0:
                raise ValueError(f'Invalid token budget for {name}')
            for key in ('first_output_timeout_seconds', 'total_timeout_seconds'):
                if key in role and (type(role[key]) not in (int, float)
                                    or not math.isfinite(role[key]) or not 0 < role[key] <= 300):
                    raise ValueError(f'{name}.{key} must be between 0 and 300')
            total_timeout = role.get('total_timeout_seconds',
                document['llm']['profiles'][role['profile']].get('timeout_seconds', 45))
            if role.get('first_output_timeout_seconds', total_timeout) > total_timeout:
                raise ValueError(f'{name} first output timeout must not exceed total timeout')
            for key in ('model', 'model_env'):
                if key in role and (not isinstance(role[key], str) or not role[key]):
                    raise ValueError(f'Invalid model field for {name}')
            profile = document['llm']['profiles'][role['profile']]
            if profile['provider'] == 'google_vertex':
                google_vertex_url(profile, required=True)
                validate_vertex_model(profile, resolved(role, 'model'))

    def profile(self, domain):
        if domain == 'media':
            return self.media_profiles()['music']
        section = self.document[domain]
        # A selected region is a complete speech stack. Per-domain environment
        # overrides remain available only with explicit (unselected) bindings.
        name = (section['profile'] if self.region and domain in {'asr', 'tts'} else
                os.getenv(domain.upper() + '_PROFILE', section['profile']))
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
    from super_xd.nlp.config.regions import apply_region
    region = os.getenv('SERVICE_REGION', '')
    with path.open('rb') as source:
        return ServiceConfig(apply_region(tomllib.load(source), region), source=path, region=region)
