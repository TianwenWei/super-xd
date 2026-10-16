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

TOOLS: dict[str, ToolSpec] = {}
SKILLS: dict[str, SkillManifest] = {}
_EXECUTORS: dict[str, Callable[[dict, dict], Any]] = {}
SEATS = ["driver", "front_passenger", "rear_left", "rear_right"]
WINDOWS = SEATS
ZONES = ["all", "driver", "front_passenger", "rear"]
MIRRORS = ["left", "right"]


def enum(values: list[str], description: str) -> dict:
    return {"type": "string", "enum": values, "description": description}


def number(lo: float, hi: float, description: str, integer: bool = False) -> dict:
    return {"type": "integer" if integer else "number", "minimum": lo, "maximum": hi, "description": description}


def boolean(description: str) -> dict:
    return {"type": "boolean", "description": description}


def string(description: str, max_length: int = 200) -> dict:
    # Also permit whole-string regex decoding in providers' strict mode. A bare
    # \S means "contains nonspace" to JSON Schema but can constrain decoding to
    # one character; this equivalent pattern explicitly spans the entire value.
    return {"type": "string", "minLength": 1, "maxLength": max_length, "pattern": r"[\s\S]*\S[\s\S]*", "description": description}


SEAT = enum(SEATS, "本模拟为左舵车；主驾/左前/left_front=driver，副驾/右前/right_front=front_passenger，左后=rear_left，右后=rear_right")
WINDOW = enum(WINDOWS, "本模拟为左舵车；主驾/左前/left_front车窗=driver，副驾/右前/right_front车窗=front_passenger，左后车窗=rear_left，右后车窗=rear_right")
ZONE = enum(ZONES, "温区：全部=all，主驾/左前=driver，副驾/右前=front_passenger，后排=rear")
MIRROR = enum(MIRRORS, "后视镜：left左/right右")
SLOT = number(1, 3, "记忆槽位，1至3", True)


def skill(id: str, name: str, description: str) -> None:
    SKILLS[id] = SkillManifest(id=id, name=name, description=description, tool_names=[])


def register(skill_id: str, name: str, description: str, props: dict, example: dict,
             execute: Callable[[dict, dict], Any], paths: list[str], operation: str = "set") -> None:
    if name in TOOLS:
        raise ValueError(f"duplicate function {name}")
    schema = {"type": "object", "properties": deepcopy(props), "required": list(props), "additionalProperties": False}
    Draft202012Validator.check_schema(schema)
    TOOLS[name] = ToolSpec(name=name, description=description + "（模拟车控）", parameters=schema,
                           skill_id=skill_id, state_mapping={"paths": paths, "operation": operation, "simulated": True},
                           example=example)
    SKILLS[skill_id].tool_names.append(name)
    _EXECUTORS[name] = execute


def set_simple(skill_id: str, name: str, description: str, key: str, prop: dict,
               example_value: Any, arg: str = "enabled", guard: Callable | None = None) -> None:
    def run(a: dict, s: dict):
        if guard:
            guard(a, s)
        s[skill_id][key] = a[arg]
        return s[skill_id][key]
    register(skill_id, name, description, {arg: prop}, {arg: example_value}, run, [f"{skill_id}.{key}"])


def get_group(skill_id: str, name: str, description: str) -> None:
    register(skill_id, name, description, {}, {}, lambda a, s: s[skill_id], [skill_id], "read")


class VehicleRuleError(ValueError):
    def __init__(self, reason, reason_code, resume_condition):
        super().__init__(reason)
        self.reason_code, self.resume_condition = reason_code, resume_condition


def ensure(condition: bool, reason: str, reason_code='vehicle_rejected', resume_condition=None) -> None:
    if not condition:
        raise VehicleRuleError(reason, reason_code, resume_condition)


def stopped(s: dict, reason: str = "车辆行驶中，需停车后操作") -> None:
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
        "media": {"volume": 30, "mute": False, "playback": "paused", "source": "bluetooth", "title": "模拟曲目 1",
                  "track_index": 1, "radio_frequency_mhz": 98.5, "balance": 0, "fader": 0, "equalizer": "flat",
                  "simulated": True},
        "navigation": {"destination": None, "waypoints": [], "active": False,
                       "preferences": {"avoid_tolls": False, "avoid_highways": False, "prefer_fastest": True},
                       "home": None, "work": None, "simulated": True,
                       "notice": "仅保存模拟目的地、途经点与偏好；未计算真实路线"},
    }


