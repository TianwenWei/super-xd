"""Startup-only regional bindings; never infer location or fail over providers."""
from copy import deepcopy
import tomllib

from super_xd.nlp.defaults import ROOT


REGIONS_PATH = ROOT / 'config/regions.toml'


def apply_region(document, region, *, presets=None):
    if not region:
        return deepcopy(document)
    if presets is None:
        with REGIONS_PATH.open('rb') as source:
            presets = tomllib.load(source)
    if (not isinstance(presets, dict) or set(presets) != {'version', 'regions'}
            or type(presets['version']) is not int or presets['version'] != 1
            or not isinstance(presets['regions'], dict)):
        raise ValueError('Regional presets must declare version=1 and regions')
    if region not in presets['regions']:
        raise ValueError('Unknown SERVICE_REGION; choose europe or china, or unset it for explicit bindings')
    selected = presets['regions'][region]
    if (not isinstance(selected, dict) or set(selected) != {'llm', 'asr', 'tts'}
            or not isinstance(selected['llm'], dict)
            or set(selected['llm']) != {'talker', 'planner', 'thinker', 'finalizer'}):
        raise ValueError('A region must bind all four model roles, ASR and TTS')
    result = deepcopy(document)
    for role, binding in selected['llm'].items():
        if (not isinstance(binding, dict) or set(binding) not in ({'profile', 'model'}, {'profile', 'model_env'})
                or any(not isinstance(value, str) or not value for value in binding.values())):
            raise ValueError('Regional model bindings require profile and exactly one model selector')
        target = result['llm']['roles'][role]
        target.pop('model', None)
        target.pop('model_env', None)
        target.update(binding)
    for domain in ('asr', 'tts'):
        if not isinstance(selected[domain], str) or not selected[domain]:
            raise ValueError('Regional audio bindings must name profiles')
        result[domain]['profile'] = selected[domain]
    return result
