"""Pure local oracle/identity checks; no provider, device, HTTP or WebSocket calls."""
import asyncio
import copy
import struct
import pytest

from scripts.evaluate_voice_interactions import (
    PCMConsumer, SCENARIOS, background_errors, background_weather_evidence, claims_all_windows_closed, normalize_transcript,
    resample_pcm, semantic_errors, transport_errors,
)


def state():
    return {"climate": {"power": False, "fan_speed": 3, "temperatures": {"driver": 23, "front_passenger": 23, "rear": 23}},
            "windows": {"positions": {"driver": 0, "front_passenger": 0, "rear_left": 0, "rear_right": 0}},
            "seats": {"positions": {"driver": {"massage": "off", "heating": 0, "ventilation": 0}}}}


def compound():
    before = state()
    after = copy.deepcopy(before)
    after["climate"]["power"] = True
    after["seats"]["positions"]["driver"]["massage"] = "relax"
    commands = [{"name": "set_climate_power", "arguments": {"enabled": True}},
                {"name": "close_all_windows", "arguments": {}},
                {"name": "set_seat_massage", "arguments": {"seat": "driver", "mode": "relax"}}]
    return {"before": before, "after": after, "commands": commands,
            "receipts": [{"result": {"ok": True}} for _ in commands],
            "output": "空调开了，车窗全关了，主驾按摩打开了。", "turn_id": "turn"}


def test_compound_oracle_rejects_extra_valid_commands_and_state_changes():
    attempt = compound()
    assert semantic_errors(SCENARIOS[1], attempt, []) == []
    attempt["commands"].append({"name": "set_seat_heating", "arguments": {"seat": "driver", "level": 0}})
    attempt["receipts"].append({"result": {"ok": True}})
    assert "unexpected_command_set" in semantic_errors(SCENARIOS[1], attempt, [])
    attempt = compound()
    attempt["after"]["climate"]["fan_speed"] = 2
    assert "unexpected_state_change:climate.fan_speed" in semantic_errors(SCENARIOS[1], attempt, [])


def test_current_state_question_cannot_pass_on_old_task_answer_or_task_get():
    attempt = {"before": state(), "after": state(), "commands": [], "receipts": [], "output": "全部车窗关着。", "turn_id": "new"}
    assert semantic_errors(SCENARIOS[2], attempt, []) == []
    attempt["output"] = "无法获取定位，不能判断最近的机场。"
    assert "feedback_does_not_answer_current_window_state" in semantic_errors(SCENARIOS[2], attempt, [])
    attempt["output"] = "全部车窗关着。"
    trace = {"turn_id": "new", "kind": "tool.started", "data": {"name": "manage_task"}}
    assert "state_question_routed_to_task" in semantic_errors(SCENARIOS[2], attempt, [trace])
    attempt["output"] = "车窗不是关闭状态，是开着的。"
    assert "feedback_does_not_answer_current_window_state" in semantic_errors(SCENARIOS[2], attempt, [])


def read_windows_attempt():
    attempt = {"before": state(), "after": state(), "commands": [], "receipts": [], "output": "四扇车窗全部关闭。", "turn_id": "turn"}
    for window in state()["windows"]["positions"]:
        args = {"window": window}
        attempt["commands"].append({"command_id": window, "name": "get_window_state", "arguments": args})
        attempt["receipts"].append({"command_id": window, "turn_id": "turn", "result": {"ok": True, "name": "get_window_state", "arguments": args,
                                    "value": {"position": 0}, "state": state()}})
    return attempt


def test_real_window_reads_are_permitted_only_with_matching_actual_values_and_receipts():
    attempt = read_windows_attempt()
    assert semantic_errors(SCENARIOS[6], attempt, []) == []
    assert len(attempt["verified_readonly_commands"]) == 4
    attempt["receipts"][0]["result"]["value"]["position"] = 50
    assert "window_read_missing_or_wrong_actual_receipt" in semantic_errors(SCENARIOS[6], attempt, [])


def test_no_receipt_duplicate_read_wrong_turn_or_extra_write_cannot_use_read_exemption():
    for modification in ("missing", "wrong_turn", "write", "duplicate"):
        attempt = read_windows_attempt()
        if modification == "missing":
            attempt["receipts"].pop()
        elif modification == "wrong_turn":
            attempt["receipts"][0]["turn_id"] = "other"
        elif modification == "write":
            attempt["commands"][0]["name"] = "close_all_windows"
        else:
            attempt["commands"].append(copy.deepcopy(attempt["commands"][0]))
        assert semantic_errors(SCENARIOS[6], attempt, [])


