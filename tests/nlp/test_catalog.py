from copy import deepcopy
import math

import pytest
from jsonschema import Draft202012Validator

from super_xd.nlp.catalog.vehicle import EXPECTED_COUNTS, HOT_TOOL_NAMES, COLD_TOOL_NAMES
from super_xd.nlp.prompts.catalog import SKILLS, TOOLS, load_skill
from super_xd.demo.devices.simulator import execute_tool, initial_vehicle_state


def test_exact_catalog_and_manifests():
    assert len(TOOLS) == len(set(TOOLS)) == 100
    assert len(SKILLS) == 10
    assert {key: len(skill.tool_names) for key, skill in SKILLS.items()} == EXPECTED_COUNTS
    assert set(name for skill in SKILLS.values() for name in skill.tool_names) == set(TOOLS)
    assert len(HOT_TOOL_NAMES) == len(set(HOT_TOOL_NAMES)) == 15
    assert len(COLD_TOOL_NAMES) == len(set(COLD_TOOL_NAMES)) == 85
    assert set(HOT_TOOL_NAMES).isdisjoint(COLD_TOOL_NAMES)
    assert set(HOT_TOOL_NAMES) | set(COLD_TOOL_NAMES) == set(TOOLS)
    assert set(HOT_TOOL_NAMES) <= set(TOOLS)
    for name, tool in TOOLS.items():
        assert tool.name == name and tool.description and tool.skill_id in SKILLS
        assert tool.state_mapping["paths"] and "simulated" not in tool.state_mapping
        Draft202012Validator.check_schema(tool.parameters)
        Draft202012Validator(tool.parameters).validate(tool.example)
        assert tool.openai_tool()["function"]["parameters"] == tool.parameters


@pytest.mark.parametrize("name", list(TOOLS))
def test_every_example_has_a_working_executor(name):
    state = initial_vehicle_state()
    if name == "remove_navigation_waypoint":
        execute_tool("add_navigation_waypoint", {"destination": "模拟途经点"}, state)
    result = execute_tool(name, TOOLS[name].example, state)
    assert result["ok"], result
    assert "value" in result and result["state"] == state
    assert result["arguments"] == TOOLS[name].example
    assert result["name"] == name


@pytest.mark.parametrize("name", list(TOOLS))
def test_every_schema_rejects_unexpected_parameters_atomically(name):
    state = initial_vehicle_state()
    before = deepcopy(state)
    result = execute_tool(name, {**TOOLS[name].example, "unexpected": True}, state)
    assert not result["ok"] and result["reason"]
    assert state == before == result["state"]


@pytest.mark.parametrize("name", [name for name, tool in TOOLS.items() if tool.parameters["properties"]])
def test_required_parameters_and_wrong_types_rejected(name):
    state = initial_vehicle_state()
    before = deepcopy(state)
    example = deepcopy(TOOLS[name].example)
    key = next(iter(example))
    del example[key]
    assert not execute_tool(name, example, state)["ok"]
    example = deepcopy(TOOLS[name].example)
    example[key] = ["invalid"]
    assert not execute_tool(name, example, state)["ok"]
    assert state == before


def test_numeric_schema_bounds_are_enforced_for_every_function():
    for name, tool in TOOLS.items():
        for key, schema in tool.parameters["properties"].items():
            if "maximum" not in schema:
                continue
            for value in [schema["minimum"] - 1, schema["maximum"] + 1]:
                state = initial_vehicle_state()
                before = deepcopy(state)
                arguments = {**tool.example, key: value}
                assert not execute_tool(name, arguments, state)["ok"], (name, key, value)
                assert state == before


def test_set_read_roundtrips_all_subsystems():
    state = initial_vehicle_state()
    operations = [
        ("set_temperature", {"zone": "all", "temperature": 23.5}, "get_climate_state", {}, lambda v: v["temperatures"]["rear"], 23.5),
        ("set_seat_heating", {"seat": "rear_right", "level": 3}, "get_seat_state", {"seat": "rear_right"}, lambda v: v["heating"], 3),
        ("set_window_position", {"window": "driver", "position": 70}, "get_window_state", {"window": "driver"}, lambda v: v["position"], 70),
        ("set_mirror_angle", {"mirror": "right", "horizontal": 4, "vertical": -2}, "get_mirror_state", {"mirror": "right"}, lambda v: v["vertical"], -2),
        ("set_ambient_color", {"color": "purple"}, "get_light_state", {}, lambda v: v["ambient_color"], "purple"),
        ("unlock_all_doors", {}, "get_lock_state", {}, lambda v: v["doors"]["driver"], False),
        ("set_tailgate_open", {"open": True}, "get_tailgate_state", {}, lambda v: v["open"], True),
        ("set_wiper_mode", {"mode": "high"}, "get_wiper_state", {}, lambda v: v["mode"], "high"),
        ("set_volume", {"volume": 73}, "get_media_state", {}, lambda v: v["volume"], 73),
        ("set_navigation_destination", {"destination": "上海博物馆"}, "get_navigation_state", {}, lambda v: v["destination"], "上海博物馆"),
    ]
    for setter, args, getter, read_args, read, expected in operations:
        assert execute_tool(setter, args, state)["ok"]
        result = execute_tool(getter, read_args, state)
        assert result["ok"] and read(result["value"]) == expected
    assert state["navigation"]["simulated"] and "未计算真实路线" in state["navigation"]["notice"]