# 空调与空气管理：18
skill("climate", "空调与空气管理", "温区温度、风量、送风、内外循环、除雾、空气净化与香氛。")
get_group("climate", "get_climate_state", "读取空调与空气管理全部状态")
set_simple("climate", "set_climate_power", "开启或关闭空调电源", "power", boolean("是否开启"), True)


def temperature(a, s, relative=False):
    keys = list(s["climate"]["temperatures"]) if a["zone"] == "all" else [a["zone"]]
    values = {k: s["climate"]["temperatures"][k] + a["delta"] if relative else a["temperature"] for k in keys}
    ensure(all(16 <= value <= 30 for value in values.values()), "目标温度超出16至30摄氏度，已保留原温度")
    s["climate"]["temperatures"].update(values)
    s["climate"]["power"] = True
    if s["climate"]["sync_zones"] and a["zone"] == "driver":
        s["climate"]["temperatures"] = {k: values["driver"] for k in s["climate"]["temperatures"]}
    return deepcopy(s["climate"]["temperatures"])


register("climate", "set_temperature", "设置温区目标温度，单位摄氏度，自动打开空调", {"zone": ZONE, "temperature": number(16, 30, "目标摄氏温度")}, {"zone": "all", "temperature": 24}, temperature, ["climate.temperatures.{zone}", "climate.power"])
register("climate", "adjust_temperature", "相对调高或调低温区温度，自动打开空调，超范围拒绝整条指令", {"zone": ZONE, "delta": number(-5, 5, "摄氏温度变化量")}, {"zone": "driver", "delta": -1}, lambda a, s: temperature(a, s, True), ["climate.temperatures.{zone}", "climate.power"], "adjust")
set_simple("climate", "set_fan_speed", "设置空调风量，0关闭风机，7最大", "fan_speed", number(0, 7, "风量档位", True), 4, "level")


def adjust_fan(a, s):
    value = s["climate"]["fan_speed"] + a["delta"]
    ensure(0 <= value <= 7, "风量调整后超出0至7档，已保留原档位")
    s["climate"]["fan_speed"] = value
    return value


register("climate", "adjust_fan_speed", "相对调整空调风量", {"delta": number(-3, 3, "变化档数", True)}, {"delta": 1}, adjust_fan, ["climate.fan_speed"], "adjust")
set_simple("climate", "set_airflow_mode", "设置吹面、吹脚、挡风除雾或混合送风", "airflow_mode", enum(["face", "feet", "defrost", "mixed"], "送风模式"), "feet", "mode")
set_simple("climate", "set_recirculation", "设置内循环、外循环或自动循环", "recirculation", enum(["internal", "external", "auto"], "循环模式"), "internal", "mode")
set_simple("climate", "set_auto_climate", "开关自动空调模式", "auto", boolean("是否开启"), True)
set_simple("climate", "set_front_defrost", "开关前挡风玻璃除雾", "front_defrost", boolean("是否开启"), True)
set_simple("climate", "set_rear_defrost", "开关后挡风玻璃加热除雾", "rear_defrost", boolean("是否开启"), True)
set_simple("climate", "set_air_purifier", "开关车内空气净化器", "air_purifier", boolean("是否开启"), True)
set_simple("climate", "set_fragrance", "设置香氛浓度，0关闭，3最高", "fragrance", number(0, 3, "浓度档位", True), 1, "level")
set_simple("climate", "set_humidity", "设置模拟车内目标相对湿度", "humidity_target", number(30, 70, "目标湿度百分比", True), 45, "target")
set_simple("climate", "set_temperature_unit", "设置温度显示单位，控制温度仍使用摄氏度", "temperature_unit", enum(["celsius", "fahrenheit"], "显示单位"), "fahrenheit", "unit")


def sync_climate(a, s):
    s["climate"]["sync_zones"] = a["enabled"]
    if a["enabled"]:
        s["climate"]["temperatures"] = {key: s["climate"]["temperatures"]["driver"] for key in s["climate"]["temperatures"]}
    return s["climate"]["sync_zones"]


register("climate", "sync_climate_zones", "开关温区同步，打开时所有温区跟随主驾温度", {"enabled": boolean("是否同步")}, {"enabled": True}, sync_climate, ["climate.sync_zones", "climate.temperatures"])
set_simple("climate", "set_rear_climate_power", "开关后排独立空调", "rear_power", boolean("是否开启"), True)
register("climate", "get_air_quality", "读取模拟PM2.5与CO2空气质量数值", {}, {}, lambda a, s: s["climate"]["air_quality"], ["climate.air_quality"], "read")

# 座椅与舒适：16
skill("seats", "座椅与舒适", "四个座位的加热、通风、按摩、位置、记忆与方向盘舒适设置。")
register("seats", "get_seat_state", "读取指定座位状态", {"seat": SEAT}, {"seat": "driver"}, lambda a, s: s["seats"]["positions"][a["seat"]], ["seats.positions.{seat}"], "read")


