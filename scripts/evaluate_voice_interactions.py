"""Bounded real ASR -> query -> PCM interaction evaluation, never a microphone test.

Default prints a plan without network calls. Use --run only against the intended
live service. Chinese input fixtures are real Xiaohe synthesis cached by text SHA;
no recognizer/model answer is replaced by fixture text. All failed attempts stay
in the output, and an existing output file is never overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import time
import unicodedata
from uuid import uuid4
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import websockets
from scripts.vehicle_device import execute_vehicle

from scripts.evaluate_interactions import (
    extracted_forecast_records, extracted_forecast_report_matches,
    forecast_date_evidence, normalized_text, report_limitations, temperature_range_facts,
)

ORACLE_VERSION = "voice-interaction-v3-verified-window-reads"


SCENARIOS = [
    {"id": "single_control", "text": "把所有温区的空调温度设为二十三度。",
     "commands": ["set_temperature"], "allowed_changes": ["climate.temperatures", "climate.power"],
     "semantic": "temperature"},
    {"id": "compound_control", "text": "打开空调，关闭全部车窗，打开主驾座椅按摩。其他设置不要调整。",
     "commands": ["set_climate_power", "close_all_windows", "set_seat_massage"],
     "allowed_changes": ["climate.power", "windows.positions", "seats.positions.driver.massage"],
     "semantic": "compound"},
    {"id": "state_question", "text": "现在全部车窗都是关着的吗？",
     "commands": [], "allowed_changes": [], "semantic": "windows"},
    {"id": "negative_correction", "text": "不是关闭座椅按摩，是打开主驾座椅按摩。",
     "commands": ["set_seat_massage"], "allowed_changes": ["seats.positions.driver.massage"],
     "semantic": "massage"},
    {"id": "chat", "text": "今天终于把拖了很久的小事做完了，陪我轻松聊一句。",
     "commands": [], "allowed_changes": [], "semantic": "chat", "cancel_after_first_pcm": True},
    {"id": "task_delegation", "text": "帮我查一下明天巴黎的天气，读取法国气象局官网，简短告诉我温度。",
     "commands": [], "allowed_changes": [], "semantic": "delegate", "wait_drain": False},
    {"id": "task_topic_switch", "text": "先不说天气了，现在车窗是开的还是关的？",
     "commands": [], "allowed_changes": [], "semantic": "windows"},
]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def normalize_transcript(text):
    text = text.replace("二十三", "23")
    return "".join(c for c in text if not unicodedata.category(c).startswith(("P", "Z")) and not c.isspace())


def changed_paths(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else key
            paths.extend(changed_paths(before.get(key), after.get(key), path))
        return paths
    return [] if before == after else [prefix]


def claims_all_windows_closed(output):
    """Conservative oracle for this fixed state, not a production intent router."""
    universal_closed = re.search(r"(?:(?:四|4)(?:扇|个)?车窗|(?:全部|所有)(?:的)?车窗|(?:车窗|窗户)(?:全|都)|都|全部).{0,16}(?:关(?:闭|着|上|好)?|闭合)", output)
    contradiction = re.search(r"(?:没(?:有)?|未|不是|并非|不处于).{0,5}关|开着|仍.{0,3}开|处于.{0,4}打开|除.{0,12}外", output)
    specific_seat = re.search(r"主驾|副驾|左前|右前|左后|右后", output)
    explicit_group = re.search(r"(?:四|4)(?:扇|个)?车窗|(?:全部|所有)(?:的)?车窗|全部(?:都|已经|已)?关", output)
    return bool(universal_closed and not contradiction and "机场" not in output and not (specific_seat and not explicit_group))


def resample_pcm(pcm, input_rate=24000, output_rate=16000):
    """Whole-fixture weighted averaging; no fabricated speech or recognition."""
    if not pcm or len(pcm) % 2:
        raise ValueError("Invalid synthesis PCM fixture")
    accumulator = weight = 0
    output = bytearray()
    for (value,) in struct.iter_unpack("<h", pcm):
        remaining = output_rate
        while remaining:
            amount = min(remaining, input_rate - weight)
            accumulator += value * amount
            weight += amount
            remaining -= amount
            if weight == input_rate:
                sample = max(-32768, min(32767, round(accumulator / input_rate)))
                output.extend(struct.pack("<h", sample))
                accumulator = weight = 0
    return bytes(output)


def semantic_errors(scenario, attempt, traces):
    errors = []
    before, after = attempt.get("before", {}), attempt.get("after", {})
    commands = attempt.get("commands", [])
    actionable = []
    verified_reads, read_windows = [], set()
    for command in commands:
        if scenario["semantic"] != "windows" or command.get("name") != "get_window_state":
            actionable.append(command)
            continue
        args = command.get("arguments", {})
        window = args.get("window")
        receipt = next((row for row in attempt.get("receipts", []) if row.get("command_id") == command.get("command_id")), {})
        returned = receipt.get("result", {})
        positions = before.get("windows", {}).get("positions", {})
        valid = (set(args) == {"window"} and window in {"driver", "front_passenger", "rear_left", "rear_right"}
                 and window in positions and window not in read_windows
                 and receipt.get("turn_id") == attempt.get("turn_id") and returned.get("ok") is True
                 and returned.get("name") == "get_window_state" and returned.get("arguments") == args
                 and returned.get("value", {}).get("position") == positions[window]
                 and returned.get("state", {}).get("windows", {}).get("positions") == positions)
        if not valid:
            errors.append("window_read_missing_or_wrong_actual_receipt")
            actionable.append(command)
        else:
            read_windows.add(window)
            verified_reads.append({"command_id": command["command_id"], "window": window,
                                   "actual_position": returned["value"]["position"]})
    attempt["verified_readonly_commands"] = verified_reads
    actual = Counter(c.get("name") for c in actionable)
    expected = Counter(scenario["commands"])
    if actual != expected:
        errors.append("unexpected_command_set")
    for path in changed_paths(before, after):
        if not any(path == allowed or path.startswith(allowed + ".") for allowed in scenario["allowed_changes"]):
            errors.append("unexpected_state_change:" + path)
    if len(attempt.get("receipts", [])) != len(commands) or any(r.get("result", {}).get("ok") is not True for r in attempt.get("receipts", [])):
        errors.append("missing_or_failed_execution_receipt")
    output = attempt.get("output", "")
    if not output.strip():
        errors.append("empty_user_feedback")
    tasks = attempt.get("registered_task_ids", [])
    if scenario["semantic"] != "delegate" and tasks:
        errors.append("unexpected_task_registration")
    current_tools = [e.get("data", {}).get("name") for e in traces
                     if e.get("turn_id") == attempt.get("turn_id") and e.get("kind") == "tool.started"]
    if scenario["semantic"] == "temperature":
        temperatures = after.get("climate", {}).get("temperatures", {})
        if len(temperatures) != 3 or set(temperatures.values()) != {23}:
            errors.append("temperature_not_all_23")
        if commands and commands[0].get("arguments") != {"zone": "all", "temperature": 23}:
            errors.append("wrong_temperature_arguments")
    if scenario["semantic"] in {"compound", "massage"}:
        if after.get("seats", {}).get("positions", {}).get("driver", {}).get("massage") not in {"relax", "lumbar", "wave"}:
            errors.append("driver_massage_not_enabled")
        for command in commands:
            if command.get("name") == "set_seat_massage" and (command.get("arguments", {}).get("seat") != "driver" or command.get("arguments", {}).get("mode") == "off"):
                errors.append("wrong_massage_target_or_negative_polarity")
    if scenario["semantic"] in {"compound", "windows"}:
        positions = after.get("windows", {}).get("positions", {})
        if len(positions) != 4 or any(value != 0 for value in positions.values()):
            errors.append("windows_not_all_closed")
    if scenario["semantic"] == "compound" and after.get("climate", {}).get("power") is not True:
        errors.append("climate_not_enabled")
    if scenario["semantic"] == "windows":
        if not claims_all_windows_closed(output):
            errors.append("feedback_does_not_answer_current_window_state")
        if "manage_task" in current_tools or "delegate_task" in current_tools:
            errors.append("state_question_routed_to_task")
    if scenario["semantic"] == "delegate":
        if len(tasks) != 1:
            errors.append("expected_one_registered_task")
        if not any(token in output for token in ("查", "核对", "天气", "温度")):
            errors.append("missing_delegation_feedback")
        delegated = [e for e in traces if e.get("turn_id") == attempt.get("turn_id")
                     and e.get("kind") == "tool.started" and e.get("data", {}).get("name") == "delegate_task"]
        if len(delegated) != 1:
            errors.append("delegate_parameters_not_uniquely_observed")
        for event in delegated:
            args = event["data"].get("arguments", {})
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                args = None
            if not isinstance(args, dict) or args.get("read_details", False) is not False:
                errors.append("brief_delegation_enabled_detailed_reading")
    return sorted(set(errors))


def transport_errors(scenario, attempt, record):
    errors = []
    if record.get("client_request_id") != attempt.get("client_request_id") or record.get("turn_id") != attempt.get("turn_id"):
        errors.append("audio_query_identity_mismatch")
    first = record.get("first_nonzero_pcm_mono_ms")
    if first is None:
        errors.append("missing_nonzero_reply_pcm")
    if scenario.get("cancel_after_first_pcm"):
        if not record.get("server_terminal") or record["server_terminal"].get("type") != "audio.cancelled":
            errors.append("server_cancel_not_observed")
        if any(packet["accepted"] and packet["received_mono_ms"] > record.get("cancelled_mono_ms", float("inf")) for packet in record.get("packets", [])):
            errors.append("cancelled_pcm_was_accepted")
    elif scenario.get("wait_drain", True) and record.get("status") != "ended":
        errors.append("natural_audio_terminal_not_observed")
    elif record.get("status") not in {"ended", "cancelled"}:
        errors.append("audio_terminal_not_observed")
    if first is not None:
        for receipt in attempt.get("receipts", []):
            if receipt.get("turn_id") != attempt.get("turn_id") or receipt.get("received_mono_ms", float("inf")) > first:
                errors.append("audio_preceded_matching_execution_receipt")
        if scenario["semantic"] == "delegate" and attempt.get("task_registered_mono_ms", float("inf")) > first:
            errors.append("audio_preceded_task_registration")
    return errors


def official_weather_url(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in ("meteofrance.com", "meteofrance.fr"))


def target_uncertainty(text):
    pattern = r"未(?:能)?(?:核实|确认|明确|标明|标注)|没有(?:核实|明确)|无法(?:核实|确认|确定|判断)|不能(?:核实|确认)|尚未确认"
    for match in re.finditer(pattern, text):
        if not re.search(r"不是|并非", text[max(0, match.start() - 4):match.start()]):
            return True
    return False


def background_weather_evidence(task, traces):
    """Fixed Paris/tomorrow contract; unparsed source text never becomes truth."""
    errors = []
    evidence = {"scope": "Actual tool/raw/date/unit/source checks; unmatched webpage prose requires explicit spoken uncertainty, never model-as-judge."}
    result = task.get("result") or {}
    sources = result.get("sources") or task.get("sources") or []
    actual = [e for e in traces if e.get("task_id") == task.get("id") and e.get("kind") == "tool.completed"
              and e.get("data", {}).get("result", {}).get("ok") is True]
    pages = [page for event in actual if event["data"].get("name") == "extract_web"
             for page in event["data"]["result"].get("results", [])]
    official = [source for source in sources if official_weather_url(source.get("url", ""))
                and source.get("extracted") is True and any(page.get("url") == source["url"]
                    and (page.get("raw_content") or page.get("content")) for page in pages)]
    evidence["actually_extracted_official_urls"] = [source["url"] for source in official]
    if not official:
        errors.append("official_weather_page_not_actually_read")
    if task.get("context", {}).get("read_details", False) is not False:
        errors.append("brief_task_context_enabled_detailed_reading")
    try:
        captured = datetime.fromisoformat(task["context"]["captured_at"].replace("Z", "+00:00"))
        if captured.utcoffset() is None:
            raise ValueError
        target = (captured.astimezone(ZoneInfo("Europe/Paris")).date() + timedelta(days=1)).isoformat()
    except (KeyError, ValueError, TypeError):
        target = None
        errors.append("target_day_not_grounded_in_task_capture")
    evidence["expected_date"] = target
    adopted = {source.get("url") for source in sources}
    forecasts = [event["data"]["result"] for event in actual if event["data"].get("name") == "get_weather_forecast"]
    valid_structured = []
    for forecast in forecasts:
        low, high = forecast.get("temperature_min"), forecast.get("temperature_max")
        location = forecast.get("location") or {}
        raw = (forecast.get("raw") or {}).get("forecast") or {}
        daily, units = raw.get("daily") or {}, raw.get("daily_units") or {}
        numbers = all(type(value) in (int, float) and math.isfinite(value) for value in (low, high))
        valid = (forecast.get("data_type") == "forecast" and forecast.get("date") == target
                 and forecast.get("timezone") == location.get("timezone") == raw.get("timezone") == "Europe/Paris"
                 and location.get("country_code") == "FR" and str(location.get("name", "")).casefold() in {"paris", "巴黎"}
                 and forecast.get("unit") == units.get("temperature_2m_min") == units.get("temperature_2m_max") == "°C"
                 and daily.get("time") == [target] and daily.get("temperature_2m_min") == [low]
                 and daily.get("temperature_2m_max") == [high] and numbers and low <= high)
        if not valid:
            errors.append("structured_forecast_disagrees_with_target_or_actual_raw")
            continue
        if forecast.get("source_url") in adopted:
            keys = ("date", "location", "timezone", "temperature_min", "temperature_max", "unit", "source_url", "queried_at")
            retained = {key: forecast.get(key) for key in keys}
            if retained not in result.get("forecast_records", []):
                errors.append("finish_did_not_preserve_actual_structured_forecast")
            source = next(source for source in sources if source.get("url") == forecast["source_url"])
            if source.get("kind") != "forecast" or source.get("verified") is not True or source.get("extracted") is not False:
                errors.append("structured_forecast_mislabeled_as_web_extract")
            valid_structured.append(retained)
    web_records = extracted_forecast_records(actual, sources, target, {"location_names": ["Paris", "巴黎"]})
    official_dated = [record for record in web_records if official_weather_url(record["source_url"])]
    evidence["adopted_structured_forecasts"] = valid_structured
    evidence["actual_dated_web_forecasts"] = web_records
    available = valid_structured + web_records
    for field in ("summary", "spoken_summary"):
        body = result.get(field, "")
        clauses = re.split(r"[，,。；;\n]", body)
        for clause in clauses:
            if re.search(r"open[- ]?meteo", clause, re.I) and re.search(r"官方|法国气象局", clause):
                if not re.search(r"并非|不是|非官方|不属于|未(?:用|使用)|没有使用", clause):
                    errors.append("open_meteo_falsely_called_official:" + field)
        facts = temperature_range_facts(body)
        roles = forecast_date_evidence(body, target)
        evidence[field + "_date_roles"] = roles
        evidence[field + "_temperature_facts"] = facts
        if facts:
            if not roles["pass"]:
                errors.append("weather_report_changed_or_omitted_target_date:" + field)
            if not extracted_forecast_report_matches(body, available):
                errors.append("weather_temperature_not_supported_by_actual_dated_source:" + field)
        elif re.search(r"-?\d+(?:\.\d+)?\s*度", normalized_text(body)):
            errors.append("weather_scalar_temperature_not_verified:" + field)
        elif available and not target_uncertainty(body):
            errors.append("weather_result_omitted_verified_temperature_and_limitation:" + field)
        # A real official page read is not proof of tomorrow's temperature.
        # If that page has no parsed dated facts, speech must preserve that limit.
        if not official_dated and not target_uncertainty(body):
            errors.append("official_target_day_unverified_but_limitation_missing:" + field)
        if not available and not target_uncertainty(body):
            errors.append("target_forecast_unverified_but_limitation_missing:" + field)
    evidence["validation_errors"] = sorted(set(errors))
    return evidence


def background_errors(task_ids, snapshot, traces):
    errors = []
    tasks = {task["id"]: task for task in snapshot.get("tasks", [])}
    for identity in task_ids:
        task = tasks.get(identity, {})
        if task.get("status") != "completed":
            errors.append("background_task_not_completed:" + identity)
            continue
        result = task.get("result") or {}
        sources = result.get("sources") or task.get("sources") or []
        official = [source for source in sources if official_weather_url(source.get("url", "")) and source.get("extracted") is True]
        if not official:
            errors.append("official_weather_source_not_extracted:" + identity)
        if not result.get("summary", result.get("answer")) or not result.get("spoken_summary"):
            errors.append("missing_full_or_spoken_background_result:" + identity)
        for tool in ("search_web", "extract_web"):
            if not any(event.get("task_id") == identity and event.get("kind") == "tool.completed"
                       and event.get("data", {}).get("name") == tool and event.get("data", {}).get("result", {}).get("ok") is True for event in traces):
                errors.append("missing_actual_" + tool + ":" + identity)
        errors.extend(error + ":" + identity for error in background_weather_evidence(task, traces)["validation_errors"])
    return errors


class PCMConsumer:
    """Protocol identity checks plus a *simulated* real-time drain, no audio device."""
    def __init__(self, harness):
        self.harness = harness
        self.records = {}
        self.current = None
        self.rejected = set()
        self.capturing = False
        self.jobs = set()
        self.dropped = []

    async def metadata(self, message):
        kind, identity = message.get("type"), message.get("playback_id")
        if kind == "audio.started":
            if (message.get("session_id") != self.harness.sid or not re.fullmatch(r"[a-fA-F0-9]{32}", identity or "")
                    or message.get("sample_rate") != 24000 or message.get("channels") != 1 or message.get("format") != "pcm_s16le"):
                self.harness.errors.append("invalid_audio_identity_or_format")
                return
            if identity in self.records:
                self.harness.errors.append("duplicate_audio_started:" + identity)
                return
            record = {**message, "started_mono_ms": self.harness.now(), "packets": [], "pcm_bytes": 0,
                      "accepted_pcm_bytes": 0, "status": "pending", "scheduled_until_mono_ms": self.harness.now()}
            self.records[identity] = record
            if self.capturing or identity in self.rejected:
                record["status"] = "rejected_start"
                self.rejected.add(identity)
                return
            if self.current:
                await self.cancel("replaced")
            self.current = identity
            await self.harness.send({"type": "playback", "speaking": True, "turn_id": message["turn_id"], "playback_id": identity})
        elif identity in self.records:
            record = self.records[identity]
            if kind == "audio.ended":
                record["input_ended_mono_ms"] = self.harness.now()
                if identity not in self.rejected:
                    job = asyncio.create_task(self.drain(identity))
                    self.jobs.add(job)
                    job.add_done_callback(self.jobs.discard)
            elif kind in {"audio.cancelled", "audio.error"}:
                record["server_terminal"] = message
                if identity not in self.rejected:
                    await self.cancel(kind)

    def pcm(self, packet):
        now = self.harness.now()
        if len(packet) <= 32 or (len(packet) - 32) % 2:
            self.harness.errors.append("invalid_pcm_packet")
            return
        identity = packet[:32].decode("ascii", errors="replace")
        record = self.records.get(identity)
        accepted = bool(record and self.current == identity and identity not in self.rejected and not self.capturing and "input_ended_mono_ms" not in record)
        detail = {"received_mono_ms": now, "bytes": len(packet) - 32, "accepted": accepted,
                  "sha256": hashlib.sha256(packet[32:]).hexdigest()}
        if not record:
            self.dropped.append({"playback_id": identity, **detail})
            self.harness.errors.append("pcm_without_audio_started")
            return
        record["packets"].append(detail)
        record["pcm_bytes"] += len(packet) - 32
        if not accepted:
            self.dropped.append({"playback_id": identity, **detail})
            return
        record.setdefault("first_pcm_mono_ms", now)
        if any(sample != 0 for (sample,) in struct.iter_unpack("<h", packet[32:])):
            record.setdefault("first_nonzero_pcm_mono_ms", now)
        record["accepted_pcm_bytes"] += len(packet) - 32
        record["scheduled_until_mono_ms"] = max(now, record["scheduled_until_mono_ms"]) + (len(packet) - 32) / 48

    async def drain(self, identity):
        record = self.records[identity]
        await asyncio.sleep(max(0, (record["scheduled_until_mono_ms"] - self.harness.now()) / 1000))
        if self.current == identity and identity not in self.rejected:
            record["simulated_drain_mono_ms"] = self.harness.now()
            record["status"] = "ended"
            self.current = None
            await self.harness.send({"type": "playback", "speaking": False, "turn_id": record["turn_id"], "playback_id": identity})
            self.harness.changed.set()

    async def cancel(self, reason):
        identity = self.current
        if not identity:
            return
        self.rejected.add(identity)
        self.current = None
        record = self.records[identity]
        record.update(status="cancelled", cancel_reason=reason, cancelled_mono_ms=self.harness.now())
        await self.harness.send({"type": "audio.cancel", "turn_id": record["turn_id"], "playback_id": identity})
        await self.harness.send({"type": "playback", "speaking": False, "turn_id": record["turn_id"], "playback_id": identity})


class Harness:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.wsbase = self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        self.http = httpx.AsyncClient(base_url=self.base, timeout=20)
        self.started = time.perf_counter()
        self.sid = None
        self.events, self.errors = [], []
        self.changed = asyncio.Event()
        self.consumer = PCMConsumer(self)
        self.jobs = []
        self.control = self.audio = None

    def now(self):
        return (time.perf_counter() - self.started) * 1000

    async def send(self, message):
        await self.control.send(json.dumps(message, ensure_ascii=False))

    async def connect(self):
        response = await self.http.post("/api/sessions")
        response.raise_for_status()
        self.sid = response.json()["session_id"]
        self.control = await websockets.connect(f"{self.wsbase}/ws/{self.sid}")
        self.audio = await websockets.connect(f"{self.wsbase}/ws/{self.sid}/audio")
        self.jobs = [asyncio.create_task(self.read_control()), asyncio.create_task(self.read_audio())]
        await self.wait(lambda: any(e.get("type") == "session.snapshot" for e in self.events), 10)
        await self.send({"type": "audio.configure", "enabled": True})

    async def read_control(self):
        try:
            async for raw in self.control:
                message = json.loads(raw)
                self.events.append({**message, "received_mono_ms": self.now(), "transport": "control"})
                if message.get("type") == "vehicle.command":
                    self.jobs.append(asyncio.create_task(execute_vehicle(self.http,self.base,self.sid,message,self.control)))
                self.changed.set()
        except Exception as error:
            self.errors.append("control_socket:" + type(error).__name__)
            self.changed.set()

    async def read_audio(self):
        try:
            async for raw in self.audio:
                if isinstance(raw, bytes):
                    self.consumer.pcm(raw)
                else:
                    message = json.loads(raw)
                    self.events.append({**message, "received_mono_ms": self.now(), "transport": "audio"})
                    await self.consumer.metadata(message)
                self.changed.set()
        except Exception as error:
            self.errors.append("audio_socket:" + type(error).__name__)
            self.changed.set()

    async def wait(self, predicate, timeout=60):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                value = predicate()
                if value:
                    return value
                if self.errors:
                    raise RuntimeError(self.errors[-1])
                await self.changed.wait()

    async def snapshot(self):
        response = await self.http.get(f"/api/sessions/{self.sid}")
        response.raise_for_status()
        return response.json()

    async def traces(self):
        events, cursor = [], 0
        while True:
            response = await self.http.get(f"/api/sessions/{self.sid}/traces", params={"after_id": cursor})
            response.raise_for_status()
            body = response.json()
            if body.get("recording_error"):
                self.errors.append("trace_recording_error:" + body["recording_error"])
            events.extend(body["events"])
            cursor = body["next_after_id"]
            if not body["has_more"]:
                return events

    async def recognize(self, pcm, record):
        capture_id = uuid4().hex
        record.update(capture_id=capture_id, asr_events=[], pcm_bytes=len(pcm), pcm_duration_ms=len(pcm) / 32)
        self.consumer.capturing = True
        await self.send({"type": "asr.capture", "capture_id": capture_id, "active": True})
        await self.consumer.cancel("asr_capture")
        await self.send({"type": "audio.configure", "enabled": False})
        stopped = False
        async with websockets.connect(f"{self.wsbase}/ws/{self.sid}/asr") as socket:
            await socket.send(json.dumps({"type": "asr.start", "capture_id": capture_id, "format": "pcm_s16le", "sample_rate": 16000, "channels": 1}))
            ready = asyncio.Event()

            async def receive():
                async for raw in socket:
                    message = json.loads(raw)
                    record["asr_events"].append({**message, "received_mono_ms": self.now()})
                    if message.get("capture_id") != capture_id:
                        raise RuntimeError("asr_capture_id_mismatch")
                    if message.get("type") == "asr.ready":
                        ready.set()
                    if message.get("type") == "asr.error":
                        ready.set()
                        raise RuntimeError(message.get("message", "asr_error"))
                    if message.get("type") == "asr.final" and not stopped:
                        raise RuntimeError("asr_final_before_explicit_stop")

            receiver = asyncio.create_task(receive())
            try:
                await asyncio.wait_for(ready.wait(), 15)
                if receiver.done():
                    await receiver
                    raise RuntimeError("asr_closed_before_audio")
                record["first_pcm_sent_mono_ms"] = self.now()
                for offset in range(0, len(pcm), 3200):
                    chunk = pcm[offset:offset + 3200]
                    await socket.send(chunk)
                    await asyncio.sleep(len(chunk) / 32000)
                    if receiver.done():
                        await receiver
                        raise RuntimeError("asr_closed_before_stop")
                stopped = True
                record["stop_sent_mono_ms"] = self.now()
                await socket.send(json.dumps({"type": "asr.stop", "capture_id": capture_id}))
                await asyncio.wait_for(receiver, 20)
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        finals = [e for e in record["asr_events"] if e.get("type") == "asr.final"]
        if len(finals) != 1 or not finals[0].get("text", "").strip():
            raise RuntimeError("expected_one_nonempty_asr_final")
        record["final_text"] = finals[0]["text"].strip()
        record["stop_to_final_received_ms"] = finals[0]["received_mono_ms"] - record["stop_sent_mono_ms"]
        return record["final_text"]

    async def query(self, text, attempt, scenario):
        request_id = uuid4().hex
        attempt.update(client_request_id=request_id, submit_mono_ms=self.now())
        # Same control socket preserves configure -> input -> capture-release order.
        await self.send({"type": "audio.configure", "enabled": True})
        self.consumer.capturing = False
        await self.send({"type": "input", "text": text, "client_request_id": request_id})
        await self.send({"type": "asr.capture", "capture_id": attempt["asr"]["capture_id"], "active": False})
        started = await self.wait(lambda: next((e for e in self.events if e.get("type") == "turn.started" and e.get("client_request_id") == request_id), None))
        turn = attempt["turn_id"] = started["turn_id"]
        done = await self.wait(lambda: next((e for e in self.events if e.get("type") == "stream.done" and e.get("turn_id") == turn), None))
        attempt["output"], attempt["turn_metrics"] = done.get("text", ""), done.get("metrics", {})
        audio = await self.wait(lambda: next((r for r in self.consumer.records.values() if r.get("turn_id") == turn and r.get("first_nonzero_pcm_mono_ms") is not None), None))
        attempt["playback_id"] = audio["playback_id"]
        attempt["query_to_first_nonzero_pcm_received_ms"] = audio["first_nonzero_pcm_mono_ms"] - attempt["submit_mono_ms"]
        if scenario.get("cancel_after_first_pcm"):
            await self.consumer.cancel("validation_user_stop")
            await self.wait(lambda: audio.get("server_terminal"), 10)
            attempt["intentional_audio_cancel"] = True
        elif scenario.get("wait_drain", True):
            await self.wait(lambda: audio["status"] in {"ended", "cancelled", "error"}, 90)
        relevant = [e for e in self.events if e.get("turn_id") == turn]
        attempt["commands"] = [{k: e[k] for k in ("command_id", "name", "arguments")} for e in relevant if e.get("type") == "vehicle.command"]
        attempt["receipts"] = [e for e in relevant if e.get("type") == "vehicle.result"]
        attempt["registered_task_ids"] = sorted({e["task"]["id"] for e in self.events if e.get("type") in {"ui.card", "task.event"}
                                                and (e.get("origin_turn_id") == turn or e.get("task", {}).get("context", {}).get("origin_turn_id") == turn)})
        registered = [e["received_mono_ms"] for e in self.events if e.get("type") in {"ui.card", "task.event"}
                      and e.get("task", {}).get("id") in attempt["registered_task_ids"]]
        if registered:
            attempt["task_registered_mono_ms"] = min(registered)

    async def close(self):
        try:
            await self.consumer.cancel("evaluation_finished")
        except Exception as error:
            self.errors.append("cleanup_control:" + type(error).__name__)
        for job in self.jobs + list(self.consumer.jobs):
            job.cancel()
        await asyncio.gather(*self.jobs, *self.consumer.jobs, return_exceptions=True)
        if self.audio:
            await self.audio.close()
        if self.control:
            await self.control.close()
        await self.http.aclose()


async def fixture(text, directory):
    key = hashlib.sha256(text.encode()).hexdigest()
    path = directory / (key + ".pcm")
    manifest = path.with_suffix(".json")
    if path.exists() and manifest.exists():
        metadata = json.loads(manifest.read_text())
        pcm = path.read_bytes()
        if metadata.get("text") != text or metadata.get("sha256") != hashlib.sha256(pcm).hexdigest():
            raise RuntimeError("fixture_manifest_mismatch")
        return pcm, {**metadata, "source": "reused_real_xiaohe_synthesis"}
    # Import server adapters only for an explicitly requested live fixture creation.
    from backend import settings  # noqa: F401
    from backend.tts import VolcanoTTS
    tts = VolcanoTTS()
    async def texts():
        yield text
    chunks, diagnostics = [], []
    try:
        async for item in tts.stream_synthesize(texts()):
            if item.get("type") == "audio":
                chunks.append(item["audio"])
            elif item.get("type") == "metrics":
                diagnostics.append(item.get("metrics", {}))
    finally:
        await tts.aclose()
    source = b"".join(chunks)
    if not source or not any(value != 0 for (value,) in struct.iter_unpack("<h", source)):
        raise RuntimeError("fixture_synthesis_no_nonzero_pcm")
    # Explicit 200ms leading/trailing silence is input-fixture padding, never reply latency.
    pcm = bytes(6400) + resample_pcm(source) + bytes(6400)
    metadata = {"text": text, "format": "pcm_s16le", "sample_rate": 16000, "channels": 1,
                "sha256": hashlib.sha256(pcm).hexdigest(), "created_at": utcnow(), "input_padding_ms": 400,
                "provider": "volcengine-speech-tts", "voice": "xiaohe2.0", "diagnostics": diagnostics}
    directory.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm)
    manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return pcm, {**metadata, "source": "new_real_xiaohe_synthesis"}


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


async def run(args):
    output = Path(args.output)
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing evidence: {output}")
    report = {"kind": "real_asr_query_pcm_interactions", "oracle_version": ORACLE_VERSION, "started_at": utcnow(), "base": args.base,
              "measurement_boundary": "Python monotonic WS receipt; synthetic Xiaohe inputs paced in real time; no browser, microphone, AudioContext, Worklet or acoustic render measured. Playback drain is simulated from accepted sample counts, not observed audio output.",
              "query_pcm_target_ms": 1500, "evaluation_load_note": args.load_note,
              "attempts": [], "transport_errors": [], "traces": [],
              "physical_microphone_tested": False, "browser_audio_tested": False}
    harness = Harness(args.base)
    try:
        response = await harness.http.get("/api/config")
        response.raise_for_status()
        report["service_config"] = response.json()
        if args.expected_talker and report["service_config"].get("model", {}).get("talker") != args.expected_talker:
            raise RuntimeError("configured_talker_does_not_match_expected_model")
        await harness.connect()
        report["session_id"] = harness.sid
        write_report(output, report)
        for scenario in SCENARIOS:
            attempt = {"scenario_id": scenario["id"], "fixture_text": scenario["text"], "expected": scenario,
                       "started_at": utcnow(), "asr": {}, "status": "pending"}
            report["attempts"].append(attempt)
            try:
                pcm, attempt["fixture"] = await fixture(scenario["text"], Path(args.fixture_dir))
                snapshot = await harness.snapshot()
                attempt["before"] = snapshot["vehicle"]
                attempt["background_tasks_at_capture"] = [{k: t.get(k) for k in ("id", "status", "revision")} for t in snapshot["tasks"]]
                text = await harness.recognize(pcm, attempt["asr"])
                attempt["asr_text_matches_fixture"] = normalize_transcript(text) == normalize_transcript(scenario["text"])
                await harness.query(text, attempt, scenario)
                attempt["after"] = (await harness.snapshot())["vehicle"]
                attempt["status"] = "observed"
            except Exception as error:
                attempt.update(status="error", error=str(error), error_type=type(error).__name__)
                harness.consumer.capturing = False
                if attempt["asr"].get("capture_id"):
                    await harness.send({"type": "asr.capture", "capture_id": attempt["asr"]["capture_id"], "active": False})
            write_report(output, report)
            print(json.dumps({k: attempt.get(k) for k in ("scenario_id", "turn_id", "status", "query_to_first_nonzero_pcm_received_ms", "error")}, ensure_ascii=False), flush=True)
        task_ids = [task for attempt in report["attempts"] for task in attempt.get("registered_task_ids", [])]
        if task_ids:
            try:
                await harness.wait(lambda: next((e for e in reversed(harness.events) if e.get("type") == "task.event"
                                   and e.get("task", {}).get("id") in task_ids and e.get("task", {}).get("status") in {"completed", "failed", "waiting_clarification", "cancelled"}), None), 180)
                await harness.wait(lambda: any(e.get("type") == "turn.started" and e.get("proactive") and e.get("task_id") in task_ids for e in harness.events), 100)
                await harness.wait(lambda: any(r.get("turn_id") in {e.get("turn_id") for e in harness.events if e.get("proactive") and e.get("task_id") in task_ids}
                                   and r.get("status") == "ended" for r in harness.consumer.records.values()), 100)
            except Exception as error:
                report["background_result_wait_error"] = type(error).__name__ + ":" + str(error)
        # Allow the asynchronous trace writer to catch up once; no repeated green runs.
        await asyncio.sleep(.25)
        report["traces"] = await harness.traces()
        report["final_snapshot"] = await harness.snapshot()
        report["background_validation_errors"] = background_errors(task_ids, report["final_snapshot"], report["traces"])
        report["background_validation_evidence"] = {task["id"]: background_weather_evidence(task, report["traces"])
                                                   for task in report["final_snapshot"].get("tasks", []) if task["id"] in task_ids}
    except Exception as error:
        report["fatal_error"] = type(error).__name__ + ":" + str(error)
    finally:
        await harness.close()
        report["events"] = harness.events
        report["audio_records"] = list(harness.consumer.records.values())
        report["dropped_pcm_packets"] = harness.consumer.dropped
        report["transport_errors"] = harness.errors
        for scenario, attempt in zip(SCENARIOS, report["attempts"]):
            issues = semantic_errors(scenario, attempt, report["traces"]) if attempt["status"] == "observed" else ["attempt_not_completed"]
            if not attempt.get("asr_text_matches_fixture"):
                issues.append("asr_transcript_changed_fixture_content")
            record = harness.consumer.records.get(attempt.get("playback_id"), {})
            issues.extend(transport_errors(scenario, attempt, record))
            attempt["validation_errors"] = sorted(set(issues))
            attempt["semantic_and_transport_pass"] = not issues
        report["finished_at"] = utcnow()
        report["summary"] = {"attempts": len(report["attempts"]), "semantic_and_transport_passed": sum(a["semantic_and_transport_pass"] for a in report["attempts"]),
                             "within_1500ms_python_receipt": sum(a.get("query_to_first_nonzero_pcm_received_ms", float("inf")) <= 1500 for a in report["attempts"]),
                             "latency_is_browser_measurement": False}
        write_report(output, report)
    print(json.dumps({"output": str(output), "session_id": harness.sid, **report["summary"]}, ensure_ascii=False), flush=True)
    return 0 if (report["summary"]["semantic_and_transport_passed"] == len(SCENARIOS) and not harness.errors
                 and not report.get("fatal_error") and not report.get("background_validation_errors") and not report.get("background_result_wait_error")) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="docs/voice-interactions-live.json")
    parser.add_argument("--fixture-dir", default="data/voice-interaction-fixtures")
    parser.add_argument("--expected-talker", help="Fail before provider requests if /api/config advertises another Talker")
    parser.add_argument("--load-note", default=None, help="Record concurrent acceptance traffic; never label these samples unloaded")
    parser.add_argument("--run", action="store_true", help="Explicitly make real ASR, model and TTS requests")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"mode": "plan_only_no_requests", "scenarios": SCENARIOS,
                          "note": "7 voice queries / 6 interaction classes; chat is intentionally stopped after first real nonzero PCM. Independent expectations are fixed before live execution."}, ensure_ascii=False, indent=2))
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