def test_receipt_and_registration_order_need_matching_observed_clock():
    attempt = {"client_request_id": "request", "turn_id": "turn", "receipts": [{"turn_id": "turn", "received_mono_ms": 12}]}
    record = {"client_request_id": "request", "turn_id": "turn", "first_nonzero_pcm_mono_ms": 10, "status": "ended"}
    assert "audio_preceded_matching_execution_receipt" in transport_errors(SCENARIOS[0], attempt, record)
    attempt["receipts"][0]["received_mono_ms"] = 9
    assert not transport_errors(SCENARIOS[0], attempt, record)
    assert "audio_preceded_task_registration" in transport_errors(SCENARIOS[5], attempt, record)
    attempt["task_registered_mono_ms"] = 8
    assert not transport_errors(SCENARIOS[5], attempt, record)


@pytest.mark.parametrize("text", ["车窗没关。", "所有车窗未关闭。", "四扇车窗不是关闭状态。", "其他都关了，主驾仍开着。", "主驾车窗全关了。", "除主驾外全部车窗都关闭了。"])
def test_closed_window_oracle_rejects_negation_contradiction_and_partial_claim(text):
    assert not claims_all_windows_closed(text)


@pytest.mark.parametrize("text", ["所有车窗已关闭。", "四扇车窗都处于全关状态。", "全部车窗都是关闭的。", "车窗都关着。"])
def test_closed_window_oracle_requires_a_universal_positive_claim(text):
    assert claims_all_windows_closed(text)


def test_unintentional_cancel_cannot_pass_as_complete_audio():
    attempt = {"client_request_id": "r", "turn_id": "t"}
    record = {"client_request_id": "r", "turn_id": "t", "status": "cancelled", "first_nonzero_pcm_mono_ms": 5}
    assert "natural_audio_terminal_not_observed" in transport_errors(SCENARIOS[0], attempt, record)
    assert "server_cancel_not_observed" in transport_errors(SCENARIOS[4], attempt, record)


def test_transcript_comparison_only_tolerates_punctuation_and_explicit_number():
    assert normalize_transcript("二十三度。") == normalize_transcript("23度！")
    assert normalize_transcript("打开主驾按摩") != normalize_transcript("关闭主驾按摩")


def test_fixture_resampling_preserves_signal_and_duration():
    source = struct.pack("<" + "h" * 240, *([1200] * 240))
    output = resample_pcm(source)
    assert len(output) == 160 * 2
    assert set(value for (value,) in struct.iter_unpack("<h", output)) == {1200}


def test_background_needs_completed_full_and_spoken_result_and_real_official_extract():
    task = {"id": "t", "status": "completed", "context": {"captured_at": "2026-10-13T05:00:00+00:00", "read_details": False},
            "result": {"summary": "巴黎明天大约11到19摄氏度。", "spoken_summary": "巴黎明天大约11到19摄氏度。", "sources": []}}
    traces = [{"task_id": "t", "kind": "tool.completed", "data": {"name": tool, "result": {"ok": True}}} for tool in ("search_web", "extract_web")]
    assert "official_weather_source_not_extracted:t" in background_errors(["t"], {"tasks": [task]}, traces)
    task["result"]["sources"] = [{"url": "https://meteofrance.com/previsions-meteo-france/paris/75000", "extracted": True}]
    traces[1]["data"]["result"]["results"] = [{"url": task["result"]["sources"][0]["url"], "raw_content": "Daily Forecast in Paris\n2026-10-14 | 11 / 19°C"}]
    assert background_errors(["t"], {"tasks": [task]}, traces) == []
    assert "missing_actual_extract_web:t" in background_errors(["t"], {"tasks": [task]}, traces[:1])


def weather_fixture(*, dated=True):
    url = "https://meteofrance.com/previsions-meteo-france/paris/75000"
    spoken = "巴黎明天大约11到19摄氏度。" if dated else "尚未核实巴黎明天的最低和最高温度。"
    task = {"id": "t", "status": "completed", "context": {"captured_at": "2026-10-13T05:00:00+00:00", "read_details": False},
            "result": {"summary": spoken, "spoken_summary": spoken, "sources": [{"url": url, "extracted": True}]}}
    raw = "Daily Forecast in Paris\n2026-10-14 | 11 / 19°C" if dated else "Paris forecast overview with no dated temperatures."
    traces = [{"task_id": "t", "kind": "tool.completed", "data": {"name": "extract_web", "result": {"ok": True, "results": [{"url": url, "raw_content": raw}]}}}]
    return task, traces


def test_brief_delegation_must_not_enable_read_details():
    attempt = {"before": state(), "after": state(), "commands": [], "receipts": [], "output": "我帮你查天气。", "turn_id": "turn", "registered_task_ids": ["t"]}
    trace = {"turn_id": "turn", "kind": "tool.started", "data": {"name": "delegate_task", "arguments": '{"goal":"天气","read_details":true}'}}
    assert "brief_delegation_enabled_detailed_reading" in semantic_errors(SCENARIOS[5], attempt, [trace])
    trace["data"]["arguments"] = '{"goal":"天气","read_details":false}'
    assert semantic_errors(SCENARIOS[5], attempt, [trace]) == []


