#!/usr/bin/env python3
"""Real Ark + websocket persona invariance regression against a running service.

Each query/persona pair receives a new session (empty history and default car).
Vehicle commands are acknowledged over the public browser protocol. No model,
retrieval, or vehicle outcome is replaced by an evaluation stub.
"""
from __future__ import annotations

if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import websockets
from scripts.vehicle_device import execute_vehicle

PERSONAS = ["professional", "warm", "playful"]
CASES = [
    {"id": "temperature", "query": "把空调温度设为23度", "expected_tool": "set_temperature", "expected_arguments": {"zone": "all", "temperature": 23}, "route": "tool"},
    {"id": "left_front_window", "query": "把左前车窗打开到50%", "expected_tool": "set_window_position", "expected_arguments": {"window": "driver", "position": 50}, "route": "tool"},
    {"id": "mirror_heating", "query": "打开后视镜加热", "expected_tool": "set_mirror_heating", "expected_arguments": {"enabled": True}, "route": "tool", "load_skill": "mirrors"},
    {"id": "chat", "query": "我今天终于把一件拖了很久的小事做完了。陪我聊一句吧。", "route": "chat"},
    {"id": "departure_greeting", "query": "你好，我准备出发了", "route": "chat", "require_language_difference": False},
    {"id": "greeting_after_temperature", "query": "你好，我准备出发了", "route": "chat", "prime_query": "把空调温度设为23度", "require_language_difference": False},
]