def seat_set(key):
    def run(a, s):
        s["seats"]["positions"][a["seat"]][key] = a["mode" if key == "massage" else "level" if key in ["heating", "ventilation", "side_support"] else "occupied" if key == "occupied" else "position"]
        return s["seats"]["positions"][a["seat"]][key]
    return run


for name, desc, key in [("set_seat_heating", "设置指定座椅加热，0关闭，3最高", "heating"), ("set_seat_ventilation", "设置指定座椅通风，0关闭，3最高", "ventilation")]:
    register("seats", name, desc, {"seat": SEAT, "level": number(0, 3, "档位", True)}, {"seat": "driver", "level": 2}, seat_set(key), [f"seats.positions.{{seat}}.{key}"])
register("seats", "set_seat_massage", "开关指定座椅按摩；开启未指定模式默认舒缓relax，关闭off。与加热、通风独立", {"seat": SEAT, "mode": enum(["off", "relax", "lumbar", "wave"], "off关闭，relax舒缓/放松，lumbar腰部，wave波浪")}, {"seat": "driver", "mode": "relax"}, seat_set("massage"), ["seats.positions.{seat}.massage"])
AXIS = enum(["fore_aft", "height", "backrest", "lumbar"], "座椅轴向，backrest靠背角度80至140度，其余0至100")


def position_seat(a, s, relative=False):
    if a["seat"] == "driver":
        stopped(s, "驾驶中禁止调整主驾座椅位置")
    key = a["axis"]
    value = s["seats"]["positions"][a["seat"]][key] + a["delta"] if relative else a["position"]
    lower, upper = (80, 140) if key == "backrest" else (0, 100)
    ensure(lower <= value <= upper, f"{key}允许范围为{lower}至{upper}，已保留原位置")
    s["seats"]["positions"][a["seat"]][key] = value
    return value


register("seats", "set_seat_position", "调节座椅滑轨前后位置、整椅高度、靠背角度或腰托位置；仅fore_aft/height/backrest/lumbar四种轴。主驾停车才可调；靠背80至140度，其余0至100", {"seat": SEAT, "axis": AXIS, "position": number(0, 140, "目标位置，需满足轴向范围")}, {"seat": "driver", "axis": "fore_aft", "position": 60}, position_seat, ["seats.positions.{seat}.{axis}"])
register("seats", "adjust_seat_position", "相对调整座椅位置，主驾停车才可调", {"seat": SEAT, "axis": AXIS, "delta": number(-10, 10, "位置变化量")}, {"seat": "driver", "axis": "height", "delta": 5}, lambda a, s: position_seat(a, s, True), ["seats.positions.{seat}.{axis}"], "adjust")


def seat_memory(a, s, recall=False):
    if recall:
        stopped(s, "行驶中禁止恢复座椅记忆")
        s["seats"]["positions"][a["seat"]] = deepcopy(s["seats"]["memories"][a["seat"]][str(a["slot"])])
        return s["seats"]["positions"][a["seat"]]
    s["seats"]["memories"][a["seat"]][str(a["slot"])] = deepcopy(s["seats"]["positions"][a["seat"]])
    return s["seats"]["memories"][a["seat"]][str(a["slot"])]


for name, desc, recall in [("save_seat_memory", "保存座椅设置到指定记忆槽位", False), ("recall_seat_memory", "停车后恢复指定座椅记忆槽位", True)]:
    register("seats", name, desc, {"seat": SEAT, "slot": SLOT}, {"seat": "driver", "slot": 1}, lambda a, s, recall=recall: seat_memory(a, s, recall), ["seats.memories.{seat}.{slot}", "seats.positions.{seat}"])
set_simple("seats", "set_steering_heating", "开关方向盘加热", "steering_heating", boolean("是否开启"), True)


def steering(a, s):
    stopped(s, "驾驶中禁止调节方向盘位置")
    s["seats"]["steering_position"][a["axis"]] = a["position"]
    return s["seats"]["steering_position"]