@pytest.mark.parametrize("name,args", [
    ("adjust_temperature", {"zone": "all", "delta": 5}),
    ("adjust_fan_speed", {"delta": 3}),
    ("adjust_volume", {"delta": 30}),
    ("adjust_window_position", {"window": "driver", "delta": 100}),
    ("adjust_mirror_angle", {"mirror": "left", "axis": "horizontal", "delta": 10}),
])
def test_relative_out_of_range_preserves_whole_state(name, args):
    state = initial_vehicle_state()
    state["climate"]["temperatures"]["rear"] = 30
    state["climate"]["fan_speed"] = 7
    state["media"]["volume"] = 100
    state["windows"]["positions"]["driver"] = 100
    state["mirrors"]["positions"]["left"]["horizontal"] = 20
    before = deepcopy(state)
    result = execute_tool(name, args, state)
    assert not result["ok"] and result["reason"]
    assert state == before


def test_bulk_window_failure_does_not_partially_open_front_windows():
    state = initial_vehicle_state()
    state["windows"]["child_lock"] = True
    before = deepcopy(state)
    result = execute_tool("open_all_windows", {}, state)
    assert not result["ok"] and "童锁" in result["reason"]
    assert state == before
    state["windows"]["positions"] = {window: 70 for window in state["windows"]["positions"]}
    assert execute_tool("close_all_windows", {}, state)["ok"]
    assert all(value == 0 for value in state["windows"]["positions"].values())


@pytest.mark.parametrize("name,args", [
    ("set_tailgate_open", {"open": True}),
    ("unlock_all_doors", {}),
    ("set_mirror_fold", {"folded": True}),
    ("set_window_position", {"window": "driver", "position": 100}),
    ("set_seat_position", {"seat": "driver", "axis": "fore_aft", "position": 60}),
    ("recall_seat_memory", {"seat": "driver", "slot": 1}),
    ("set_steering_position", {"axis": "height", "position": 60}),
])
def test_safety_refusal_at_speed_is_atomic(name, args):
    state = initial_vehicle_state()
    state["_meta"]["speed_kph"] = 90
    before = deepcopy(state)
    result = execute_tool(name, args, state)
    assert not result["ok"] and state == before


def test_resource_refusal_charging_and_empty_washer():
    state = initial_vehicle_state()
    state["_meta"]["charging"] = True
    state["wipers"]["washer_fluid_pct"] = 0
    before = deepcopy(state)
    assert not execute_tool("set_charge_port_lock", {"locked": False}, state)["ok"]
    assert not execute_tool("wash_windshield", {"target": "front"}, state)["ok"]
    assert state == before


def test_seat_memory_restores_previous_real_simulated_values():
    state = initial_vehicle_state()
    execute_tool("set_seat_position", {"seat": "driver", "axis": "fore_aft", "position": 65}, state)
    execute_tool("save_seat_memory", {"seat": "driver", "slot": 2}, state)
    execute_tool("set_seat_position", {"seat": "driver", "axis": "fore_aft", "position": 20}, state)
    execute_tool("recall_seat_memory", {"seat": "driver", "slot": 2}, state)
    assert execute_tool("get_seat_state", {"seat": "driver"}, state)["value"]["fore_aft"] == 65


def test_navigation_waypoint_limit_missing_index_and_cancellation():
    state = initial_vehicle_state()
    assert not execute_tool("remove_navigation_waypoint", {"index": 0}, state)["ok"]
    for i in range(10):
        assert execute_tool("add_navigation_waypoint", {"destination": f"点{i}"}, state)["ok"]
    before = deepcopy(state)
    assert not execute_tool("add_navigation_waypoint", {"destination": "第11个"}, state)["ok"]
    assert state == before
    assert execute_tool("remove_navigation_waypoint", {"index": 3}, state)["value"]["removed"] == "点3"
    execute_tool("set_navigation_destination", {"destination": "终点"}, state)
    assert execute_tool("cancel_navigation", {}, state)["ok"]
    assert state["navigation"]["active"] is False and state["navigation"]["waypoints"] == []


def test_receipts_are_independent_and_nonfinite_arguments_rejected():
    state = initial_vehicle_state()
    before = deepcopy(state)
    for value in [math.nan, math.inf, -math.inf]:
        assert not execute_tool("set_temperature", {"zone": "all", "temperature": value}, state)["ok"]
        assert state == before
    result = execute_tool("set_volume", {"volume": 50}, state)
    result["state"]["media"]["volume"] = 99
    assert state["media"]["volume"] == 50
    assert not execute_tool("does_not_exist", {}, state)["ok"]


def test_load_every_skill_contains_complete_schemas():
    for skill_id in SKILLS:
        loaded = load_skill(skill_id)
        assert loaded["ok"]
        assert {tool["name"] for tool in loaded["tools"]} == set(SKILLS[skill_id].tool_names)
        assert all(tool["parameters"] and tool["state_mapping"] for tool in loaded["tools"])
    assert not load_skill("unknown")["ok"]


def test_free_text_patterns_allow_full_place_names_in_strict_decoding():
    for tool in TOOLS.values():
        for prop in tool.parameters['properties'].values():
            if prop.get('type') == 'string' and 'pattern' in prop:
                validator = Draft202012Validator(prop)
                for value in ('上海虹桥站', 'Paris Gare de Lyon', '上海市\n虹桥路123号'):
                    assert validator.is_valid(value)
                assert not validator.is_valid(' \t\n')