def normalize_numbers(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_numbers(item) for item in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(normalize_numbers(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tool_calls(history):
    calls = []
    for message in history:
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            raw = function.get("arguments", "{}")
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                arguments = {"invalid_arguments_json": True}
            calls.append({"name": function.get("name"), "arguments": arguments})
    return calls


def websocket_url(base_url, session_id):
    parsed = urlsplit(base_url)
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, f"{parsed.path.rstrip('/')}/ws/{session_id}", "", ""))


def semantic_history(history):
    """Discard random function call IDs; preserve all actual messages and results."""
    messages = []
    for message in history:
        clean = {"role": message.get("role"), "content": message.get("content")}
        if message.get("role") == "tool":
            try:
                clean["content"] = json.loads(message.get("content", "{}"))
            except (TypeError, ValueError):
                pass
        if message.get("tool_calls"):
            clean["tool_calls"] = tool_calls([message])
        messages.append(normalize_numbers(clean))
    return messages


async def prime_history(ws, query, timeout, client, base_url, session_id, device_jobs):
    """Execute a real common control with the same default persona before switching."""
    await ws.send(json.dumps({"type": "input", "text": query, "client_request_id": uuid4().hex}, ensure_ascii=False))
    deadline = time.perf_counter() + timeout
    priming = {"query": query, "commands": [], "receipts": [], "ok": False}
    turn_id = None
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("priming turn exceeded evaluation deadline")
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        kind = frame.get("type")
        if kind == "turn.started":
            turn_id = frame["turn_id"]
        elif kind == "vehicle.command":
            device_jobs.append(asyncio.create_task(execute_vehicle(client,base_url,session_id,frame,ws)))
            priming["commands"].append({"name": frame["name"], "arguments": frame["arguments"]})
        elif kind == "vehicle.result":
            result = frame["result"]
            priming["receipts"].append({"name": result.get("name"), "arguments": result.get("arguments"), "ok": result.get("ok"), "value": result.get("value")})
        elif kind == "error":
            raise RuntimeError(frame.get("message", "priming server error"))
        elif kind == "stream.done" and (turn_id is None or frame.get("turn_id") == turn_id):
            priming["text"] = frame.get("text")
            priming["metrics"] = frame.get("metrics", {})
            expected = [{"name": "set_temperature", "arguments": {"zone": "all", "temperature": 23}}]
            priming["ok"] = priming["commands"] == expected and all(receipt["ok"] for receipt in priming["receipts"]) and not priming["metrics"].get("error")
            return priming


async def evaluate_one(client, base_url, persona, case, timeout):
    record = {"persona": persona, "case": case["id"], "query": case["query"], "ok": False,
              "commands": [], "receipts": [], "errors": [], "tool_calls": []}
    started = None
    session_id = None
    device_jobs = []
    try:
        response = await client.post(f"{base_url}/api/sessions")
        response.raise_for_status()
        session_id = response.json()["session_id"]
        record["session_id"] = session_id
        async with websockets.connect(websocket_url(base_url, session_id), open_timeout=10, max_size=4 * 1024 * 1024) as ws:
            initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if initial.get("type") != "session.snapshot":
                raise RuntimeError("initial websocket frame was not a session snapshot")
            record["initial_history_count"] = len(initial.get("history", []))
            record["initial_task_count"] = len(initial.get("tasks", []))
            record["initial_state_sha256"] = fingerprint(initial.get("vehicle", {}))
            query_snapshot = initial
            if case.get("prime_query"):
                record["priming"] = await prime_history(ws, case["prime_query"], timeout, client, base_url, session_id, device_jobs)
                response = await client.get(f"{base_url}/api/sessions/{session_id}")
                response.raise_for_status()
                query_snapshot = response.json()
            record["query_initial_state_sha256"] = fingerprint(query_snapshot.get("vehicle", {}))
            record["query_initial_history_count"] = len(query_snapshot.get("history", []))
            record["query_initial_history_sha256"] = fingerprint(semantic_history(query_snapshot.get("history", [])))
            await ws.send(json.dumps({"type": "persona.set", "preset": persona}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if frame.get("type") == "persona.changed":
                    record["persona_version"] = frame.get("persona_version")
                    record["persona_name"] = frame.get("persona", {}).get("name")
                    break
                if frame.get("type") == "error":
                    raise RuntimeError(frame.get("message", "persona switch failed"))
            started = time.perf_counter()
            await ws.send(json.dumps({"type": "input", "text": case["query"], "client_request_id": uuid4().hex}, ensure_ascii=False))
            deadline = started + timeout
            turn_id = None
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("turn exceeded evaluation deadline")
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                kind = frame.get("type")
                elapsed = (time.perf_counter() - started) * 1000
                if kind == "turn.started":
                    turn_id = frame["turn_id"]
                    record["turn_id"] = turn_id
                elif kind == "vehicle.command":
                    # The exact public browser acknowledgement triggers the server edge simulator.
                    device_jobs.append(asyncio.create_task(execute_vehicle(client,base_url,session_id,frame,ws)))
                    record["commands"].append({"name": frame["name"], "arguments": frame["arguments"], "received_ms": round(elapsed, 2)})
                elif kind == "vehicle.result":
                    result = frame["result"]
                    record["receipts"].append({"name": result.get("name"), "arguments": result.get("arguments"),
                                               "ok": result.get("ok"), "value": result.get("value"), "reason": result.get("reason"),
                                               "client_received_ms": round(elapsed, 2), "server_latency_ms": frame.get("latency_ms"),
                                               "state_sha256": fingerprint(result.get("state", {}))})
                elif kind == "error":
                    record["errors"].append(frame.get("message", "server error"))
                elif kind == "stream.done" and (turn_id is None or frame.get("turn_id") == turn_id):
                    record["text"] = frame.get("text", "")
                    record["metrics"] = frame.get("metrics", {})
                    record["client_total_ms"] = round(elapsed, 2)
                    record["route"] = record["metrics"].get("route")
                    if record["metrics"].get("error"):
                        record["errors"].append(record["metrics"]["error"])
                    break
        response = await client.get(f"{base_url}/api/sessions/{session_id}")
        response.raise_for_status()
        snapshot = response.json()
        all_history = snapshot.get("history", [])
        record["tool_calls"] = tool_calls(all_history[record["query_initial_history_count"]:])
        record["final_state_sha256"] = fingerprint(snapshot.get("vehicle", {}))
        record["initial_conditions_ok"] = record["initial_history_count"] == 0 and record["initial_task_count"] == 0 and record.get("priming", {"ok": True})["ok"]
        if "expected_tool" in case:
            expected = {"name": case["expected_tool"], "arguments": case["expected_arguments"]}
            actual = [{"name": command["name"], "arguments": command["arguments"]} for command in record["commands"]]
            record["expected_action_ok"] = actual == [expected]
            record["receipts_ok"] = bool(record["receipts"]) and all(receipt["ok"] is True for receipt in record["receipts"])
        else:
            record["expected_action_ok"] = not record["commands"] and not record["tool_calls"]
            record["receipts_ok"] = not record["receipts"]
        if case.get("load_skill"):
            expected_load = {"name": "load_skill", "arguments": {"skill_id": case["load_skill"]}}
            calls = record["tool_calls"]
            record["load_skill_before_action"] = expected_load in calls and calls.index(expected_load) < next((i for i, item in enumerate(calls) if item["name"] == case["expected_tool"]), -1)
        record["ok"] = all([not record["errors"], record["initial_conditions_ok"], record["expected_action_ok"],
                             record["receipts_ok"], record.get("load_skill_before_action", True), record["route"] == case["route"], bool(record.get("text", "").strip())])
    except Exception as exc:
        # These are local HTTP/WS errors; do not inspect credentials or model request headers.
        record["errors"].append(f"{type(exc).__name__}: {str(exc)[:240]}")
        if started is not None:
            record["client_total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    outcomes = await asyncio.gather(*device_jobs, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            record["errors"].append("device:" + type(outcome).__name__)
            record["ok"] = False
    return record


def comparisons(records):
    result = []
    for case in CASES:
        rows = [row for row in records if row["case"] == case["id"]]
        signatures = [{"route": row.get("route"), "commands": [{"name": c["name"], "arguments": c["arguments"]} for c in row["commands"]], "tool_calls": row["tool_calls"]} for row in rows]
        actions_invariant = len(rows) == len(PERSONAS) and all(signature == signatures[0] for signature in signatures)
        same_state = len({row.get("query_initial_state_sha256") for row in rows}) == 1
        same_history = len({row.get("query_initial_history_sha256") for row in rows}) == 1
        texts = {row["persona"]: row.get("text", "") for row in rows}
        language_differs = len(set(texts.values())) == len(PERSONAS) and all(texts.values())
        result.append({"case": case["id"], "query": case["query"], "all_runs_ok": len(rows) == len(PERSONAS) and all(row["ok"] for row in rows),
                       "same_initial_state": same_state, "same_initial_history": same_history,
                       "empty_query_history": all(row.get("query_initial_history_count") == 0 for row in rows),
                       "uses_real_executed_history": bool(case.get("prime_query")),
                       "action_and_route_invariant": actions_invariant, "three_distinct_expressions": language_differs, "language_difference_required": case.get("require_language_difference", True),
                       "expressions": texts,
                       "pass": len(rows) == len(PERSONAS) and all(row["ok"] for row in rows) and same_state and same_history and actions_invariant and (language_differs or not case.get("require_language_difference", True))})
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", default="docs/persona-live.json")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    report = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "base_url": base_url,
              "real_model": True, "vehicle": "mock edge simulator driven through websocket acknowledgement", "personas": PERSONAS,
              "method": "Every query/persona pair starts a fresh session. The explicit history case executes the same real 23C control in the default warm persona before switching. Random function call IDs and equivalent JSON numeric representations are removed for history comparison. No model or outcome substitution.",
              "records": []}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.get(f"{base_url}/api/config")
            response.raise_for_status()
            report["models"] = response.json().get("model", {})
        except Exception as exc:
            report["setup_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        if "setup_error" not in report:
            for case in CASES:
                for persona in PERSONAS:
                    record = await evaluate_one(client, base_url, persona, case, args.timeout)
                    report["records"].append(record)
                    print(f"{case['id']} / {persona}: {'PASS' if record['ok'] else 'FAIL'}; {record.get('client_total_ms')} ms", flush=True)
    report["comparisons"] = comparisons(report["records"])
    report["pass"] = all(comparison["pass"] for comparison in report["comparisons"])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Report: {output.resolve()} ; overall={'PASS' if report['pass'] else 'FAIL'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