register("seats", "set_steering_position", "停车后调节方向盘伸缩或高度", {"axis": enum(["reach", "height"], "伸缩或高度"), "position": number(0, 100, "位置百分比", True)}, {"axis": "reach", "position": 60}, steering, ["seats.steering_position.{axis}"])
set_simple("seats", "set_easy_entry", "开关座椅迎宾模式", "easy_entry", boolean("是否开启"), True)
register("seats", "set_seat_occupancy", "模拟指定座椅乘员占用状态，供车控状态验收", {"seat": SEAT, "occupied": boolean("是否有人")}, {"seat": "rear_left", "occupied": True}, seat_set("occupied"), ["seats.positions.{seat}.occupied"])
register("seats", "get_seat_memory", "读取指定座椅记忆槽位", {"seat": SEAT, "slot": SLOT}, {"seat": "driver", "slot": 1}, lambda a, s: s["seats"]["memories"][a["seat"]][str(a["slot"])], ["seats.memories.{seat}.{slot}"], "read")
for name, desc, key, arg, hi in [("set_seat_cushion_length", "设置座垫长度", "cushion_length", "position", 100), ("set_seat_side_support", "设置座椅侧翼支撑档位", "side_support", "level", 5), ("set_seat_headrest", "设置头枕高度", "headrest", "position", 100)]:
    register("seats", name, desc, {"seat": SEAT, arg: number(0, hi, "目标位置或档位", True)}, {"seat": "driver", arg: min(2, hi)}, seat_set(key), [f"seats.positions.{{seat}}.{key}"])

# 车窗：10
skill("windows", "车窗", "四门车窗开合百分比、童锁、防夹、天窗、遮阳帘与雨天自动关窗。")
register("windows", "get_window_state", "读取指定车窗开度，0全关，100全开", {"window": WINDOW}, {"window": "driver"}, lambda a, s: {"position": s["windows"]["positions"][a["window"]], "anti_pinch": s["windows"]["anti_pinch"][a["window"]]}, ["windows.positions.{window}", "windows.anti_pinch.{window}"], "read")


def position_window(a, s, relative=False):
    window = a["window"]
    target = s["windows"]["positions"][window] + a["delta"] if relative else a["position"]
    ensure(0 <= target <= 100, "车窗目标开度超出0至100，已保留原开度")
    ensure(not (s["windows"]["child_lock"] and window.startswith("rear") and target > s["windows"]["positions"][window]), "后排车窗童锁已开启，拒绝打开后排车窗")
    ensure(not (s.get("_meta", {}).get("speed_kph", 0) > 80 and target > 50), "车速超过80公里/小时，拒绝将车窗打开超过50%", "speed_limit", {"speed_kph_max": 80, "position_max_while_driving": 50})
    s["windows"]["positions"][window] = target
    return target


register("windows", "set_window_position", "设置单扇车窗开度百分比，0全关，100全开；童锁/高速状态可能拒绝", {"window": WINDOW, "position": number(0, 100, "开度百分比", True)}, {"window": "driver", "position": 50}, position_window, ["windows.positions.{window}"])
register("windows", "adjust_window_position", "相对调整单扇车窗开度百分比", {"window": WINDOW, "delta": number(-100, 100, "开度变化百分比", True)}, {"window": "driver", "delta": 20}, lambda a, s: position_window(a, s, True), ["windows.positions.{window}"], "adjust")


def all_windows(a, s, target):
    for window in WINDOWS:
        position_window({"window": window, "position": target}, s)
    return s["windows"]["positions"]


register("windows", "close_all_windows", "关闭全部四门车窗", {}, {}, lambda a, s: all_windows(a, s, 0), ["windows.positions.*"])
register("windows", "open_all_windows", "打开全部四门车窗，任一车窗不安全则整条拒绝", {}, {}, lambda a, s: all_windows(a, s, 100), ["windows.positions.*"])
set_simple("windows", "set_window_child_lock", "开关后排车窗童锁，开启后不能打开后排车窗", "child_lock", boolean("是否开启"), True)


def anti_pinch(a, s):
    stopped(s, "行驶中禁止更改车窗防夹设置")
    s["windows"]["anti_pinch"][a["window"]] = a["enabled"]
    return a["enabled"]


register("windows", "set_window_anti_pinch", "停车后开关指定车窗防夹模拟设置", {"window": WINDOW, "enabled": boolean("是否开启")}, {"window": "driver", "enabled": True}, anti_pinch, ["windows.anti_pinch.{window}"])
set_simple("windows", "set_sunroof_position", "设置天窗开度，0关闭，100全开", "sunroof", number(0, 100, "开度百分比", True), 30, "position", lambda a, s: ensure(not (s.get("_meta", {}).get("speed_kph", 0) > 80 and a["position"] > 50), "高速行驶时天窗开度不能超过50%", "speed_limit", {"speed_kph_max": 80, "position_max_while_driving": 50}))
set_simple("windows", "set_sunshade_position", "设置天窗遮阳帘开度，0遮住，100打开", "sunshade", number(0, 100, "开度百分比", True), 50, "position")
set_simple("windows", "set_rain_close_windows", "开关雨天自动关闭车窗的模拟偏好", "rain_close", boolean("是否开启"), True)

