"""Atomic in-memory vehicle behavior for the local Demo and offline fixtures only."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError

from backend.catalog import TOOLS, SEATS, WINDOWS
from backend.settings import environment_defaults
from backend.speech_config import vehicle_text

_EXECUTORS: dict[str, Callable[[dict, dict], Any]] = {}


def bind(name: str, execute: Callable[[dict, dict], Any]) -> None:
    if name not in TOOLS or name in _EXECUTORS:
        raise ValueError(f"Unknown or duplicate simulator binding: {name}")
    _EXECUTORS[name] = execute


def simple_set(skill_id: str, key: str, arg: str = "enabled", guard: Callable | None = None):
    def run(a: dict, s: dict):
        if guard:
            guard(a, s)
        s[skill_id][key] = a[arg]
        return s[skill_id][key]
    return run


class VehicleRuleError(ValueError):
    def __init__(self, reason, reason_code, resume_condition):
        super().__init__(reason)
        self.reason_code, self.resume_condition = reason_code, resume_condition

def ensure(condition: bool, reason: str, reason_code='vehicle_rejected', resume_condition=None) -> None:
    if not condition:
        raise VehicleRuleError(reason, reason_code, resume_condition)

def stopped(s: dict, reason: str = vehicle_text("rejections.vehicle_moving")) -> None:
    ensure(s.get("_meta", {}).get("speed_kph", 0) == 0, reason, "vehicle_moving", {"speed_kph": 0})

def initial_vehicle_state() -> dict:
    seats = {seat: {"heating": 0, "ventilation": 0, "massage": "off", "fore_aft": 50,
                    "height": 50, "backrest": 105, "lumbar": 50, "cushion_length": 50,
                    "side_support": 0, "headrest": 50, "occupied": seat == "driver"} for seat in SEATS}
    memories = {seat: {str(slot): deepcopy(settings) for slot in range(1, 4)} for seat, settings in seats.items()}
    mirrors = {"left": {"horizontal": 0, "vertical": 0}, "right": {"horizontal": 0, "vertical": 0}}
    return {
        "_meta": {"simulated": True, "speed_kph": 0, "gear": "P", "call_active": False, **environment_defaults(), "charging": False},
        "climate": {"power": False, "temperatures": {"driver": 22, "front_passenger": 22, "rear": 22},
                    "fan_speed": 3, "airflow_mode": "face", "recirculation": "auto", "auto": True,
                    "front_defrost": False, "rear_defrost": False, "air_purifier": False, "fragrance": 0,
                    "humidity_target": 50, "temperature_unit": "celsius", "sync_zones": False,
                    "rear_power": False, "air_quality": {"pm25_ug_m3": 12, "co2_ppm": 650, "simulated": True}},
        "seats": {"positions": seats, "memories": memories, "steering_heating": False,
                  "steering_position": {"reach": 50, "height": 50}, "easy_entry": False},
        "windows": {"positions": {w: 0 for w in WINDOWS}, "child_lock": False,
                    "anti_pinch": {w: True for w in WINDOWS}, "sunroof": 0, "sunshade": 0, "rain_close": False},
        "mirrors": {"positions": mirrors, "folded": False, "heating": False, "reverse_tilt": False,
                    "memories": {str(slot): deepcopy(mirrors) for slot in range(1, 4)}},
        "lights": {"interior": {"front": False, "rear": False}, "ambient": False, "ambient_color": "warm",
                   "ambient_brightness": 40, "headlight_mode": "auto", "high_beam": False,
                   "front_fog": False, "rear_fog": False, "reading": {seat: False for seat in SEATS},
                   "follow_me_home_seconds": 30, "hazard": False},
        "locks": {"doors": {door: True for door in SEATS}, "child_lock": {"left": False, "right": False},
                  "auto_lock": True, "walk_away_lock": False, "charge_port_locked": True},
        "tailgate": {"open": False, "position": 0, "height": 100, "moving": False, "last_action": "idle"},
        "wipers": {"mode": "off", "interval_seconds": 5, "rear": False, "rain_sensitivity": 3,
                   "washer_fluid_pct": 100, "wash_count": 0, "last_wash": None},
        "media": {"volume": 30, "mute": False, "playback": "paused", "source": "bluetooth", "title": vehicle_text("state.initial_track"),
                  "track_index": 1, "radio_frequency_mhz": 98.5, "balance": 0, "fader": 0, "equalizer": "flat",
                  "simulated": True},
        "navigation": {"destination": None, "waypoints": [], "active": False,
                       "preferences": {"avoid_tolls": False, "avoid_highways": False, "prefer_fastest": True},
                       "home": None, "work": None, "simulated": True,
                       "notice": vehicle_text("state.navigation_notice")},
    }

bind('get_climate_state', lambda a, s: s['climate'])

bind('set_climate_power', simple_set('climate', 'power'))

def temperature(a, s, relative=False):
    keys = list(s["climate"]["temperatures"]) if a["zone"] == "all" else [a["zone"]]
    values = {k: s["climate"]["temperatures"][k] + a["delta"] if relative else a["temperature"] for k in keys}
    ensure(all(16 <= value <= 30 for value in values.values()), vehicle_text("rejections.temperature_range"))
    s["climate"]["temperatures"].update(values)
    s["climate"]["power"] = True
    if s["climate"]["sync_zones"] and a["zone"] == "driver":
        s["climate"]["temperatures"] = {k: values["driver"] for k in s["climate"]["temperatures"]}
    return deepcopy(s["climate"]["temperatures"])

bind('set_temperature', temperature)

bind('adjust_temperature', lambda a, s: temperature(a, s, True))

bind('set_fan_speed', simple_set('climate', 'fan_speed', 'level'))

def adjust_fan(a, s):
    value = s["climate"]["fan_speed"] + a["delta"]
    ensure(0 <= value <= 7, vehicle_text("rejections.fan_range"))
    s["climate"]["fan_speed"] = value
    return value

bind('adjust_fan_speed', adjust_fan)

bind('set_airflow_mode', simple_set('climate', 'airflow_mode', 'mode'))

bind('set_recirculation', simple_set('climate', 'recirculation', 'mode'))

bind('set_auto_climate', simple_set('climate', 'auto'))

bind('set_front_defrost', simple_set('climate', 'front_defrost'))

bind('set_rear_defrost', simple_set('climate', 'rear_defrost'))

bind('set_air_purifier', simple_set('climate', 'air_purifier'))

bind('set_fragrance', simple_set('climate', 'fragrance', 'level'))

bind('set_humidity', simple_set('climate', 'humidity_target', 'target'))

bind('set_temperature_unit', simple_set('climate', 'temperature_unit', 'unit'))

def sync_climate(a, s):
    s["climate"]["sync_zones"] = a["enabled"]
    if a["enabled"]:
        s["climate"]["temperatures"] = {key: s["climate"]["temperatures"]["driver"] for key in s["climate"]["temperatures"]}
    return s["climate"]["sync_zones"]

bind('sync_climate_zones', sync_climate)

bind('set_rear_climate_power', simple_set('climate', 'rear_power'))

bind('get_air_quality', lambda a, s: s['climate']['air_quality'])

bind('get_seat_state', lambda a, s: s['seats']['positions'][a['seat']])

def seat_set(key):
    def run(a, s):
        s["seats"]["positions"][a["seat"]][key] = a["mode" if key == "massage" else "level" if key in ["heating", "ventilation", "side_support"] else "occupied" if key == "occupied" else "position"]
        return s["seats"]["positions"][a["seat"]][key]
    return run

for name, key in [('set_seat_heating', 'heating'), ('set_seat_ventilation', 'ventilation')]:
    bind(name, seat_set(key))

bind('set_seat_massage', seat_set('massage'))

def position_seat(a, s, relative=False):
    if a["seat"] == "driver":
        stopped(s, vehicle_text("rejections.driver_seat_moving"))
    key = a["axis"]
    value = s["seats"]["positions"][a["seat"]][key] + a["delta"] if relative else a["position"]
    lower, upper = (80, 140) if key == "backrest" else (0, 100)
    ensure(lower <= value <= upper, vehicle_text("rejections.seat_axis_range", axis=key, lower=str(lower), upper=str(upper)))
    s["seats"]["positions"][a["seat"]][key] = value
    return value

bind('set_seat_position', position_seat)

bind('adjust_seat_position', lambda a, s: position_seat(a, s, True))

def seat_memory(a, s, recall=False):
    if recall:
        stopped(s, vehicle_text("rejections.seat_memory_moving"))
        s["seats"]["positions"][a["seat"]] = deepcopy(s["seats"]["memories"][a["seat"]][str(a["slot"])])
        return s["seats"]["positions"][a["seat"]]
    s["seats"]["memories"][a["seat"]][str(a["slot"])] = deepcopy(s["seats"]["positions"][a["seat"]])
    return s["seats"]["memories"][a["seat"]][str(a["slot"])]

for name, recall in [('save_seat_memory', False), ('recall_seat_memory', True)]:
    bind(name, lambda a, s, recall=recall: seat_memory(a, s, recall))

bind('set_steering_heating', simple_set('seats', 'steering_heating'))

def steering(a, s):
    stopped(s, vehicle_text("rejections.steering_moving"))
    s["seats"]["steering_position"][a["axis"]] = a["position"]
    return s["seats"]["steering_position"]

bind('set_steering_position', steering)

bind('set_easy_entry', simple_set('seats', 'easy_entry'))

bind('set_seat_occupancy', seat_set('occupied'))

bind('get_seat_memory', lambda a, s: s['seats']['memories'][a['seat']][str(a['slot'])])

for name, key, arg, hi in [('set_seat_cushion_length', 'cushion_length', 'position', 100), ('set_seat_side_support', 'side_support', 'level', 5), ('set_seat_headrest', 'headrest', 'position', 100)]:
    bind(name, seat_set(key))

bind('get_window_state', lambda a, s: {'position': s['windows']['positions'][a['window']], 'anti_pinch': s['windows']['anti_pinch'][a['window']]})

def position_window(a, s, relative=False):
    window = a["window"]
    target = s["windows"]["positions"][window] + a["delta"] if relative else a["position"]
    ensure(0 <= target <= 100, vehicle_text("rejections.window_range"))
    ensure(not (s["windows"]["child_lock"] and window.startswith("rear") and target > s["windows"]["positions"][window]), vehicle_text("rejections.rear_window_locked"))
    ensure(not (s.get("_meta", {}).get("speed_kph", 0) > 80 and target > 50), vehicle_text("rejections.window_speed"), "speed_limit", {"speed_kph_max": 80, "position_max_while_driving": 50})
    s["windows"]["positions"][window] = target
    return target

bind('set_window_position', position_window)

bind('adjust_window_position', lambda a, s: position_window(a, s, True))

def all_windows(a, s, target):
    for window in WINDOWS:
        position_window({"window": window, "position": target}, s)
    return s["windows"]["positions"]

bind('close_all_windows', lambda a, s: all_windows(a, s, 0))

bind('open_all_windows', lambda a, s: all_windows(a, s, 100))

bind('set_window_child_lock', simple_set('windows', 'child_lock'))

def anti_pinch(a, s):
    stopped(s, vehicle_text("rejections.anti_pinch_moving"))
    s["windows"]["anti_pinch"][a["window"]] = a["enabled"]
    return a["enabled"]

bind('set_window_anti_pinch', anti_pinch)

bind('set_sunroof_position', simple_set('windows', 'sunroof', 'position', lambda a, s: ensure(not (s.get('_meta', {}).get('speed_kph', 0) > 80 and a['position'] > 50), vehicle_text('rejections.sunroof_speed'), 'speed_limit', {'speed_kph_max': 80, 'position_max_while_driving': 50})))

bind('set_sunshade_position', simple_set('windows', 'sunshade', 'position'))

bind('set_rain_close_windows', simple_set('windows', 'rain_close'))

bind('get_mirror_state', lambda a, s: {**s['mirrors']['positions'][a['mirror']], 'folded': s['mirrors']['folded'], 'heating': s['mirrors']['heating']})

bind('set_mirror_fold', simple_set('mirrors', 'folded', 'folded', lambda a, s: ensure(not (a['folded'] and s.get('_meta', {}).get('speed_kph', 0) > 5), vehicle_text('rejections.mirror_speed'), 'speed_limit', {'speed_kph_max': 5})))

def mirror_angle(a, s, relative=False):
    ensure(not s["mirrors"]["folded"], vehicle_text("rejections.mirror_folded"))
    pos = s["mirrors"]["positions"][a["mirror"]]
    if relative:
        target = pos[a["axis"]] + a["delta"]
        ensure(-20 <= target <= 20, vehicle_text("rejections.mirror_range"))
        pos[a["axis"]] = target
    else:
        pos.update(horizontal=a["horizontal"], vertical=a["vertical"])
    return pos

bind('set_mirror_angle', mirror_angle)

bind('adjust_mirror_angle', lambda a, s: mirror_angle(a, s, True))

bind('set_mirror_heating', simple_set('mirrors', 'heating'))

bind('set_reverse_mirror_tilt', simple_set('mirrors', 'reverse_tilt'))

def mirror_memory(a, s, recall=False):
    if recall:
        ensure(not s["mirrors"]["folded"], vehicle_text("rejections.mirror_folded"))
        s["mirrors"]["positions"] = deepcopy(s["mirrors"]["memories"][str(a["slot"])])
        return s["mirrors"]["positions"]
    s["mirrors"]["memories"][str(a["slot"])] = deepcopy(s["mirrors"]["positions"])
    return s["mirrors"]["memories"][str(a["slot"])]

for name, recall in [('save_mirror_memory', False), ('recall_mirror_memory', True)]:
    bind(name, lambda a, s, recall=recall: mirror_memory(a, s, recall))

bind('get_light_state', lambda a, s: s['lights'])

def interior(a, s):
    zones = ["front", "rear"] if a["zone"] == "all" else [a["zone"]]
    for zone in zones:
        s["lights"]["interior"][zone] = a["enabled"]
    return s["lights"]["interior"]

bind('set_interior_light', interior)

bind('set_ambient_light', simple_set('lights', 'ambient'))

bind('set_ambient_color', simple_set('lights', 'ambient_color', 'color'))

bind('set_ambient_brightness', simple_set('lights', 'ambient_brightness', 'brightness'))

bind('set_headlight_mode', simple_set('lights', 'headlight_mode', 'mode'))

bind('set_high_beam', simple_set('lights', 'high_beam'))

bind('set_front_fog_lights', simple_set('lights', 'front_fog'))

bind('set_rear_fog_light', simple_set('lights', 'rear_fog'))

def reading_light(a, s):
    s["lights"]["reading"][a["seat"]] = a["enabled"]
    return a["enabled"]

bind('set_reading_light', reading_light)

bind('set_follow_me_home', simple_set('lights', 'follow_me_home_seconds', 'seconds'))

bind('set_hazard_lights', simple_set('lights', 'hazard'))

bind('get_lock_state', lambda a, s: s['locks'])

def door_lock(a, s):
    if not a["locked"]:
        stopped(s, vehicle_text("rejections.unlock_moving"))
    doors = SEATS if a["door"] == "all" else [a["door"]]
    for door in doors:
        s["locks"]["doors"][door] = a["locked"]
    return s["locks"]["doors"]

bind('set_door_lock', door_lock)

bind('lock_all_doors', lambda a, s: door_lock({'door': 'all', 'locked': True}, s))

bind('unlock_all_doors', lambda a, s: door_lock({'door': 'all', 'locked': False}, s))

def child_lock(a, s):
    stopped(s, vehicle_text("rejections.child_lock_moving"))
    s["locks"]["child_lock"][a["side"]] = a["enabled"]
    return a["enabled"]

bind('set_child_lock', child_lock)

bind('set_auto_lock', simple_set('locks', 'auto_lock'))

bind('set_walk_away_lock', simple_set('locks', 'walk_away_lock'))

bind('set_charge_port_lock', simple_set('locks', 'charge_port_locked', 'locked', lambda a, s: ensure(not (not a['locked'] and s.get('_meta', {}).get('charging', False)), vehicle_text('rejections.charge_port_busy'))))

bind('get_tailgate_state', lambda a, s: s['tailgate'])

def tailgate_open(a, s):
    if a["open"]:
        stopped(s, vehicle_text("rejections.tailgate_moving"))
    s["tailgate"].update(open=a["open"], position=s["tailgate"]["height"] if a["open"] else 0,
                         moving=False, last_action="opened" if a["open"] else "closed")
    return s["tailgate"]

bind('set_tailgate_open', tailgate_open)

bind('set_tailgate_height', simple_set('tailgate', 'height', 'height', lambda a, s: stopped(s)))

def stop_tailgate(a, s):
    s["tailgate"].update(moving=False, last_action="stopped")
    return s["tailgate"]

bind('stop_tailgate', stop_tailgate)

bind('get_wiper_state', lambda a, s: s['wipers'])

bind('set_wiper_mode', simple_set('wipers', 'mode', 'mode'))

bind('set_wiper_interval', simple_set('wipers', 'interval_seconds', 'seconds'))

def wash(a, s):
    ensure(s["wipers"]["washer_fluid_pct"] > 0, vehicle_text("rejections.washer_empty"))
    s["wipers"]["washer_fluid_pct"] -= 1
    s["wipers"]["wash_count"] += 1
    s["wipers"]["last_wash"] = a["target"]
    return {"target": a["target"], "washer_fluid_pct": s["wipers"]["washer_fluid_pct"], "wash_count": s["wipers"]["wash_count"]}

bind('wash_windshield', wash)

bind('set_rear_wiper', simple_set('wipers', 'rear'))

bind('set_rain_sensitivity', simple_set('wipers', 'rain_sensitivity', 'level'))

bind('get_media_state', lambda a, s: s['media'])

bind('set_volume', simple_set('media', 'volume', 'volume'))

def adjust_volume(a, s):
    value = s["media"]["volume"] + a["delta"]
    ensure(0 <= value <= 100, vehicle_text("rejections.volume_range"))
    s["media"]["volume"] = value
    return value

bind('adjust_volume', adjust_volume)

bind('set_mute', simple_set('media', 'mute'))

def playback(a, s):
    action = a["action"]
    if action in ["next", "previous"]:
        s["media"]["track_index"] = max(1, s["media"]["track_index"] + (1 if action == "next" else -1))
        s["media"]["title"] = vehicle_text("state.track", index=str(s["media"]["track_index"]))
        s["media"]["playback"] = "playing"
    else:
        s["media"]["playback"] = {"play": "playing", "pause": "paused", "stop": "stopped"}[action]
    return {"playback": s["media"]["playback"], "title": s["media"]["title"]}

bind('control_playback', playback)

bind('set_media_source', simple_set('media', 'source', 'source'))

def play_media(a, s):
    s["media"].update(title=a["title"].strip(), playback="playing")
    return {"title": s["media"]["title"], "playback": s["media"]["playback"], "simulated": True}

bind('play_media', play_media)

bind('set_radio_frequency', simple_set('media', 'radio_frequency_mhz', 'frequency'))

def balance(a, s):
    s["media"].update(balance=a["balance"], fader=a["fader"])
    return {"balance": a["balance"], "fader": a["fader"]}

bind('set_audio_balance', balance)

bind('set_equalizer', simple_set('media', 'equalizer', 'preset'))

bind('get_navigation_state', lambda a, s: s['navigation'])

def destination(a, s):
    s['navigation'].pop('destination_place', None)
    s["navigation"].update(destination=a["destination"].strip(), active=True)
    return {"destination": s["navigation"]["destination"], "simulated": True, "notice": s["navigation"]["notice"]}

bind('set_navigation_destination', destination)

def add_waypoint(a, s):
    ensure(len(s["navigation"]["waypoints"]) < 10, vehicle_text("rejections.waypoint_limit"))
    s["navigation"]["waypoints"].append(a["destination"].strip())
    return s["navigation"]["waypoints"]

bind('add_navigation_waypoint', add_waypoint)

def remove_waypoint(a, s):
    ensure(a["index"] < len(s["navigation"]["waypoints"]), vehicle_text("rejections.waypoint_missing"))
    removed = s["navigation"]["waypoints"].pop(a["index"])
    return {"removed": removed, "waypoints": s["navigation"]["waypoints"]}

bind('remove_navigation_waypoint', remove_waypoint)

def preferences(a, s):
    s["navigation"]["preferences"] = deepcopy(a)
    return {"preferences": a, "simulated": True, "notice": vehicle_text("state.preferences_notice")}

bind('set_navigation_preferences', preferences)

def cancel_navigation(a, s):
    s['navigation'].pop('destination_place', None)
    s["navigation"].update(active=False, destination=None, waypoints=[])
    return s["navigation"]

bind('cancel_navigation', cancel_navigation)

bind('set_home_destination', simple_set('navigation', 'home', 'destination'))

bind('set_work_destination', simple_set('navigation', 'work', 'destination'))

_VALIDATORS = {name: Draft202012Validator(tool.parameters) for name, tool in TOOLS.items()}

def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True

def execute_tool(name: str, arguments: dict, state: dict) -> dict:
    """Validate, execute atomically and return an independent structured receipt."""
    result = {"ok": False, "name": name, "arguments": deepcopy(arguments)}
    try:
        ensure(name in TOOLS, "未知车控函数")
        ensure(isinstance(arguments, dict), "参数必须是JSON对象")
        ensure(_finite(arguments), "参数包含非有限数值")
        _VALIDATORS[name].validate(arguments)
        candidate = deepcopy(state)
        value = _EXECUTORS[name](arguments, candidate)
        ensure(_finite(candidate), "模拟状态包含非有限数值")
        state.clear()
        state.update(candidate)
        result.update(ok=True, value=deepcopy(value))
    except ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "arguments"
        result["reason"] = f"参数校验失败（{path}）：{exc.message}"
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        result["reason"] = str(exc) or vehicle_text("rejections.default")
        result["reason_code"] = getattr(exc, "reason_code", "vehicle_rejected")
        result["resume_condition"] = getattr(exc, "resume_condition", None)
    result["state"] = deepcopy(state)
    return result

assert set(TOOLS) == set(_EXECUTORS), "Every vehicle schema requires an explicit Demo binding"
