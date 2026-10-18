"""Editable wording must not become an editable vehicle capability boundary."""
from copy import deepcopy
from pathlib import Path

import super_xd.nlp.catalog.vehicle as structure
import super_xd.nlp.prompts.catalog as catalog
import super_xd.nlp.prompts.config as prompt_config
import super_xd.nlp.prompts.speech as speech_config
from super_xd.demo.devices.simulator import initial_vehicle_state, execute_tool


def load_catalog_with_text_override(monkeypatch, replacements):
    original = prompt_config.config_text

    def configured_text(file, key, **variables):
        if (file, key) in replacements:
            return replacements[file, key]
        return original(file, key, **variables)

    monkeypatch.setattr(prompt_config, "config_text", configured_text)
    monkeypatch.setattr(speech_config, "config_text", configured_text)
    namespace = {"__name__": "super_xd.nlp.prompts.catalog_config_test", "__package__": "super_xd.nlp.prompts"}
    source = Path(catalog.__file__).read_text(encoding="utf-8")
    exec(compile(source, catalog.__file__, "exec"), namespace)
    return namespace


def test_editing_capability_wording_preserves_all_structural_schemas_and_permissions(monkeypatch):
    replacements = {
        ("vehicle/functions.toml", "functions.set_temperature.description"): "编辑后的温度说明",
        ("vehicle/functions.toml", "functions.set_temperature.parameters.temperature"): "编辑后的参数说明",
        ("vehicle/functions.toml", "skills.climate.name"): "编辑后的空调名称",
    }
    hot_before = tuple(structure.HOT_TOOL_NAMES)
    cold_before = tuple(structure.COLD_TOOL_NAMES)
    configured = load_catalog_with_text_override(monkeypatch, replacements)
    assert configured["SKILLS"]["climate"].name == "编辑后的空调名称"
    assert tuple(structure.HOT_TOOL_NAMES) == hot_before
    assert tuple(structure.COLD_TOOL_NAMES) == cold_before
    for name, tool in configured["TOOLS"].items():
        expected = catalog.TOOLS[name].model_dump()
        if name == "set_temperature":
            expected["description"] = "编辑后的温度说明"
            expected["parameters"]["properties"]["temperature"]["description"] = "编辑后的参数说明"
        assert tool.model_dump() == expected
    state = initial_vehicle_state()
    before = deepcopy(state)
    result = execute_tool("set_temperature", {"zone": "driver", "temperature": 31}, state)
    assert not result["ok"] and state == before


def test_editing_physical_rejection_changes_words_but_preserves_refusal_and_resume_condition(monkeypatch):
    configured = load_catalog_with_text_override(monkeypatch, {
        ("speech/vehicle.toml", "rejections.driver_seat_moving"): "编辑后的停车提示",
    })
    state = initial_vehicle_state()
    state["_meta"]["speed_kph"] = 30
    before = deepcopy(state)
    result = execute_tool("set_seat_position", {
        "seat": "driver", "axis": "height", "position": 60,
    }, state)
    assert not result["ok"] and result["reason"] == "编辑后的停车提示"
    assert result["reason_code"] == "vehicle_moving"
    assert result["resume_condition"] == {"speed_kph": 0}
    assert state == before == result["state"]