# 后视镜：8
skill("mirrors", "后视镜", "左右后视镜角度、折叠、加热、倒车下翻与记忆。")
register("mirrors", "get_mirror_state", "读取指定后视镜位置及公共设置", {"mirror": MIRROR}, {"mirror": "left"}, lambda a, s: {**s["mirrors"]["positions"][a["mirror"]], "folded": s["mirrors"]["folded"], "heating": s["mirrors"]["heating"]}, ["mirrors.positions.{mirror}", "mirrors.folded", "mirrors.heating"], "read")
set_simple("mirrors", "set_mirror_fold", "折叠或展开左右后视镜，车速超过5时拒绝折叠", "folded", boolean("是否折叠"), True, "folded", lambda a, s: ensure(not (a["folded"] and s.get("_meta", {}).get("speed_kph", 0) > 5), "行驶中拒绝折叠后视镜", "speed_limit", {"speed_kph_max": 5}))


def mirror_angle(a, s, relative=False):
    ensure(not s["mirrors"]["folded"], "后视镜已折叠，请先展开")
    pos = s["mirrors"]["positions"][a["mirror"]]
    if relative:
        target = pos[a["axis"]] + a["delta"]
        ensure(-20 <= target <= 20, "后视镜角度超出-20至20度，已保留原角度")
        pos[a["axis"]] = target
    else:
        pos.update(horizontal=a["horizontal"], vertical=a["vertical"])
    return pos


register("mirrors", "set_mirror_angle", "设置指定后视镜水平和垂直角度，范围-20至20度", {"mirror": MIRROR, "horizontal": number(-20, 20, "水平角度"), "vertical": number(-20, 20, "垂直角度")}, {"mirror": "left", "horizontal": 2, "vertical": -3}, mirror_angle, ["mirrors.positions.{mirror}"])
register("mirrors", "adjust_mirror_angle", "相对调整指定后视镜单轴角度", {"mirror": MIRROR, "axis": enum(["horizontal", "vertical"], "调整轴"), "delta": number(-10, 10, "变化度数")}, {"mirror": "left", "axis": "vertical", "delta": -2}, lambda a, s: mirror_angle(a, s, True), ["mirrors.positions.{mirror}.{axis}"], "adjust")
set_simple("mirrors", "set_mirror_heating", "开关两侧后视镜加热除雾", "heating", boolean("是否开启"), True)
set_simple("mirrors", "set_reverse_mirror_tilt", "开关倒车时后视镜自动下翻偏好", "reverse_tilt", boolean("是否开启"), True)


def mirror_memory(a, s, recall=False):
    if recall:
        ensure(not s["mirrors"]["folded"], "后视镜已折叠，请先展开")
        s["mirrors"]["positions"] = deepcopy(s["mirrors"]["memories"][str(a["slot"])])
        return s["mirrors"]["positions"]
    s["mirrors"]["memories"][str(a["slot"])] = deepcopy(s["mirrors"]["positions"])
    return s["mirrors"]["memories"][str(a["slot"])]


for name, desc, recall in [("save_mirror_memory", "保存左右后视镜角度记忆", False), ("recall_mirror_memory", "恢复左右后视镜角度记忆", True)]:
    register("mirrors", name, desc, {"slot": SLOT}, {"slot": 1}, lambda a, s, recall=recall: mirror_memory(a, s, recall), ["mirrors.memories.{slot}", "mirrors.positions"])

# 灯光：12
skill("lights", "灯光", "车内灯、阅读灯、氛围灯及颜色亮度、大灯、雾灯、双闪与伴我回家。")
get_group("lights", "get_light_state", "读取车内外灯光全部状态")


def interior(a, s):
    zones = ["front", "rear"] if a["zone"] == "all" else [a["zone"]]
    for zone in zones:
        s["lights"]["interior"][zone] = a["enabled"]
    return s["lights"]["interior"]


