"""Cached prose projections of structural catalogs; no provider or device IO."""
from copy import deepcopy

from super_xd.nlp.catalog import vehicle, cockpit, maps, manual, web, control
from super_xd.nlp.prompts.config import config_text, validate_config
from super_xd.nlp.prompts.tools import TOOL_TEXT_KEYS, tool_text


TOOLS = {name: spec.model_copy(deep=True) for name, spec in vehicle.TOOLS.items()}
SKILLS = {name: skill.model_copy(deep=True) for name, skill in vehicle.SKILLS.items()}
for skill_id, manifest in SKILLS.items():
    manifest.name = config_text('vehicle/functions.toml', f'skills.{skill_id}.name')
    manifest.description = config_text('vehicle/functions.toml', f'skills.{skill_id}.description')
for name, spec in TOOLS.items():
    spec.description = config_text('vehicle/functions.toml', f'functions.{name}.description')
    for argument, parameter in spec.parameters['properties'].items():
        parameter['description'] = config_text('vehicle/functions.toml', f'functions.{name}.parameters.{argument}')

_VEHICLE_TEXT_EXPECTED = {
    f"skills.{skill_id}.{field}": set()
    for skill_id in SKILLS for field in ("name", "description")
}
for _name, _tool in TOOLS.items():
    _VEHICLE_TEXT_EXPECTED[f"functions.{_name}.description"] = set()
    for _parameter in _tool.parameters["properties"]:
        _VEHICLE_TEXT_EXPECTED[f"functions.{_name}.parameters.{_parameter}"] = set()
validate_config("vehicle/functions.toml", _VEHICLE_TEXT_EXPECTED)


def _compile_tools(definitions, *, cockpit_text=False):
    result = deepcopy(definitions)
    for tool in result:
        function = tool['function']
        name = function['name']
        function['description'] = (config_text('cockpit/tools.toml', name + '.description')
                                   if cockpit_text else tool_text(name + '.description'))
        for argument, parameter in function['parameters']['properties'].items():
            key = f'{name}.parameters.{argument}.description'
            if key in TOOL_TEXT_KEYS:
                parameter['description'] = tool_text(key)
    return result


validate_config('cockpit/tools.toml', {name + '.description': set() for name in cockpit.TOOL_KEYS})
COCKPIT_TOOLS = _compile_tools(cockpit.COCKPIT_TOOLS, cockpit_text=True)
MAPS_TOOLS = _compile_tools(maps.MAPS_TOOLS)
MANUAL_TOOLS = _compile_tools(manual.MANUAL_TOOLS)
WEB_TOOLS = _compile_tools(web.WEB_TOOLS)

FEEDBACK_KEYS = ('not_configured', 'unknown_content', 'unknown_place', 'place_not_confirmed', 'no_content', 'queue_boundary',
                 'invalid_action', 'device_failed', 'device_timeout', 'stale_receipt', 'video_driving', 'media_done', 'navigation_done', 'route_not_configured', 'origin_required', 'unsupported_coordinate_system', 'navigation_failed', 'route_failed', 'unsupported_capability')
validate_config('cockpit/feedback.toml', {key: set() for key in FEEDBACK_KEYS})


def cockpit_feedback(key):
    return config_text('cockpit/feedback.toml', key)

def load_skill(skill_id: str) -> dict:
    if skill_id not in SKILLS:
        return {"ok": False, "reason": "未知skill_id", "available_skills": list(SKILLS)}
    manifest = SKILLS[skill_id]
    return {"ok": True, "skill": manifest.model_dump(), "tools": [TOOLS[name].model_dump() for name in manifest.tool_names]}


def _resolve_control_text(value):
    if isinstance(value, list):
        return [_resolve_control_text(item) for item in value]
    if isinstance(value, dict):
        return {key: tool_text(item) if key == 'description' else _resolve_control_text(item)
                for key, item in value.items()}
    return deepcopy(value)


CONTROL_TOOLS = _resolve_control_text(control.CONTROL_TOOLS)
CONTROL_BY_NAME = {tool['function']['name']: tool for tool in CONTROL_TOOLS}
