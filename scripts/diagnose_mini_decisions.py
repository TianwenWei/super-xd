"""Six bounded Mini-only replays of two failed decisions; never executes tools."""
if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from backend.models import ArkClient, sanitized_error, utcnow


MINI = "doubao-seed-2-0-mini-260428"
RULE = ("提交工具前只核对当前原句的两个集合：A是用户明确要求执行或设置的项目，H是要求暂缓、保持或不操作的项目。"
        "tool_calls必须且只能完整覆盖A，不能因参数是false就漏掉明确关闭动作，也不能为满足H额外生成false设置。"
        "关闭某功能是动作；先不要打开某功能是保持原状，不是要求关闭。历史操作不属于本轮A。不公开清单，直接调用所需工具。")
VARIANTS = ["repeat_current_user", "brief_action_content", "replace_final_check"]


def calls(message):
    result = []
    for call in message.get("tool_calls", []):
        fn = call.get("function", {})
        args = fn.get("arguments", "{}")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            args = {"invalid_raw_arguments": args}
        result.append({"name": fn.get("name"), "arguments": args})
    return result


def equivalent(actual, expected):
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return Counter(map(encode, actual)) == Counter(map(encode, expected))


def source_cases(payload):
    cases = []
    for group in payload["groups"]:
        for record in group["records"]:
            if record.get("pass") is not False or record["step_id"] not in {"ambient_color_brightness", "lock_and_child_off"}:
                continue
            if any(case["case_id"] == record["step_id"] for case in cases):
                continue
            traces = group["complete_traces"]
            started = [event for event in traces if event["kind"] == "model.started" and event.get("turn_id") == record["turn_id"]]
            original = started[-1]
            completed = next(event for event in traces if event["kind"] == "model.completed" and event.get("call_id") == original["call_id"])
            cases.append({"case_id": record["step_id"], "query": record["query"], "expected": record["expectation"]["actions"],
                          "source_trace_id": original["trace_id"], "source_call_id": original["call_id"],
                          "original_messages": original["data"]["messages"], "original_tools": original["data"]["tools"],
                          "baseline_response": completed["data"]})
    if len(cases) != 2:
        raise RuntimeError("Expected exactly two original failed decision contexts")
    return cases


def messages_for(case, variant):
    messages = deepcopy(case["original_messages"])
    if variant == "repeat_current_user":
        messages.append({"role": "user", "content": "本轮原句再次确认：" + case["query"] + "\n请完整覆盖每一个明确动作，包括明确关闭。不要、先不要、别动等保持项不生成设置调用，不以false替代保持。只处理这条原句。"})
    elif variant == "brief_action_content":
        messages.append({"role": "system", "content": "本次回复先在content用一句简短文字列出‘执行：…；保持：…’，再在同一回复的tool_calls中完整列出全部执行项。明确关闭也是执行；先不要打开是保持，不生成关闭调用。不得把计划说成已完成，不执行历史动作。"})
    elif variant == "replace_final_check":
        if messages[-1].get("role") != "system":
            raise RuntimeError("Original trailing check was not a system message")
        messages[-1] = {"role": "system", "content": RULE}
    else:
        raise ValueError("Unknown variant")
    return messages


async def main(args):
    source = Path(args.input)
    destination = Path(args.output)
    if destination.exists():
        raise RuntimeError("Refusing to overwrite diagnostic evidence")
    payload = json.loads(source.read_text())
    cases = source_cases(payload)
    report = {"kind": "bounded_mini_decision_replay", "created_at": utcnow(), "input": str(source),
              "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "model": MINI,
              "thinking": "disabled", "temperature": 0, "tool_execution": False, "sessions_created": 0,
              "baseline_repeated": False, "maximum_requests": 6, "cases": cases, "attempts": [],
              "scope": "Raw original decision contexts, diagnostic prompt variants, exact original command oracle; no browser/audio/acoustic test."}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    ark = ArkClient()
    try:
        for variant in VARIANTS:
            for case in cases:
                messages = messages_for(case, variant)
                request = ark.payload(messages, case["original_tools"], thinker=False, stream=True)
                if request["model"] != MINI or request["thinking"] != {"type": "disabled"} or request["temperature"] != 0:
                    raise RuntimeError("Refusing non-Mini or thinking-enabled diagnostic request")
                attempt = {"case_id": case["case_id"], "variant": variant, "source_trace_id": case["source_trace_id"],
                           "request": request, "started_at": utcnow(), "expected": case["expected"], "pass": False}
                report["attempts"].append(attempt)
                started = time.perf_counter()
                try:
                    async for item in ark.stream(messages, case["original_tools"]):
                        if item["type"] == "message":
                            attempt["response"] = item
                    attempt["actual"] = calls(attempt.get("response", {}).get("message", {}))
                    attempt["pass"] = equivalent(attempt["actual"], case["expected"])
                except Exception as error:
                    attempt["error"] = sanitized_error(error)
                    if getattr(error, "trace_partial", None):
                        attempt["partial"] = error.trace_partial
                attempt["elapsed_ms"] = (time.perf_counter() - started) * 1000
                save()
                print(json.dumps({key: attempt.get(key) for key in ("variant", "case_id", "pass", "actual", "elapsed_ms", "error")}, ensure_ascii=False), flush=True)
    finally:
        await ark.aclose()
        report["finished_at"] = utcnow()
        report["summary"] = {variant: {"attempts": sum(item["variant"] == variant for item in report["attempts"]),
                                        "passed": sum(item["variant"] == variant and item["pass"] for item in report["attempts"])} for variant in VARIANTS}
        save()
    print(json.dumps({"output": str(destination), "summary": report["summary"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="docs/interactions-mini-targeted-run3.json")
    parser.add_argument("--output", default="docs/mini-decision-diagnostic-20261013.json")
    asyncio.run(main(parser.parse_args()))