register("lights", "set_interior_light", "开关共用车厢顶灯，按前排、后排或全部区域控制", {"zone": enum(["all", "front", "rear"], "照明区域"), "enabled": boolean("是否开启")}, {"zone": "all", "enabled": True}, interior, ["lights.interior.{zone}"])
set_simple("lights", "set_ambient_light", "开启或关闭氛围灯电源；设置颜色/亮度不需要本工具；不要打开/保持原状不构成关闭指令", "ambient", boolean("true=开启氛围灯；false=明确关闭氛围灯"), True)
set_simple("lights", "set_ambient_color", "设置氛围灯颜色", "ambient_color", enum(["white", "warm", "red", "blue", "green", "purple"], "颜色"), "blue", "color")
set_simple("lights", "set_ambient_brightness", "设置氛围灯亮度", "ambient_brightness", number(0, 100, "亮度百分比", True), 60, "brightness")
set_simple("lights", "set_headlight_mode", "设置大灯关闭、自动或近光模式", "headlight_mode", enum(["off", "auto", "low"], "大灯模式"), "auto", "mode")
set_simple("lights", "set_high_beam", "开关远光灯", "high_beam", boolean("是否开启"), True)
set_simple("lights", "set_front_fog_lights", "开关前雾灯", "front_fog", boolean("是否开启"), True)
set_simple("lights", "set_rear_fog_light", "开关后雾灯", "rear_fog", boolean("是否开启"), True)


def reading_light(a, s):
    s["lights"]["reading"][a["seat"]] = a["enabled"]
    return a["enabled"]


register("lights", "set_reading_light", "开关指定座位阅读灯", {"seat": SEAT, "enabled": boolean("是否开启")}, {"seat": "rear_left", "enabled": True}, reading_light, ["lights.reading.{seat}"])
set_simple("lights", "set_follow_me_home", "设置伴我回家大灯延时，0关闭", "follow_me_home_seconds", number(0, 120, "延时秒数", True), 60, "seconds")
set_simple("lights", "set_hazard_lights", "开关双闪危险警告灯", "hazard", boolean("是否开启"), True)

# 门锁：8
skill("locks", "门锁", "四门门锁、儿童安全锁、自动落锁、离车上锁与充电口锁。")
get_group("locks", "get_lock_state", "读取全部门锁和安全锁状态")


def door_lock(a, s):
    if not a["locked"]:
        stopped(s, "车辆行驶中拒绝解锁车门")
    doors = SEATS if a["door"] == "all" else [a["door"]]
    for door in doors:
        s["locks"]["doors"][door] = a["locked"]
    return s["locks"]["doors"]


register("locks", "set_door_lock", "锁定或解锁指定车门或全部车门，行驶中拒绝解锁", {"door": enum(["all", *SEATS], "车门"), "locked": boolean("是否锁定")}, {"door": "driver", "locked": False}, door_lock, ["locks.doors.{door}"])
register("locks", "lock_all_doors", "锁定全部四门", {}, {}, lambda a, s: door_lock({"door": "all", "locked": True}, s), ["locks.doors.*"])
register("locks", "unlock_all_doors", "停车后解锁全部四门", {}, {}, lambda a, s: door_lock({"door": "all", "locked": False}, s), ["locks.doors.*"])


def child_lock(a, s):
    stopped(s, "行驶中禁止更改儿童安全锁")
    s["locks"]["child_lock"][a["side"]] = a["enabled"]
    return a["enabled"]


register("locks", "set_child_lock", "停车后启用或关闭指定侧后车门儿童安全锁，与普通车门门锁独立。打开儿童锁=启用安全功能enabled:true；关闭儿童锁=解除安全功能enabled:false。", {"side": enum(["left", "right"], "后排左右侧"), "enabled": boolean("true=开启儿童安全锁；false=关闭儿童安全锁；不是车门开合")}, {"side": "left", "enabled": True}, child_lock, ["locks.child_lock.{side}"])
set_simple("locks", "set_auto_lock", "开关行车自动落锁偏好", "auto_lock", boolean("是否开启"), True)
set_simple("locks", "set_walk_away_lock", "开关离车自动上锁偏好", "walk_away_lock", boolean("是否开启"), True)
set_simple("locks", "set_charge_port_lock", "锁定或解锁充电口，充电中拒绝解锁", "charge_port_locked", boolean("是否锁定"), False, "locked", lambda a, s: ensure(not (not a["locked"] and s.get("_meta", {}).get("charging", False)), "充电中拒绝解锁充电口"))

# 尾门：4
skill("tailgate", "尾门", "电动尾门开关、开启高度与停止，行驶中不能打开。")
get_group("tailgate", "get_tailgate_state", "读取尾门开合与高度状态")


def tailgate_open(a, s):
    if a["open"]:
        stopped(s, "车辆行驶中拒绝打开尾门")
    s["tailgate"].update(open=a["open"], position=s["tailgate"]["height"] if a["open"] else 0,
                         moving=False, last_action="opened" if a["open"] else "closed")
    return s["tailgate"]