def test_official_flag_without_matching_actual_raw_page_cannot_pass():
    task, traces = weather_fixture()
    assert not background_weather_evidence(task, traces)["validation_errors"]
    traces[0]["data"]["result"]["results"][0]["url"] = "https://example.com/forecast"
    assert "official_weather_page_not_actually_read" in background_weather_evidence(task, traces)["validation_errors"]


def test_undated_or_historical_official_page_requires_spoken_uncertainty():
    task, traces = weather_fixture(dated=False)
    assert not background_weather_evidence(task, traces)["validation_errors"]
    task["result"]["spoken_summary"] = "巴黎明天11到19摄氏度。"
    errors = background_weather_evidence(task, traces)["validation_errors"]
    assert "weather_temperature_not_supported_by_actual_dated_source:spoken_summary" in errors
    assert "official_target_day_unverified_but_limitation_missing:spoken_summary" in errors


def test_historical_table_and_wrong_temperature_do_not_become_forecasts():
    task, traces = weather_fixture()
    task["result"]["spoken_summary"] = "巴黎明天大约30到40摄氏度。"
    assert "weather_temperature_not_supported_by_actual_dated_source:spoken_summary" in background_weather_evidence(task, traces)["validation_errors"]
    traces[0]["data"]["result"]["results"][0]["raw_content"] = "Climate Averages in Paris\n2026-10-14 | 11 / 19°C"
    assert "official_target_day_unverified_but_limitation_missing:summary" in background_weather_evidence(task, traces)["validation_errors"]


def test_successful_structured_forecast_must_match_its_actual_raw_and_retained_record():
    task, traces = weather_fixture()
    location = {"name": "Paris", "country_code": "FR", "timezone": "Europe/Paris"}
    forecast = {"ok": True, "data_type": "forecast", "date": "2026-10-14", "location": location, "timezone": "Europe/Paris", "temperature_min": 11, "temperature_max": 19, "unit": "°C", "source_url": "https://api.open-meteo.com/v1/forecast", "queried_at": "2026-10-13T05:01:00+00:00",
                "raw": {"forecast": {"timezone": "Europe/Paris", "daily": {"time": ["2026-10-14"], "temperature_2m_min": [11], "temperature_2m_max": [19]}, "daily_units": {"temperature_2m_min": "°C", "temperature_2m_max": "°C"}}}}
    task["result"]["sources"].append({"url": forecast["source_url"], "kind": "forecast", "verified": True, "extracted": False})
    keys = ("date", "location", "timezone", "temperature_min", "temperature_max", "unit", "source_url", "queried_at")
    task["result"]["forecast_records"] = [{key: forecast[key] for key in keys}]
    traces.append({"task_id": "t", "kind": "tool.completed", "data": {"name": "get_weather_forecast", "result": forecast}})
    assert not background_weather_evidence(task, traces)["validation_errors"]
    forecast["raw"]["forecast"]["daily"]["time"] = ["2026-10-13"]
    assert "structured_forecast_disagrees_with_target_or_actual_raw" in background_weather_evidence(task, traces)["validation_errors"]


def test_open_meteo_cannot_be_presented_as_requested_official_provider():
    task, traces = weather_fixture()
    task["result"]["spoken_summary"] = "Open-Meteo官方预报说巴黎明天大约11到19摄氏度。"
    assert "open_meteo_falsely_called_official:spoken_summary" in background_weather_evidence(task, traces)["validation_errors"]


def test_pcm_consumer_drops_old_identity_after_cancel_and_does_not_count_silence():
    class FakeHarness:
        sid = "session"
        errors = []
        changed = asyncio.Event()
        def now(self):
            return 10
        async def send(self, _):
            pass
    async def check():
        consumer = PCMConsumer(FakeHarness())
        identity = "a" * 32
        await consumer.metadata({"type": "audio.started", "session_id": "session", "turn_id": "turn", "playback_id": identity,
                                 "client_request_id": "request", "sample_rate": 24000, "channels": 1, "format": "pcm_s16le"})
        consumer.pcm(identity.encode() + b"\0\0")
        assert "first_nonzero_pcm_mono_ms" not in consumer.records[identity]
        consumer.pcm(identity.encode() + b"\1\0")
        assert consumer.records[identity]["first_nonzero_pcm_mono_ms"] == 10
        accepted = consumer.records[identity]["accepted_pcm_bytes"]
        await consumer.cancel("user_stop")
        consumer.pcm(identity.encode() + b"\1\0")
        assert consumer.records[identity]["accepted_pcm_bytes"] == accepted
        assert len(consumer.dropped) == 1
    asyncio.run(check())
