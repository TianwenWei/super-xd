"""Single source of truth for 100 explicitly simulated vehicle functions.

Every mutation is validated against JSON Schema, performed on a private copy,
and committed only if the complete command succeeds. This module has no network
or hardware access. The browser acknowledgement is handled by the session actor.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError

from .types import SkillManifest, ToolSpec
from .settings import environment_defaults
from .prompt_config import config_text, validate_config

TOOLS: dict[str, ToolSpec] = {}
SKILLS: dict[str, SkillManifest] = {}
_EXECUTORS: dict[str, Callable[[dict, dict], Any]] = {}
SEATS = ["driver", "front_passenger", "rear_left", "rear_right"]
WINDOWS = SEATS
ZONES = ["all", "driver", "front_passenger", "rear"]
MIRRORS = ["left", "right"]

_VEHICLE_SPEECH_EXPECTED = {
    key: set() for key in (
        "rejections.vehicle_moving", "rejections.temperature_range", "rejections.fan_range",
        "rejections.steering_moving", "rejections.window_range", "rejections.rear_window_locked",
        "rejections.window_speed", "rejections.anti_pinch_moving", "rejections.mirror_folded",
        "rejections.child_lock_moving", "rejections.washer_empty", "rejections.volume_range",
        "rejections.waypoint_limit", "rejections.waypoint_missing", "rejections.driver_seat_moving",
        "rejections.seat_memory_moving", "rejections.sunroof_speed", "rejections.mirror_speed",
        "rejections.mirror_range", "rejections.unlock_moving", "rejections.charge_port_busy",
        "rejections.tailgate_moving", "rejections.default", "state.preferences_notice",
        "state.initial_track", "state.navigation_notice",
        "speaker.driver", "speaker.front_passenger", "speaker.rear_left", "speaker.rear_right",
    )
}
_VEHICLE_SPEECH_EXPECTED.update({
    "rejections.seat_axis_range": {"axis", "lower", "upper"},
    "state.track": {"index"},
})
validate_config("speech/vehicle.toml", _VEHICLE_SPEECH_EXPECTED)


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
             execute: Callable[[dict, dict], Any], paths: list[str], operation: str = "set") -> None:
    if name in TOOLS:
        raise ValueError(f"duplicate function {name}")
    properties = deepcopy(props)
    for argument, parameter in properties.items():
        parameter["description"] = config_text("vehicle/functions.toml", f"functions.{name}.parameters.{argument}")
    schema = {"type": "object", "properties": properties, "required": list(props), "additionalProperties": False}
    Draft202012Validator.check_schema(schema)
    TOOLS[name] = ToolSpec(name=name, description=config_text("vehicle/functions.toml", f"functions.{name}.description"), parameters=schema,
                           skill_id=skill_id, state_mapping={"paths": paths, "operation": operation, "simulated": True},
                           example=example)
    SKILLS[skill_id].tool_names.append(name)
    _EXECUTORS[name] = execute


def set_simple(skill_id: str, name: str, key: str, prop: dict,
               example_value: Any, arg: str = "enabled", guard: Callable | None = None) -> None:
    def run(a: dict, s: dict):
        if guard:
            guard(a, s)
        s[skill_id][key] = a[arg]
        return s[skill_id][key]
    register(skill_id, name, {arg: prop}, {arg: example_value}, run, [f"{skill_id}.{key}"])


def get_group(skill_id: str, name: str) -> None:
    register(skill_id, name, {}, {}, lambda a, s: s[skill_id], [skill_id], "read")


class VehicleRuleError(ValueError):
    def __init__(self, reason, reason_code, resume_condition):
        super().__init__(reason)
        self.reason_code, self.resume_condition = reason_code, resume_condition


def ensure(condition: bool, reason: str, reason_code='vehicle_rejected', resume_condition=None) -> None:
    if not condition:
        raise VehicleRuleError(reason, reason_code, resume_condition)


def stopped(s: dict, reason: str = config_text("speech/vehicle.toml", "rejections.vehicle_moving")) -> None:
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
        "media": {"volume": 30, "mute": False, "playback": "paused", "source": "bluetooth", "title": config_text("speech/vehicle.toml", "state.initial_track"),
                  "track_index": 1, "radio_frequency_mhz": 98.5, "balance": 0, "fader": 0, "equalizer": "flat",
                  "simulated": True},
        "navigation": {"destination": None, "waypoints": [], "active": False,
                       "preferences": {"avoid_tolls": False, "avoid_highways": False, "prefer_fastest": True},
                       "home": None, "work": None, "simulated": True,
                       "notice": config_text("speech/vehicle.toml", "state.navigation_notice")},
    }


# 空调与空气管理：18
skill("climate")
get_group("climate", "get_climate_state")
set_simple("climate", "set_climate_power", "power", boolean(), True)


def temperature(a, s, relative=False):
    keys = list(s["climate"]["temperatures"]) if a["zone"] == "all" else [a["zone"]]
    values = {k: s["climate"]["temperatures"][k] + a["delta"] if relative else a["temperature"] for k in keys}
    ensure(all(16 <= value <= 30 for value in values.values()), config_text("speech/vehicle.toml", "rejections.temperature_range"))
    s["climate"]["temperatures"].update(values)
    s["climate"]["power"] = True
    if s["climate"]["sync_zones"] and a["zone"] == "driver":
        s["climate"]["temperatures"] = {k: values["driver"] for k in s["climate"]["temperatures"]}
    return deepcopy(s["climate"]["temperatures"])


register("climate", "set_temperature", {"zone": ZONE, "temperature": number(16, 30)}, {"zone": "all", "temperature": 24}, temperature, ["climate.temperatures.{zone}", "climate.power"])
register("climate", "adjust_temperature", {"zone": ZONE, "delta": number(-5, 5)}, {"zone": "driver", "delta": -1}, lambda a, s: temperature(a, s, True), ["climate.temperatures.{zone}", "climate.power"], "adjust")
set_simple("climate", "set_fan_speed", "fan_speed", number(0, 7, True), 4, "level")


def adjust_fan(a, s):
    value = s["climate"]["fan_speed"] + a["delta"]
    ensure(0 <= value <= 7, config_text("speech/vehicle.toml", "rejections.fan_range"))
    s["climate"]["fan_speed"] = value
    return value


register("climate", "adjust_fan_speed", {"delta": number(-3, 3, True)}, {"delta": 1}, adjust_fan, ["climate.fan_speed"], "adjust")
set_simple("climate", "set_airflow_mode", "airflow_mode", enum(["face", "feet", "defrost", "mixed"]), "feet", "mode")
set_simple("climate", "set_recirculation", "recirculation", enum(["internal", "external", "auto"]), "internal", "mode")
set_simple("climate", "set_auto_climate", "auto", boolean(), True)
set_simple("climate", "set_front_defrost", "front_defrost", boolean(), True)
set_simple("climate", "set_rear_defrost", "rear_defrost", boolean(), True)
set_simple("climate", "set_air_purifier", "air_purifier", boolean(), True)
set_simple("climate", "set_fragrance", "fragrance", number(0, 3, True), 1, "level")
set_simple("climate", "set_humidity", "humidity_target", number(30, 70, True), 45, "target")
set_simple("climate", "set_temperature_unit", "temperature_unit", enum(["celsius", "fahrenheit"]), "fahrenheit", "unit")


def sync_climate(a, s):
    s["climate"]["sync_zones"] = a["enabled"]
    if a["enabled"]:
        s["climate"]["temperatures"] = {key: s["climate"]["temperatures"]["driver"] for key in s["climate"]["temperatures"]}
    return s["climate"]["sync_zones"]


register("climate", "sync_climate_zones", {"enabled": boolean()}, {"enabled": True}, sync_climate, ["climate.sync_zones", "climate.temperatures"])
set_simple("climate", "set_rear_climate_power", "rear_power", boolean(), True)
register("climate", "get_air_quality", {}, {}, lambda a, s: s["climate"]["air_quality"], ["climate.air_quality"], "read")

# 座椅与舒适：16
skill("seats")
register("seats", "get_seat_state", {"seat": SEAT}, {"seat": "driver"}, lambda a, s: s["seats"]["positions"][a["seat"]], ["seats.positions.{seat}"], "read")


def seat_set(key):
    def run(a, s):
        s["seats"]["positions"][a["seat"]][key] = a["mode" if key == "massage" else "level" if key in ["heating", "ventilation", "side_support"] else "occupied" if key == "occupied" else "position"]
        return s["seats"]["positions"][a["seat"]][key]
    return run


for name, key in [("set_seat_heating", "heating"), ("set_seat_ventilation", "ventilation")]:
    register("seats", name, {"seat": SEAT, "level": number(0, 3, True)}, {"seat": "driver", "level": 2}, seat_set(key), [f"seats.positions.{{seat}}.{key}"])
register("seats", "set_seat_massage", {"seat": SEAT, "mode": enum(["off", "relax", "lumbar", "wave"])}, {"seat": "driver", "mode": "relax"}, seat_set("massage"), ["seats.positions.{seat}.massage"])
AXIS = enum(["fore_aft", "height", "backrest", "lumbar"])


def position_seat(a, s, relative=False):
    if a["seat"] == "driver":
        stopped(s, config_text("speech/vehicle.toml", "rejections.driver_seat_moving"))
    key = a["axis"]
    value = s["seats"]["positions"][a["seat"]][key] + a["delta"] if relative else a["position"]
    lower, upper = (80, 140) if key == "backrest" else (0, 100)
    ensure(lower <= value <= upper, config_text("speech/vehicle.toml", "rejections.seat_axis_range", axis=key, lower=str(lower), upper=str(upper)))
    s["seats"]["positions"][a["seat"]][key] = value
    return value


register("seats", "set_seat_position", {"seat": SEAT, "axis": AXIS, "position": number(0, 140)}, {"seat": "driver", "axis": "fore_aft", "position": 60}, position_seat, ["seats.positions.{seat}.{axis}"])
register("seats", "adjust_seat_position", {"seat": SEAT, "axis": AXIS, "delta": number(-10, 10)}, {"seat": "driver", "axis": "height", "delta": 5}, lambda a, s: position_seat(a, s, True), ["seats.positions.{seat}.{axis}"], "adjust")


def seat_memory(a, s, recall=False):
    if recall:
        stopped(s, config_text("speech/vehicle.toml", "rejections.seat_memory_moving"))
        s["seats"]["positions"][a["seat"]] = deepcopy(s["seats"]["memories"][a["seat"]][str(a["slot"])])
        return s["seats"]["positions"][a["seat"]]
    s["seats"]["memories"][a["seat"]][str(a["slot"])] = deepcopy(s["seats"]["positions"][a["seat"]])
    return s["seats"]["memories"][a["seat"]][str(a["slot"])]


for name, recall in [("save_seat_memory", False), ("recall_seat_memory", True)]:
    register("seats", name, {"seat": SEAT, "slot": SLOT}, {"seat": "driver", "slot": 1}, lambda a, s, recall=recall: seat_memory(a, s, recall), ["seats.memories.{seat}.{slot}", "seats.positions.{seat}"])
set_simple("seats", "set_steering_heating", "steering_heating", boolean(), True)


def steering(a, s):
    stopped(s, config_text("speech/vehicle.toml", "rejections.steering_moving"))
    s["seats"]["steering_position"][a["axis"]] = a["position"]
    return s["seats"]["steering_position"]


register("seats", "set_steering_position", {"axis": enum(["reach", "height"]), "position": number(0, 100, True)}, {"axis": "reach", "position": 60}, steering, ["seats.steering_position.{axis}"])
set_simple("seats", "set_easy_entry", "easy_entry", boolean(), True)
register("seats", "set_seat_occupancy", {"seat": SEAT, "occupied": boolean()}, {"seat": "rear_left", "occupied": True}, seat_set("occupied"), ["seats.positions.{seat}.occupied"])
register("seats", "get_seat_memory", {"seat": SEAT, "slot": SLOT}, {"seat": "driver", "slot": 1}, lambda a, s: s["seats"]["memories"][a["seat"]][str(a["slot"])], ["seats.memories.{seat}.{slot}"], "read")
for name, key, arg, hi in [("set_seat_cushion_length", "cushion_length", "position", 100), ("set_seat_side_support", "side_support", "level", 5), ("set_seat_headrest", "headrest", "position", 100)]:
    register("seats", name, {"seat": SEAT, arg: number(0, hi, True)}, {"seat": "driver", arg: min(2, hi)}, seat_set(key), [f"seats.positions.{{seat}}.{key}"])

# 车窗：10
skill("windows")
register("windows", "get_window_state", {"window": WINDOW}, {"window": "driver"}, lambda a, s: {"position": s["windows"]["positions"][a["window"]], "anti_pinch": s["windows"]["anti_pinch"][a["window"]]}, ["windows.positions.{window}", "windows.anti_pinch.{window}"], "read")


def position_window(a, s, relative=False):
    window = a["window"]
    target = s["windows"]["positions"][window] + a["delta"] if relative else a["position"]
    ensure(0 <= target <= 100, config_text("speech/vehicle.toml", "rejections.window_range"))
    ensure(not (s["windows"]["child_lock"] and window.startswith("rear") and target > s["windows"]["positions"][window]), config_text("speech/vehicle.toml", "rejections.rear_window_locked"))
    ensure(not (s.get("_meta", {}).get("speed_kph", 0) > 80 and target > 50), config_text("speech/vehicle.toml", "rejections.window_speed"), "speed_limit", {"speed_kph_max": 80, "position_max_while_driving": 50})
    s["windows"]["positions"][window] = target
    return target


register("windows", "set_window_position", {"window": WINDOW, "position": number(0, 100, True)}, {"window": "driver", "position": 50}, position_window, ["windows.positions.{window}"])
register("windows", "adjust_window_position", {"window": WINDOW, "delta": number(-100, 100, True)}, {"window": "driver", "delta": 20}, lambda a, s: position_window(a, s, True), ["windows.positions.{window}"], "adjust")


def all_windows(a, s, target):
    for window in WINDOWS:
        position_window({"window": window, "position": target}, s)
    return s["windows"]["positions"]


register("windows", "close_all_windows", {}, {}, lambda a, s: all_windows(a, s, 0), ["windows.positions.*"])
register("windows", "open_all_windows", {}, {}, lambda a, s: all_windows(a, s, 100), ["windows.positions.*"])
set_simple("windows", "set_window_child_lock", "child_lock", boolean(), True)


def anti_pinch(a, s):
    stopped(s, config_text("speech/vehicle.toml", "rejections.anti_pinch_moving"))
    s["windows"]["anti_pinch"][a["window"]] = a["enabled"]
    return a["enabled"]


register("windows", "set_window_anti_pinch", {"window": WINDOW, "enabled": boolean()}, {"window": "driver", "enabled": True}, anti_pinch, ["windows.anti_pinch.{window}"])
set_simple("windows", "set_sunroof_position", "sunroof", number(0, 100, True), 30, "position", lambda a, s: ensure(not (s.get("_meta", {}).get("speed_kph", 0) > 80 and a["position"] > 50), config_text("speech/vehicle.toml", "rejections.sunroof_speed"), "speed_limit", {"speed_kph_max": 80, "position_max_while_driving": 50}))
set_simple("windows", "set_sunshade_position", "sunshade", number(0, 100, True), 50, "position")
set_simple("windows", "set_rain_close_windows", "rain_close", boolean(), True)

# 后视镜：8
skill("mirrors")
register("mirrors", "get_mirror_state", {"mirror": MIRROR}, {"mirror": "left"}, lambda a, s: {**s["mirrors"]["positions"][a["mirror"]], "folded": s["mirrors"]["folded"], "heating": s["mirrors"]["heating"]}, ["mirrors.positions.{mirror}", "mirrors.folded", "mirrors.heating"], "read")
set_simple("mirrors", "set_mirror_fold", "folded", boolean(), True, "folded", lambda a, s: ensure(not (a["folded"] and s.get("_meta", {}).get("speed_kph", 0) > 5), config_text("speech/vehicle.toml", "rejections.mirror_speed"), "speed_limit", {"speed_kph_max": 5}))


def mirror_angle(a, s, relative=False):
    ensure(not s["mirrors"]["folded"], config_text("speech/vehicle.toml", "rejections.mirror_folded"))
    pos = s["mirrors"]["positions"][a["mirror"]]
    if relative:
        target = pos[a["axis"]] + a["delta"]
        ensure(-20 <= target <= 20, config_text("speech/vehicle.toml", "rejections.mirror_range"))
        pos[a["axis"]] = target
    else:
        pos.update(horizontal=a["horizontal"], vertical=a["vertical"])
    return pos


register("mirrors", "set_mirror_angle", {"mirror": MIRROR, "horizontal": number(-20, 20), "vertical": number(-20, 20)}, {"mirror": "left", "horizontal": 2, "vertical": -3}, mirror_angle, ["mirrors.positions.{mirror}"])
register("mirrors", "adjust_mirror_angle", {"mirror": MIRROR, "axis": enum(["horizontal", "vertical"]), "delta": number(-10, 10)}, {"mirror": "left", "axis": "vertical", "delta": -2}, lambda a, s: mirror_angle(a, s, True), ["mirrors.positions.{mirror}.{axis}"], "adjust")
set_simple("mirrors", "set_mirror_heating", "heating", boolean(), True)
set_simple("mirrors", "set_reverse_mirror_tilt", "reverse_tilt", boolean(), True)


def mirror_memory(a, s, recall=False):
    if recall:
        ensure(not s["mirrors"]["folded"], config_text("speech/vehicle.toml", "rejections.mirror_folded"))
        s["mirrors"]["positions"] = deepcopy(s["mirrors"]["memories"][str(a["slot"])])
        return s["mirrors"]["positions"]
    s["mirrors"]["memories"][str(a["slot"])] = deepcopy(s["mirrors"]["positions"])
    return s["mirrors"]["memories"][str(a["slot"])]


for name, recall in [("save_mirror_memory", False), ("recall_mirror_memory", True)]:
    register("mirrors", name, {"slot": SLOT}, {"slot": 1}, lambda a, s, recall=recall: mirror_memory(a, s, recall), ["mirrors.memories.{slot}", "mirrors.positions"])

# 灯光：12
skill("lights")
get_group("lights", "get_light_state")


def interior(a, s):
    zones = ["front", "rear"] if a["zone"] == "all" else [a["zone"]]
    for zone in zones:
        s["lights"]["interior"][zone] = a["enabled"]
    return s["lights"]["interior"]


register("lights", "set_interior_light", {"zone": enum(["all", "front", "rear"]), "enabled": boolean()}, {"zone": "all", "enabled": True}, interior, ["lights.interior.{zone}"])
set_simple("lights", "set_ambient_light", "ambient", boolean(), True)
set_simple("lights", "set_ambient_color", "ambient_color", enum(["white", "warm", "red", "blue", "green", "purple"]), "blue", "color")
set_simple("lights", "set_ambient_brightness", "ambient_brightness", number(0, 100, True), 60, "brightness")
set_simple("lights", "set_headlight_mode", "headlight_mode", enum(["off", "auto", "low"]), "auto", "mode")
set_simple("lights", "set_high_beam", "high_beam", boolean(), True)
set_simple("lights", "set_front_fog_lights", "front_fog", boolean(), True)
set_simple("lights", "set_rear_fog_light", "rear_fog", boolean(), True)


def reading_light(a, s):
    s["lights"]["reading"][a["seat"]] = a["enabled"]
    return a["enabled"]


register("lights", "set_reading_light", {"seat": SEAT, "enabled": boolean()}, {"seat": "rear_left", "enabled": True}, reading_light, ["lights.reading.{seat}"])
set_simple("lights", "set_follow_me_home", "follow_me_home_seconds", number(0, 120, True), 60, "seconds")
set_simple("lights", "set_hazard_lights", "hazard", boolean(), True)

# 门锁：8
skill("locks")
get_group("locks", "get_lock_state")


def door_lock(a, s):
    if not a["locked"]:
        stopped(s, config_text("speech/vehicle.toml", "rejections.unlock_moving"))
    doors = SEATS if a["door"] == "all" else [a["door"]]
    for door in doors:
        s["locks"]["doors"][door] = a["locked"]
    return s["locks"]["doors"]


register("locks", "set_door_lock", {"door": enum(["all", *SEATS]), "locked": boolean()}, {"door": "driver", "locked": False}, door_lock, ["locks.doors.{door}"])
register("locks", "lock_all_doors", {}, {}, lambda a, s: door_lock({"door": "all", "locked": True}, s), ["locks.doors.*"])
register("locks", "unlock_all_doors", {}, {}, lambda a, s: door_lock({"door": "all", "locked": False}, s), ["locks.doors.*"])


def child_lock(a, s):
    stopped(s, config_text("speech/vehicle.toml", "rejections.child_lock_moving"))
    s["locks"]["child_lock"][a["side"]] = a["enabled"]
    return a["enabled"]


register("locks", "set_child_lock", {"side": enum(["left", "right"]), "enabled": boolean()}, {"side": "left", "enabled": True}, child_lock, ["locks.child_lock.{side}"])
set_simple("locks", "set_auto_lock", "auto_lock", boolean(), True)
set_simple("locks", "set_walk_away_lock", "walk_away_lock", boolean(), True)
set_simple("locks", "set_charge_port_lock", "charge_port_locked", boolean(), False, "locked", lambda a, s: ensure(not (not a["locked"] and s.get("_meta", {}).get("charging", False)), config_text("speech/vehicle.toml", "rejections.charge_port_busy")))

# 尾门：4
skill("tailgate")
get_group("tailgate", "get_tailgate_state")


def tailgate_open(a, s):
    if a["open"]:
        stopped(s, config_text("speech/vehicle.toml", "rejections.tailgate_moving"))
    s["tailgate"].update(open=a["open"], position=s["tailgate"]["height"] if a["open"] else 0,
                         moving=False, last_action="opened" if a["open"] else "closed")
    return s["tailgate"]


register("tailgate", "set_tailgate_open", {"open": boolean()}, {"open": True}, tailgate_open, ["tailgate.open", "tailgate.position", "tailgate.moving", "tailgate.last_action"])
set_simple("tailgate", "set_tailgate_height", "height", number(20, 100, True), 80, "height", lambda a, s: stopped(s))


def stop_tailgate(a, s):
    s["tailgate"].update(moving=False, last_action="stopped")
    return s["tailgate"]


register("tailgate", "stop_tailgate", {}, {}, stop_tailgate, ["tailgate.moving", "tailgate.last_action"])

# 雨刷清洗：6
skill("wipers")
get_group("wipers", "get_wiper_state")
set_simple("wipers", "set_wiper_mode", "mode", enum(["off", "auto", "intermittent", "low", "high"]), "auto", "mode")
set_simple("wipers", "set_wiper_interval", "interval_seconds", number(1, 15, True), 3, "seconds")


def wash(a, s):
    ensure(s["wipers"]["washer_fluid_pct"] > 0, config_text("speech/vehicle.toml", "rejections.washer_empty"))
    s["wipers"]["washer_fluid_pct"] -= 1
    s["wipers"]["wash_count"] += 1
    s["wipers"]["last_wash"] = a["target"]
    return {"target": a["target"], "washer_fluid_pct": s["wipers"]["washer_fluid_pct"], "wash_count": s["wipers"]["wash_count"]}


register("wipers", "wash_windshield", {"target": enum(["front", "rear"])}, {"target": "front"}, wash, ["wipers.washer_fluid_pct", "wipers.wash_count", "wipers.last_wash"])
set_simple("wipers", "set_rear_wiper", "rear", boolean(), True)
set_simple("wipers", "set_rain_sensitivity", "rain_sensitivity", number(1, 5, True), 4, "level")

# 媒体：10
skill("media")
get_group("media", "get_media_state")
set_simple("media", "set_volume", "volume", number(0, 100, True), 40, "volume")


def adjust_volume(a, s):
    value = s["media"]["volume"] + a["delta"]
    ensure(0 <= value <= 100, config_text("speech/vehicle.toml", "rejections.volume_range"))
    s["media"]["volume"] = value
    return value


register("media", "adjust_volume", {"delta": number(-30, 30, True)}, {"delta": 5}, adjust_volume, ["media.volume"], "adjust")
set_simple("media", "set_mute", "mute", boolean(), True)


def playback(a, s):
    action = a["action"]
    if action in ["next", "previous"]:
        s["media"]["track_index"] = max(1, s["media"]["track_index"] + (1 if action == "next" else -1))
        s["media"]["title"] = config_text("speech/vehicle.toml", "state.track", index=str(s["media"]["track_index"]))
        s["media"]["playback"] = "playing"
    else:
        s["media"]["playback"] = {"play": "playing", "pause": "paused", "stop": "stopped"}[action]
    return {"playback": s["media"]["playback"], "title": s["media"]["title"]}


register("media", "control_playback", {"action": enum(["play", "pause", "stop", "next", "previous"])}, {"action": "pause"}, playback, ["media.playback", "media.track_index", "media.title"])
set_simple("media", "set_media_source", "source", enum(["bluetooth", "radio", "usb", "online"]), "radio", "source")


def play_media(a, s):
    s["media"].update(title=a["title"].strip(), playback="playing")
    return {"title": s["media"]["title"], "playback": s["media"]["playback"], "simulated": True}


register("media", "play_media", {"title": string()}, {"title": "夜曲"}, play_media, ["media.title", "media.playback"])
set_simple("media", "set_radio_frequency", "radio_frequency_mhz", number(87.5, 108), 101.7, "frequency")


def balance(a, s):
    s["media"].update(balance=a["balance"], fader=a["fader"])
    return {"balance": a["balance"], "fader": a["fader"]}


register("media", "set_audio_balance", {"balance": number(-10, 10, True), "fader": number(-10, 10, True)}, {"balance": 0, "fader": -2}, balance, ["media.balance", "media.fader"])
set_simple("media", "set_equalizer", "equalizer", enum(["flat", "pop", "rock", "classical", "voice"]), "voice", "preset")

# 导航：8
skill("navigation")
get_group("navigation", "get_navigation_state")


def destination(a, s):
    s['navigation'].pop('destination_place', None)
    s["navigation"].update(destination=a["destination"].strip(), active=True)
    return {"destination": s["navigation"]["destination"], "simulated": True, "notice": s["navigation"]["notice"]}


register("navigation", "set_navigation_destination", {"destination": string()}, {"destination": "巴黎卢浮宫"}, destination, ["navigation.destination", "navigation.active"])


def add_waypoint(a, s):
    ensure(len(s["navigation"]["waypoints"]) < 10, config_text("speech/vehicle.toml", "rejections.waypoint_limit"))
    s["navigation"]["waypoints"].append(a["destination"].strip())
    return s["navigation"]["waypoints"]


register("navigation", "add_navigation_waypoint", {"destination": string()}, {"destination": "附近充电站"}, add_waypoint, ["navigation.waypoints"], "append")


def remove_waypoint(a, s):
    ensure(a["index"] < len(s["navigation"]["waypoints"]), config_text("speech/vehicle.toml", "rejections.waypoint_missing"))
    removed = s["navigation"]["waypoints"].pop(a["index"])
    return {"removed": removed, "waypoints": s["navigation"]["waypoints"]}


register("navigation", "remove_navigation_waypoint", {"index": number(0, 9, True)}, {"index": 0}, remove_waypoint, ["navigation.waypoints"], "remove")


def preferences(a, s):
    s["navigation"]["preferences"] = deepcopy(a)
    return {"preferences": a, "simulated": True, "notice": config_text("speech/vehicle.toml", "state.preferences_notice")}


register("navigation", "set_navigation_preferences", {"avoid_tolls": boolean(), "avoid_highways": boolean(), "prefer_fastest": boolean()}, {"avoid_tolls": True, "avoid_highways": False, "prefer_fastest": True}, preferences, ["navigation.preferences"])


def cancel_navigation(a, s):
    s['navigation'].pop('destination_place', None)
    s["navigation"].update(active=False, destination=None, waypoints=[])
    return s["navigation"]


register("navigation", "cancel_navigation", {}, {}, cancel_navigation, ["navigation.active", "navigation.destination", "navigation.waypoints"])
set_simple("navigation", "set_home_destination", "home", string(), "巴黎市中心", "destination")
set_simple("navigation", "set_work_destination", "work", string(), "拉德芳斯", "destination")

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
assert set(TOOLS) == set(_EXECUTORS)
_VEHICLE_TEXT_EXPECTED = {
    f"skills.{skill_id}.{field}": set()
    for skill_id in SKILLS for field in ("name", "description")
}
for _name, _tool in TOOLS.items():
    _VEHICLE_TEXT_EXPECTED[f"functions.{_name}.description"] = set()
    for _parameter in _tool.parameters["properties"]:
        _VEHICLE_TEXT_EXPECTED[f"functions.{_name}.parameters.{_parameter}"] = set()
validate_config("vehicle/functions.toml", _VEHICLE_TEXT_EXPECTED)
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
        result["reason"] = str(exc) or config_text("speech/vehicle.toml", "rejections.default")
        result["reason_code"] = getattr(exc, "reason_code", "vehicle_rejected")
        result["resume_condition"] = getattr(exc, "resume_condition", None)
    result["state"] = deepcopy(state)
    return result


def load_skill(skill_id: str) -> dict:
    if skill_id not in SKILLS:
        return {"ok": False, "reason": "未知skill_id", "available_skills": list(SKILLS)}
    manifest = SKILLS[skill_id]
    return {"ok": True, "skill": manifest.model_dump(), "tools": [TOOLS[name].model_dump() for name in manifest.tool_names]}