register("tailgate", "set_tailgate_open", "开启或关闭尾门，模拟执行立即完成，行驶中拒绝开启", {"open": boolean("是否打开")}, {"open": True}, tailgate_open, ["tailgate.open", "tailgate.position", "tailgate.moving", "tailgate.last_action"])
set_simple("tailgate", "set_tailgate_height", "设置尾门下次开启高度百分比", "height", number(20, 100, "开启高度百分比", True), 80, "height", lambda a, s: stopped(s))


def stop_tailgate(a, s):
    s["tailgate"].update(moving=False, last_action="stopped")
    return s["tailgate"]


register("tailgate", "stop_tailgate", "停止模拟尾门运动并保留当前位置", {}, {}, stop_tailgate, ["tailgate.moving", "tailgate.last_action"])

# 雨刷清洗：6
skill("wipers", "雨刷清洗", "前后雨刷模式、间隔、雨量灵敏度与玻璃清洗液模拟消耗。")
get_group("wipers", "get_wiper_state", "读取雨刷与玻璃水状态")
set_simple("wipers", "set_wiper_mode", "设置前雨刷关闭、自动、间歇、低速或高速", "mode", enum(["off", "auto", "intermittent", "low", "high"], "雨刷模式"), "auto", "mode")
set_simple("wipers", "set_wiper_interval", "设置间歇雨刷间隔秒数", "interval_seconds", number(1, 15, "间隔秒数", True), 3, "seconds")


def wash(a, s):
    ensure(s["wipers"]["washer_fluid_pct"] > 0, "玻璃水已耗尽，清洗被拒绝")
    s["wipers"]["washer_fluid_pct"] -= 1
    s["wipers"]["wash_count"] += 1
    s["wipers"]["last_wash"] = a["target"]
    return {"target": a["target"], "washer_fluid_pct": s["wipers"]["washer_fluid_pct"], "wash_count": s["wipers"]["wash_count"]}


register("wipers", "wash_windshield", "清洗前或后挡风玻璃，每次模拟消耗1%玻璃水，耗尽则拒绝", {"target": enum(["front", "rear"], "清洗玻璃")}, {"target": "front"}, wash, ["wipers.washer_fluid_pct", "wipers.wash_count", "wipers.last_wash"])
set_simple("wipers", "set_rear_wiper", "开关后雨刷", "rear", boolean("是否开启"), True)
set_simple("wipers", "set_rain_sensitivity", "设置自动雨刷雨量灵敏度", "rain_sensitivity", number(1, 5, "灵敏度档位", True), 4, "level")

# 媒体：10
skill("media", "媒体", "模拟音量、静音、播放状态、曲目、音源、FM频率、声场与均衡器。")
get_group("media", "get_media_state", "读取模拟媒体状态")
set_simple("media", "set_volume", "设置媒体音量百分比", "volume", number(0, 100, "音量百分比", True), 40, "volume")


def adjust_volume(a, s):
    value = s["media"]["volume"] + a["delta"]
    ensure(0 <= value <= 100, "音量调整后超出0至100，已保留原音量")
    s["media"]["volume"] = value
    return value


register("media", "adjust_volume", "相对调高或调低媒体音量", {"delta": number(-30, 30, "音量变化百分比", True)}, {"delta": 5}, adjust_volume, ["media.volume"], "adjust")
set_simple("media", "set_mute", "开关媒体静音", "mute", boolean("是否静音"), True)


def playback(a, s):
    action = a["action"]
    if action in ["next", "previous"]:
        s["media"]["track_index"] = max(1, s["media"]["track_index"] + (1 if action == "next" else -1))
        s["media"]["title"] = f"模拟曲目 {s['media']['track_index']}"
        s["media"]["playback"] = "playing"
    else:
        s["media"]["playback"] = {"play": "playing", "pause": "paused", "stop": "stopped"}[action]
    return {"playback": s["media"]["playback"], "title": s["media"]["title"]}


register("media", "control_playback", "播放、暂停、停止、下一首或上一首，仅更新模拟媒体状态", {"action": enum(["play", "pause", "stop", "next", "previous"], "播放操作")}, {"action": "pause"}, playback, ["media.playback", "media.track_index", "media.title"])
set_simple("media", "set_media_source", "切换蓝牙、广播、USB或在线模拟音源", "source", enum(["bluetooth", "radio", "usb", "online"], "音源"), "radio", "source")


def play_media(a, s):
    s["media"].update(title=a["title"].strip(), playback="playing")
    return {"title": s["media"]["title"], "playback": s["media"]["playback"], "simulated": True}


register("media", "play_media", "按名称播放模拟曲目；不进行真实版权内容搜索或音频播放", {"title": string("曲目或内容名称")}, {"title": "夜曲"}, play_media, ["media.title", "media.playback"])
set_simple("media", "set_radio_frequency", "设置模拟FM广播频率，单位MHz", "radio_frequency_mhz", number(87.5, 108, "FM频率MHz"), 101.7, "frequency")


