"""Pure vehicle schemas, skills and declared action effects for NLP and adapters.

Device validation and execution belong to the selected external device adapter.
The local simulator is implemented separately in backend.demo.vehicle_simulator.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

from .types import SkillManifest, ToolSpec
from .prompt_config import config_text, validate_config

TOOLS: dict[str, ToolSpec] = {}
SKILLS: dict[str, SkillManifest] = {}
SEATS = ["driver", "front_passenger", "rear_left", "rear_right"]
WINDOWS = SEATS
ZONES = ["all", "driver", "front_passenger", "rear"]
MIRRORS = ["left", "right"]


def enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


def number(lo: float, hi: float, integer: bool = False) -> dict:
    return {"type": "integer" if integer else "number", "minimum": lo, "maximum": hi}


def boolean() -> dict:
    return {"type": "boolean"}


def string(max_length: int = 200) -> dict:
    # Also permit whole-string regex decoding in providers' strict mode. A bare
    # \S means "contains nonspace" to JSON Schema but can constrain decoding to
    # one character; this equivalent pattern explicitly spans the entire value.
    return {"type": "string", "minLength": 1, "maxLength": max_length, "pattern": r"[\s\S]*\S[\s\S]*"}


SEAT = enum(SEATS)
WINDOW = enum(WINDOWS)
ZONE = enum(ZONES)
MIRROR = enum(MIRRORS)
SLOT = number(1, 3, True)


def skill(id: str) -> None:
    SKILLS[id] = SkillManifest(id=id, name=config_text("vehicle/functions.toml", f"skills.{id}.name"),
                               description=config_text("vehicle/functions.toml", f"skills.{id}.description"),
                               tool_names=[])


def register(skill_id: str, name: str, props: dict, example: dict,
             paths: list[str], operation: str = "set") -> None:
    """Register a vehicle contract without importing an implementation."""
    if name in TOOLS:
        raise ValueError(f"duplicate function {name}")
    properties = deepcopy(props)
    for argument, parameter in properties.items():
        parameter["description"] = config_text("vehicle/functions.toml", f"functions.{name}.parameters.{argument}")
    schema = {"type": "object", "properties": properties, "required": list(props), "additionalProperties": False}
    Draft202012Validator.check_schema(schema)
    TOOLS[name] = ToolSpec(name=name, description=config_text("vehicle/functions.toml", f"functions.{name}.description"),
                           parameters=schema, skill_id=skill_id,
                           state_mapping={"paths": paths, "operation": operation}, example=example)
    SKILLS[skill_id].tool_names.append(name)


def set_simple(skill_id: str, name: str, key: str, prop: dict,
               example_value: Any, arg: str = "enabled") -> None:
    register(skill_id, name, {arg: prop}, {arg: example_value}, [f"{skill_id}.{key}"])


def get_group(skill_id: str, name: str) -> None:
    register(skill_id, name, {}, {}, [skill_id], "read")


# 空调与空气管理：18
skill("climate")
get_group("climate", "get_climate_state")
set_simple('climate', 'set_climate_power', 'power', boolean(), True)


register('climate', 'set_temperature', {'zone': ZONE, 'temperature': number(16, 30)}, {'zone': 'all', 'temperature': 24}, ['climate.temperatures.{zone}', 'climate.power'])
register('climate', 'adjust_temperature', {'zone': ZONE, 'delta': number(-5, 5)}, {'zone': 'driver', 'delta': -1}, ['climate.temperatures.{zone}', 'climate.power'], 'adjust')
set_simple('climate', 'set_fan_speed', 'fan_speed', number(0, 7, True), 4, 'level')


register('climate', 'adjust_fan_speed', {'delta': number(-3, 3, True)}, {'delta': 1}, ['climate.fan_speed'], 'adjust')
set_simple('climate', 'set_airflow_mode', 'airflow_mode', enum(['face', 'feet', 'defrost', 'mixed']), 'feet', 'mode')
set_simple('climate', 'set_recirculation', 'recirculation', enum(['internal', 'external', 'auto']), 'internal', 'mode')
set_simple('climate', 'set_auto_climate', 'auto', boolean(), True)
set_simple('climate', 'set_front_defrost', 'front_defrost', boolean(), True)
set_simple('climate', 'set_rear_defrost', 'rear_defrost', boolean(), True)
set_simple('climate', 'set_air_purifier', 'air_purifier', boolean(), True)
set_simple('climate', 'set_fragrance', 'fragrance', number(0, 3, True), 1, 'level')
set_simple('climate', 'set_humidity', 'humidity_target', number(30, 70, True), 45, 'target')
set_simple('climate', 'set_temperature_unit', 'temperature_unit', enum(['celsius', 'fahrenheit']), 'fahrenheit', 'unit')


register('climate', 'sync_climate_zones', {'enabled': boolean()}, {'enabled': True}, ['climate.sync_zones', 'climate.temperatures'])
set_simple('climate', 'set_rear_climate_power', 'rear_power', boolean(), True)
register('climate', 'get_air_quality', {}, {}, ['climate.air_quality'], 'read')

# 座椅与舒适：16
skill("seats")
register('seats', 'get_seat_state', {'seat': SEAT}, {'seat': 'driver'}, ['seats.positions.{seat}'], 'read')


for name, key in [('set_seat_heating', 'heating'), ('set_seat_ventilation', 'ventilation')]:
    register('seats', name, {'seat': SEAT, 'level': number(0, 3, True)}, {'seat': 'driver', 'level': 2}, [f'seats.positions.{{seat}}.{key}'])
register('seats', 'set_seat_massage', {'seat': SEAT, 'mode': enum(['off', 'relax', 'lumbar', 'wave'])}, {'seat': 'driver', 'mode': 'relax'}, ['seats.positions.{seat}.massage'])
AXIS = enum(["fore_aft", "height", "backrest", "lumbar"])


register('seats', 'set_seat_position', {'seat': SEAT, 'axis': AXIS, 'position': number(0, 140)}, {'seat': 'driver', 'axis': 'fore_aft', 'position': 60}, ['seats.positions.{seat}.{axis}'])
register('seats', 'adjust_seat_position', {'seat': SEAT, 'axis': AXIS, 'delta': number(-10, 10)}, {'seat': 'driver', 'axis': 'height', 'delta': 5}, ['seats.positions.{seat}.{axis}'], 'adjust')


for name, recall in [('save_seat_memory', False), ('recall_seat_memory', True)]:
    register('seats', name, {'seat': SEAT, 'slot': SLOT}, {'seat': 'driver', 'slot': 1}, ['seats.memories.{seat}.{slot}', 'seats.positions.{seat}'])
set_simple('seats', 'set_steering_heating', 'steering_heating', boolean(), True)


register('seats', 'set_steering_position', {'axis': enum(['reach', 'height']), 'position': number(0, 100, True)}, {'axis': 'reach', 'position': 60}, ['seats.steering_position.{axis}'])
set_simple('seats', 'set_easy_entry', 'easy_entry', boolean(), True)
register('seats', 'set_seat_occupancy', {'seat': SEAT, 'occupied': boolean()}, {'seat': 'rear_left', 'occupied': True}, ['seats.positions.{seat}.occupied'])
register('seats', 'get_seat_memory', {'seat': SEAT, 'slot': SLOT}, {'seat': 'driver', 'slot': 1}, ['seats.memories.{seat}.{slot}'], 'read')
for name, key, arg, hi in [('set_seat_cushion_length', 'cushion_length', 'position', 100), ('set_seat_side_support', 'side_support', 'level', 5), ('set_seat_headrest', 'headrest', 'position', 100)]:
    register('seats', name, {'seat': SEAT, arg: number(0, hi, True)}, {'seat': 'driver', arg: min(2, hi)}, [f'seats.positions.{{seat}}.{key}'])

# 车窗：10
skill("windows")
register('windows', 'get_window_state', {'window': WINDOW}, {'window': 'driver'}, ['windows.positions.{window}', 'windows.anti_pinch.{window}'], 'read')


register('windows', 'set_window_position', {'window': WINDOW, 'position': number(0, 100, True)}, {'window': 'driver', 'position': 50}, ['windows.positions.{window}'])
register('windows', 'adjust_window_position', {'window': WINDOW, 'delta': number(-100, 100, True)}, {'window': 'driver', 'delta': 20}, ['windows.positions.{window}'], 'adjust')


register('windows', 'close_all_windows', {}, {}, ['windows.positions.*'])
register('windows', 'open_all_windows', {}, {}, ['windows.positions.*'])
set_simple('windows', 'set_window_child_lock', 'child_lock', boolean(), True)


register('windows', 'set_window_anti_pinch', {'window': WINDOW, 'enabled': boolean()}, {'window': 'driver', 'enabled': True}, ['windows.anti_pinch.{window}'])
set_simple('windows', 'set_sunroof_position', 'sunroof', number(0, 100, True), 30, 'position')
set_simple('windows', 'set_sunshade_position', 'sunshade', number(0, 100, True), 50, 'position')
set_simple('windows', 'set_rain_close_windows', 'rain_close', boolean(), True)

# 后视镜：8
skill("mirrors")
register('mirrors', 'get_mirror_state', {'mirror': MIRROR}, {'mirror': 'left'}, ['mirrors.positions.{mirror}', 'mirrors.folded', 'mirrors.heating'], 'read')
set_simple('mirrors', 'set_mirror_fold', 'folded', boolean(), True, 'folded')


register('mirrors', 'set_mirror_angle', {'mirror': MIRROR, 'horizontal': number(-20, 20), 'vertical': number(-20, 20)}, {'mirror': 'left', 'horizontal': 2, 'vertical': -3}, ['mirrors.positions.{mirror}'])
register('mirrors', 'adjust_mirror_angle', {'mirror': MIRROR, 'axis': enum(['horizontal', 'vertical']), 'delta': number(-10, 10)}, {'mirror': 'left', 'axis': 'vertical', 'delta': -2}, ['mirrors.positions.{mirror}.{axis}'], 'adjust')
set_simple('mirrors', 'set_mirror_heating', 'heating', boolean(), True)
set_simple('mirrors', 'set_reverse_mirror_tilt', 'reverse_tilt', boolean(), True)


for name, recall in [('save_mirror_memory', False), ('recall_mirror_memory', True)]:
    register('mirrors', name, {'slot': SLOT}, {'slot': 1}, ['mirrors.memories.{slot}', 'mirrors.positions'])

# 灯光：12
skill("lights")
get_group("lights", "get_light_state")


register('lights', 'set_interior_light', {'zone': enum(['all', 'front', 'rear']), 'enabled': boolean()}, {'zone': 'all', 'enabled': True}, ['lights.interior.{zone}'])
set_simple('lights', 'set_ambient_light', 'ambient', boolean(), True)
set_simple('lights', 'set_ambient_color', 'ambient_color', enum(['white', 'warm', 'red', 'blue', 'green', 'purple']), 'blue', 'color')
set_simple('lights', 'set_ambient_brightness', 'ambient_brightness', number(0, 100, True), 60, 'brightness')
set_simple('lights', 'set_headlight_mode', 'headlight_mode', enum(['off', 'auto', 'low']), 'auto', 'mode')
set_simple('lights', 'set_high_beam', 'high_beam', boolean(), True)
set_simple('lights', 'set_front_fog_lights', 'front_fog', boolean(), True)
set_simple('lights', 'set_rear_fog_light', 'rear_fog', boolean(), True)


register('lights', 'set_reading_light', {'seat': SEAT, 'enabled': boolean()}, {'seat': 'rear_left', 'enabled': True}, ['lights.reading.{seat}'])
set_simple('lights', 'set_follow_me_home', 'follow_me_home_seconds', number(0, 120, True), 60, 'seconds')
set_simple('lights', 'set_hazard_lights', 'hazard', boolean(), True)

# 门锁：8
skill("locks")
get_group("locks", "get_lock_state")


register('locks', 'set_door_lock', {'door': enum(['all', *SEATS]), 'locked': boolean()}, {'door': 'driver', 'locked': False}, ['locks.doors.{door}'])
register('locks', 'lock_all_doors', {}, {}, ['locks.doors.*'])
register('locks', 'unlock_all_doors', {}, {}, ['locks.doors.*'])


register('locks', 'set_child_lock', {'side': enum(['left', 'right']), 'enabled': boolean()}, {'side': 'left', 'enabled': True}, ['locks.child_lock.{side}'])
set_simple('locks', 'set_auto_lock', 'auto_lock', boolean(), True)
set_simple('locks', 'set_walk_away_lock', 'walk_away_lock', boolean(), True)
set_simple('locks', 'set_charge_port_lock', 'charge_port_locked', boolean(), False, 'locked')

# 尾门：4
skill("tailgate")
get_group("tailgate", "get_tailgate_state")


register('tailgate', 'set_tailgate_open', {'open': boolean()}, {'open': True}, ['tailgate.open', 'tailgate.position', 'tailgate.moving', 'tailgate.last_action'])
set_simple('tailgate', 'set_tailgate_height', 'height', number(20, 100, True), 80, 'height')


register('tailgate', 'stop_tailgate', {}, {}, ['tailgate.moving', 'tailgate.last_action'])

# 雨刷清洗：6
skill("wipers")
get_group("wipers", "get_wiper_state")
set_simple('wipers', 'set_wiper_mode', 'mode', enum(['off', 'auto', 'intermittent', 'low', 'high']), 'auto', 'mode')
set_simple('wipers', 'set_wiper_interval', 'interval_seconds', number(1, 15, True), 3, 'seconds')


register('wipers', 'wash_windshield', {'target': enum(['front', 'rear'])}, {'target': 'front'}, ['wipers.washer_fluid_pct', 'wipers.wash_count', 'wipers.last_wash'])
set_simple('wipers', 'set_rear_wiper', 'rear', boolean(), True)
set_simple('wipers', 'set_rain_sensitivity', 'rain_sensitivity', number(1, 5, True), 4, 'level')

# 媒体：10
skill("media")
get_group("media", "get_media_state")
set_simple('media', 'set_volume', 'volume', number(0, 100, True), 40, 'volume')


register('media', 'adjust_volume', {'delta': number(-30, 30, True)}, {'delta': 5}, ['media.volume'], 'adjust')
set_simple('media', 'set_mute', 'mute', boolean(), True)


register('media', 'control_playback', {'action': enum(['play', 'pause', 'stop', 'next', 'previous'])}, {'action': 'pause'}, ['media.playback', 'media.track_index', 'media.title'])
set_simple('media', 'set_media_source', 'source', enum(['bluetooth', 'radio', 'usb', 'online']), 'radio', 'source')


register('media', 'play_media', {'title': string()}, {'title': '夜曲'}, ['media.title', 'media.playback'])
set_simple('media', 'set_radio_frequency', 'radio_frequency_mhz', number(87.5, 108), 101.7, 'frequency')


register('media', 'set_audio_balance', {'balance': number(-10, 10, True), 'fader': number(-10, 10, True)}, {'balance': 0, 'fader': -2}, ['media.balance', 'media.fader'])
set_simple('media', 'set_equalizer', 'equalizer', enum(['flat', 'pop', 'rock', 'classical', 'voice']), 'voice', 'preset')

# 导航：8
skill("navigation")
get_group("navigation", "get_navigation_state")


register('navigation', 'set_navigation_destination', {'destination': string()}, {'destination': '巴黎卢浮宫'}, ['navigation.destination', 'navigation.active', 'navigation.destination_place'])


register('navigation', 'add_navigation_waypoint', {'destination': string()}, {'destination': '附近充电站'}, ['navigation.waypoints'], 'append')


register('navigation', 'remove_navigation_waypoint', {'index': number(0, 9, True)}, {'index': 0}, ['navigation.waypoints'], 'remove')


register('navigation', 'set_navigation_preferences', {'avoid_tolls': boolean(), 'avoid_highways': boolean(), 'prefer_fastest': boolean()}, {'avoid_tolls': True, 'avoid_highways': False, 'prefer_fastest': True}, ['navigation.preferences'])


register('navigation', 'cancel_navigation', {}, {}, ['navigation.active', 'navigation.destination', 'navigation.waypoints', 'navigation.destination_place'])
set_simple('navigation', 'set_home_destination', 'home', string(), '巴黎市中心', 'destination')
set_simple('navigation', 'set_work_destination', 'work', string(), '拉德芳斯', 'destination')

HOT_TOOL_NAMES = [
    "set_climate_power", "set_temperature", "set_fan_speed", "set_window_position", "close_all_windows",
    "set_seat_heating", "set_interior_light", "set_volume", "control_playback", "set_navigation_destination",
    "adjust_temperature", "set_seat_ventilation", "set_seat_massage", "set_seat_position", "set_ambient_light",
]
# This partition is a capability boundary, not query-based tool retrieval.
# Only the delegated Pro vehicle planner may obtain the remaining schemas.
COLD_TOOL_NAMES = [name for name in TOOLS if name not in HOT_TOOL_NAMES]
EXPECTED_COUNTS = {"climate": 18, "seats": 16, "windows": 10, "mirrors": 8, "lights": 12, "locks": 8, "tailgate": 4, "wipers": 6, "media": 10, "navigation": 8}
assert len(TOOLS) == 100 and len(SKILLS) == 10
assert len(HOT_TOOL_NAMES) == 15 and len(set(HOT_TOOL_NAMES)) == 15 and len(COLD_TOOL_NAMES) == 85
assert {key: len(value.tool_names) for key, value in SKILLS.items()} == EXPECTED_COUNTS
_VEHICLE_TEXT_EXPECTED = {
    f"skills.{skill_id}.{field}": set()
    for skill_id in SKILLS for field in ("name", "description")
}
for _name, _tool in TOOLS.items():
    _VEHICLE_TEXT_EXPECTED[f"functions.{_name}.description"] = set()
    for _parameter in _tool.parameters["properties"]:
        _VEHICLE_TEXT_EXPECTED[f"functions.{_name}.parameters.{_parameter}"] = set()
validate_config("vehicle/functions.toml", _VEHICLE_TEXT_EXPECTED)


def action_write_paths(name, arguments, state):
    """Expand declared effects against supplied facts without predicting results.

    A held path conflicts even when a requested assignment would be a no-op.
    Synchronized driver temperature affects the complete temperature subtree.
    """
    if TOOLS[name].state_mapping.get('operation') == 'read':
        return []
    paths = []
    for template in TOOLS[name].state_mapping['paths']:
        nodes = [('', state)]
        for part in template.split('.'):
            expanded = []
            for prefix, node in nodes:
                key = arguments.get(part[1:-1]) if part.startswith('{') and part.endswith('}') else part
                keys = list(node) if key in ('*', 'all') and isinstance(node, dict) else [str(key)]
                for child in keys:
                    expanded.append((f'{prefix}.{child}' if prefix else child,
                                     node.get(child) if isinstance(node, dict) else None))
            nodes = expanded
        paths.extend(path for path, _ in nodes)
    if (name in {'set_temperature', 'adjust_temperature'} and arguments.get('zone') == 'driver'
            and state.get('climate', {}).get('sync_zones') is True):
        paths.append('climate.temperatures')
    return paths


def load_skill(skill_id: str) -> dict:
    if skill_id not in SKILLS:
        return {"ok": False, "reason": "未知skill_id", "available_skills": list(SKILLS)}
    manifest = SKILLS[skill_id]
    return {"ok": True, "skill": manifest.model_dump(), "tools": [TOOLS[name].model_dump() for name in manifest.tool_names]}