def balance(a, s):
    s["media"].update(balance=a["balance"], fader=a["fader"])
    return {"balance": a["balance"], "fader": a["fader"]}


register("media", "set_audio_balance", "设置音响左右平衡和前后声场，负数偏左或前，正数偏右或后", {"balance": number(-10, 10, "左右平衡", True), "fader": number(-10, 10, "前后声场", True)}, {"balance": 0, "fader": -2}, balance, ["media.balance", "media.fader"])
set_simple("media", "set_equalizer", "设置模拟音效均衡器预设", "equalizer", enum(["flat", "pop", "rock", "classical", "voice"], "均衡器预设"), "voice", "preset")

# 导航：8
skill("navigation", "导航", "只保存模拟目的地、最多10个途经点、通行偏好及家和公司地址；不计算真实路线。")
get_group("navigation", "get_navigation_state", "读取模拟导航目的地、途经点与偏好，不含真实路线")


def destination(a, s):
    s['navigation'].pop('destination_place', None)
    s["navigation"].update(destination=a["destination"].strip(), active=True)
    return {"destination": s["navigation"]["destination"], "simulated": True, "notice": s["navigation"]["notice"]}


register("navigation", "set_navigation_destination", "设置并开始模拟导航目的地；不计算真实路线、不订座支付", {"destination": string("完整保留用户给出的目的地名称或地址原文，禁止缩写、概括、翻译或替换城市名")}, {"destination": "巴黎卢浮宫"}, destination, ["navigation.destination", "navigation.active"])


def add_waypoint(a, s):
    ensure(len(s["navigation"]["waypoints"]) < 10, "最多支持10个模拟途经点")
    s["navigation"]["waypoints"].append(a["destination"].strip())
    return s["navigation"]["waypoints"]


register("navigation", "add_navigation_waypoint", "追加一个模拟途经点，最多10个，不计算真实路线", {"destination": string("途经点名称或地址")}, {"destination": "附近充电站"}, add_waypoint, ["navigation.waypoints"], "append")


def remove_waypoint(a, s):
    ensure(a["index"] < len(s["navigation"]["waypoints"]), "该索引不存在途经点，已保留原列表")
    removed = s["navigation"]["waypoints"].pop(a["index"])
    return {"removed": removed, "waypoints": s["navigation"]["waypoints"]}


register("navigation", "remove_navigation_waypoint", "删除指定索引的模拟途经点，索引从0开始", {"index": number(0, 9, "从0开始的途经点索引", True)}, {"index": 0}, remove_waypoint, ["navigation.waypoints"], "remove")


def preferences(a, s):
    s["navigation"]["preferences"] = deepcopy(a)
    return {"preferences": a, "simulated": True, "notice": "偏好已保存，未计算真实路线"}


register("navigation", "set_navigation_preferences", "保存模拟避收费、避高速与优先最快偏好；不计算真实路线", {"avoid_tolls": boolean("避开收费路段"), "avoid_highways": boolean("避开高速"), "prefer_fastest": boolean("优先最快")}, {"avoid_tolls": True, "avoid_highways": False, "prefer_fastest": True}, preferences, ["navigation.preferences"])


def cancel_navigation(a, s):
    s['navigation'].pop('destination_place', None)
    s["navigation"].update(active=False, destination=None, waypoints=[])
    return s["navigation"]


register("navigation", "cancel_navigation", "结束模拟导航并清空当前目的地和途经点", {}, {}, cancel_navigation, ["navigation.active", "navigation.destination", "navigation.waypoints"])
set_simple("navigation", "set_home_destination", "保存家的模拟导航地址", "home", string("家的地址"), "巴黎市中心", "destination")
set_simple("navigation", "set_work_destination", "保存公司的模拟导航地址", "work", string("公司的地址"), "拉德芳斯", "destination")

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
        result["reason"] = str(exc) or "模拟车端拒绝执行"
        result["reason_code"] = getattr(exc, "reason_code", "vehicle_rejected")
        result["resume_condition"] = getattr(exc, "resume_condition", None)
    result["state"] = deepcopy(state)
    return result


def load_skill(skill_id: str) -> dict:
    if skill_id not in SKILLS:
        return {"ok": False, "reason": "未知skill_id", "available_skills": list(SKILLS)}
    manifest = SKILLS[skill_id]
    return {"ok": True, "skill": manifest.model_dump(), "tools": [TOOLS[name].model_dump() for name in manifest.tool_names]}
