"""Independent, bounded Thinker ReAct process.

The worker owns task execution, never the vehicle or conversation actor. SQLite
transactions finish before every external await. Fake clients are accepted only
through explicit dependency injection, for deterministic offline harness tests.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import logging
import math
import os
import signal
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from jsonschema import Draft202012Validator

from .catalog import SKILLS
from .prompt_templates import render_prompt
from .prompt_config import config_text, validate_config
from .speech_text import brief_result, spoken_number
from .textutil import find_urls, is_domain, redact
from .task_feedback import PHASES, partial_result, task_user_detail
from .speaker import speaker_context

logger = logging.getLogger(__name__)
SKILL_GUIDE = "；".join(f"{skill_id}：{SKILLS[skill_id].name}" for skill_id in sorted(SKILLS))


validate_config("speech/thinker.toml", {
    "cancelled": set(),
    "quick_budget_exhausted": set(),
    "lookup_no_sources": set(),
    "processing": set(),
    "suggestion_saved": set(),
    "source_heading": set(),
    "source_item": {"title", "url"},
    "forecast.record": {"date", "location", "maximum", "minimum"},
    "forecast.record_separator": set(),
    "forecast.source_suffix": set(),
    "forecast.official_not_verified": set(),
    "forecast.source_title": set(),
    "forecast.geocoding_title": set(),
    "restrictions.search_only": set(),
    "restrictions.no_sources": set(),
    "spoken.search_only": set(),
    "spoken.no_sources": set(),
    "spoken.suggestions": set(),
})


def _weather_task(task):
    """The delegating Talker declares the domain; the goal text is never parsed."""
    return (task.get("context") or {}).get("query_domain") == "weather"


def _forecast_raw_matches(record, raw):
    if not isinstance(record, dict) or not isinstance(raw, dict):
        return False
    low, high = record.get("temperature_min"), record.get("temperature_max")
    daily, units = raw.get("daily") or {}, raw.get("daily_units") or {}
    return (isinstance(daily, dict) and isinstance(units, dict)
            and _iso_date(record.get("date"))
            and all(type(value) in (int, float) and math.isfinite(value) for value in (low, high))
            and low <= high and record.get("unit") == "°C" and bool(record.get("timezone"))
            and raw.get("timezone") == record["timezone"] and daily.get("time") == [record.get("date")]
            and daily.get("temperature_2m_min") == [low] and daily.get("temperature_2m_max") == [high]
            and units.get("temperature_2m_min") == units.get("temperature_2m_max") == "°C")


def _iso_date(value):
    try:
        return isinstance(value, str) and len(value) == 10 and bool(date.fromisoformat(value))
    except ValueError:
        return False


def _verified_forecast_records(observations):
    return [item["record"] for item in observations if item.get("tool") == "get_weather_forecast"
            and item.get("ok") is True and item.get("forecast_verified") is True
            and _forecast_raw_matches(item.get("record", {}), item.get("raw_forecast", {}))]


def _required_source_domains(task):
    """Official domains named by the Talker in structured task context."""
    domains = (task.get("context") or {}).get("required_source_domains") or []
    return list(dict.fromkeys(domain.lower() for domain in domains if is_domain(domain)))


def _source_in_domains(url, domains):
    host = (urlparse(url).hostname or '').lower()
    return any(host == domain.lower() or host.endswith('.' + domain.lower()) for domain in domains)


def _weather_finish_guard(args, task, sources, observations):
    """Check structured forecast claims against what the tools actually returned.

    Numbers the user hears come from ``forecast_claims`` (rendered by
    ``_forecast_sentence``), so only those structured fields need checking.
    """
    if not _weather_task(task):
        return []
    records = _verified_forecast_records(observations)
    adopted = []
    for claim in args.get("forecast_claims") or []:
        match = next((record for record in records if record["date"] == claim["date"]
                      and record["source_url"] == claim["source_url"]
                      and abs(record["temperature_min"] - claim["temperature_min"]) < 1e-6
                      and abs(record["temperature_max"] - claim["temperature_max"]) < 1e-6), None)
        if match is None:
            raise ValueError("forecast_claims必须与本版本get_weather_forecast实际返回的日期、来源和最低/最高温度完全一致。")
        adopted.append(match)
    domains = _required_source_domains(task)
    status = args.get("official_source_status")
    if domains and status not in {"verified", "not_verified"}:
        raise ValueError("任务指定了官方来源，finish_task须填写official_source_status。")
    if status == "verified" and not any(source.get("extracted") is True and _source_in_domains(source.get("url", ""), domains)
                                        for source in sources.values()):
        raise ValueError("official_source_status=verified需要本任务真实读取到指定官方域名的页面；未读到请填not_verified。")
    return adopted


def _forecast_sentence(records, official_status):
    parts = [config_text("speech/thinker.toml", "forecast.record",
                         location=str((record.get('location') or {}).get('name', '')),
                         date=str(record['date']),
                         minimum=spoken_number(record['temperature_min']),
                         maximum=spoken_number(record['temperature_max'])) for record in records]
    sentence = (config_text("speech/thinker.toml", "forecast.record_separator").join(parts)
                + config_text("speech/thinker.toml", "forecast.source_suffix")) if parts else ""
    if official_status == "not_verified":
        sentence += config_text("speech/thinker.toml", "forecast.official_not_verified")
    return sentence


# JSON Schema format constraint only (AGENTS.md §3 allows pattern for YYYY-MM-DD).
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters}}


validate_config("thinker/tools.toml", {
    "search_web.description": set(),
    "extract_web.description": set(),
    "load_skill.description": set(),
    "ask_clarification.description": set(),
    "suggest_vehicle_action.description": set(),
    "finish_task.description": set(),
    "finish_task.parameters.spoken_summary": set(),
    "finish_task.parameters.reuse_prior_evidence": set(),
    "finish_task.parameters.forecast_claims": set(),
    "finish_task.parameters.official_source_status": set(),
    "finish_task.parameters.source_urls": set(),
    "get_weather_forecast.description": set(),
    "get_weather_forecast.parameters.date": set(),
    "get_weather_forecast.parameters.country_code": set(),
    "lookup_web.description": set(),
})


THINKER_TOOLS = [
    _tool("search_web", config_text("thinker/tools.toml", "search_web.description"),
          _object({"query": {"type": "string", "minLength": 1, "maxLength": 1500},
                   "max_results": {"type": "integer", "minimum": 1, "maximum": 5}}, ["query"])),
    _tool("extract_web", config_text("thinker/tools.toml", "extract_web.description"),
          _object({"urls": {"type": "array", "items": {"type": "string", "minLength": 8},
                            "minItems": 1, "maxItems": 5, "uniqueItems": True}}, ["urls"])),
    _tool("load_skill", config_text("thinker/tools.toml", "load_skill.description"),
          _object({"skill_id": {"type": "string", "enum": sorted(SKILLS)}}, ["skill_id"])),
    _tool("ask_clarification", config_text("thinker/tools.toml", "ask_clarification.description"),
          _object({"question": {"type": "string", "minLength": 1, "maxLength": 1500}}, ["question"])),
    _tool("suggest_vehicle_action", config_text("thinker/tools.toml", "suggest_vehicle_action.description"),
          _object({"name": {"type": "string", "minLength": 1, "maxLength": 120},
                   "arguments": {"type": "object"},
                   "reason": {"type": "string", "minLength": 1, "maxLength": 1500}},
                  ["name", "arguments", "reason"])),
    _tool("finish_task", config_text("thinker/tools.toml", "finish_task.description"),
          _object({"summary": {"type": "string", "minLength": 1, "maxLength": 12000},
                   "spoken_summary": {"type": "string", "minLength": 1, "maxLength": 2000,
                                      "description": config_text("thinker/tools.toml", "finish_task.parameters.spoken_summary")},
                   "reuse_prior_evidence": {"type": "boolean", "description": config_text("thinker/tools.toml", "finish_task.parameters.reuse_prior_evidence")},
                   "forecast_claims": {"type": "array", "maxItems": 7, "description": config_text("thinker/tools.toml", "finish_task.parameters.forecast_claims"),
                                       "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["date", "source_url", "temperature_min", "temperature_max"],
                                                 "properties": {"date": {"type": "string", "pattern": DATE_PATTERN},
                                                                "source_url": {"type": "string", "minLength": 8},
                                                                "temperature_min": {"type": "number"},
                                                                "temperature_max": {"type": "number"}}}},
                   "official_source_status": {"type": "string", "enum": ["verified", "not_verified"],
                                              "description": config_text("thinker/tools.toml", "finish_task.parameters.official_source_status")},
                   "source_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                                   "description": config_text("thinker/tools.toml", "finish_task.parameters.source_urls"),
                                   "uniqueItems": True}}, ["summary"])),
]
FORECAST_TOOL = _tool("get_weather_forecast", config_text("thinker/tools.toml", "get_weather_forecast.description"),
    _object({"city": {"type": "string", "minLength": 1, "maxLength": 120},
             "date": {"type": "string", "pattern": DATE_PATTERN, "description": config_text("thinker/tools.toml", "get_weather_forecast.parameters.date")},
             "country_code": {"type": "string", "pattern": r"^[A-Za-z]{2}$", "description": config_text("thinker/tools.toml", "get_weather_forecast.parameters.country_code")}}, ["city", "date"]))
THINKER_TOOLS.append(FORECAST_TOOL)
TOOL_SCHEMAS = {tool["function"]["name"]: tool["function"]["parameters"] for tool in THINKER_TOOLS}
LOOKUP_TOOL = _tool("lookup_web", config_text("thinker/tools.toml", "lookup_web.description"),
    _object({"query": {"type": "string", "minLength": 1, "maxLength": 1500},
             "max_results": {"type": "integer", "minimum": 1, "maximum": 3}}, ["query"]))
QUICK_TOOLS = [FORECAST_TOOL, LOOKUP_TOOL] + [tool for tool in THINKER_TOOLS
    if tool["function"]["name"] in {"ask_clarification", "finish_task"}]
TOOL_SCHEMAS["lookup_web"] = LOOKUP_TOOL["function"]["parameters"]
QUICK_PROMPT = render_prompt("thinker_quick")
SYSTEM_PROMPT = render_prompt("thinker_research", skill_guide=SKILL_GUIDE)


class RevisionChanged(Exception):
    def __init__(self, task: dict[str, Any]):
        self.task = task


class TaskCancelled(Exception):
    pass


def _safe_error(exc: BaseException, max_length: int | None = 500) -> str:
    """Never let provider diagnostics disclose credentials in cards or logs."""
    value = redact(str(exc))
    return f"{type(exc).__name__}: {value if max_length is None else value[:max_length]}"


class ThinkerWorker:
    def __init__(self, db: Any, ark: Any = None, tavily: Any = None, *,
                 max_steps: int = 20, timeout_seconds: float = 60,
                 concurrency: int = 2,
                 catalog: dict[str, Any] | None = None,
                 tool_timeout_seconds: float | None = None,
                 quick_max_steps: int = 4, quick_timeout_seconds: float = 60, weather: Any = None):
        self.db = db
        if ark is None or tavily is None:
            from .models import ArkClient, TavilyClient
            ark = ark or ArkClient()
            tavily = tavily or TavilyClient()
        self.ark, self.tavily = ark, tavily
        self.weather = weather
        self.max_steps = max(1, min(20, max_steps))
        self.timeout_seconds = max(0.01, min(60, timeout_seconds))
        self.concurrency = max(1, concurrency)
        self.quick_max_steps = max(1, min(self.max_steps, 4, quick_max_steps))
        self.quick_timeout_seconds = max(0.01, min(60, quick_timeout_seconds))
        self.tool_timeout_seconds = max(0.01, tool_timeout_seconds if tool_timeout_seconds is not None
                                        else float(os.getenv("TOOL_TIMEOUT_SECONDS", "35")))
        self.catalog = catalog
        self._stopping = asyncio.Event()
        self._active: dict[str, asyncio.Task] = {}

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self, poll_interval: float = 0.15) -> None:
        """Claim only free slots. Clarification returns immediately to this loop."""
        try:
            while not self._stopping.is_set():
                self.db.expire_tasks(self.timeout_seconds)
                for task_id, future in list(self._active.items()):
                    current = self.db.get_task(task_id)
                    if current and current['status'] in {'completed', 'failed', 'cancelled'} and not future.done():
                        future.cancel()
                    if future.done():
                        # run_task handles ordinary failures, but surface unexpected ones.
                        if not future.cancelled() and future.exception():
                            logger.error("Thinker task failed unexpectedly: %s", _safe_error(future.exception()))
                        del self._active[task_id]
                # A queued task can be cancelled before any worker claims it.
                # Database.apply_pending is also used by mutate_task in the actor.
                while len(self._active) < self.concurrency and not self._stopping.is_set():
                    task = self.db.claim_next(exclude_ids=tuple(self._active))
                    if task is None:
                        break
                    if task["id"] in self._active:
                        # A cancelling task remains claimable until its currently
                        # active coroutine reaches a boundary. Yield to it.
                        break
                    self._active[task["id"]] = asyncio.create_task(self.run_task(task))
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            for future in self._active.values():
                future.cancel()
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    def _boundary(self, task_id: str, revision: int | None = None) -> dict[str, Any]:
        task = self.db.apply_pending(task_id)
        if not task:
            raise RuntimeError("Task no longer exists")
        if task["status"] in ("cancelling", "cancelled"):
            self.db.save_task(task_id, task["revision"], status="cancelled", summary=config_text("speech/thinker.toml", "cancelled"), question=None)
            raise TaskCancelled()
        if task["status"] in ("completed", "failed"):
            # A concurrent deadline sweep may have closed this task while an
            # external request was in flight. Never resurrect it.
            raise TaskCancelled()
        if task['status'] != 'waiting_clarification' and datetime.fromisoformat(task['deadline_at']) <= datetime.now(timezone.utc):
            self.db.expire_tasks(self.timeout_seconds)
            raise TaskCancelled()
        if revision is not None and task["revision"] != revision:
            raise RevisionChanged(task)
        return task

    def _save(self, task_id: str, revision: int, **fields: Any) -> None:
        if not self.db.save_task(task_id, revision, **fields):
            raise RevisionChanged(self._boundary(task_id))

    def _messages(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        # A new goal starts with a fresh context; revisions resume checkpoints.
        context = {"goal": task["goal"], "constraints": task.get("constraints", []),
                   "revision": task["revision"], "query_mode": self._query_mode(task),
                   "current_utc": datetime.now(timezone.utc).isoformat()}
        if task.get("context"):
            context["context"] = deepcopy(task["context"])
            # Talker routing metadata is redundant in a suggestion-only worker.
            if isinstance(context['context'].get('speaker'), dict):
                context['context']['speaker'].pop('control_defaults', None)
        # apply_pending may store a user's reply in transcript or a replies field.
        if task.get("replies"):
            context["user_replies"] = task["replies"]
        prompt = QUICK_PROMPT if self._query_mode(task) == "quick" else SYSTEM_PROMPT
        messages = [{"role": "system", "content": prompt}]
        speaker = (task.get('context') or {}).get('speaker')
        if speaker:
            defaults = speaker_context(speaker['seat'])
            defaults.pop('control_defaults')
            messages.append({'role': 'system', 'content': render_prompt('speaker_context',
                speaker_json=json.dumps(defaults, ensure_ascii=False))})
        messages.append({"role": "user", "content": json.dumps(context, ensure_ascii=False)})
        return messages

    @staticmethod
    def _query_mode(task: dict[str, Any]) -> str:
        # The delegating model chooses this field; no goal-keyword routing.
        return "quick" if (task.get("context") or {}).get("query_mode") == "quick" else "research"

    def _quick_usage(self, task: dict[str, Any]) -> tuple[float, int]:
        elapsed, lookups, cursor = 0.0, 0, 0
        while True:
            rows = self.db.traces_after(task["session_id"], after_id=cursor, task_id=task["id"])
            for row in rows:
                data = row["data"]
                if row["kind"] == "task.execution.finished" and data.get("query_mode") == "quick":
                    elapsed += max(0, float(data.get("elapsed_seconds", 0)))
                if row["kind"] == "tool.started" and data.get("name") == "lookup_web":
                    lookups += 1
            if len(rows) < 200:
                return elapsed, lookups
            cursor = rows[-1]["trace_id"]

    def _trace(self, task: dict[str, Any], kind: str, data: dict[str, Any], *,
               revision: int | None = None, step: int | None = None,
               call_id: str | None = None, status: str | None = None,
               model_call_id: str | None = None) -> dict[str, Any]:
        # append_trace completes its own short transaction before any await.
        # Record only model inputs/outputs, never client headers or configuration.
        return self.db.append_trace(task["session_id"], kind,
            {"agent": "thinker", "model_call_index": step, "model_call_id": model_call_id,
             "observed_at": datetime.now(timezone.utc).isoformat(), **data},
            task_id=task["id"], revision=revision if revision is not None else task["revision"],
            turn_id=(task.get("context") or {}).get("origin_turn_id") or
                    (task.get("context") or {}).get("turn_id"), step=step,
            call_id=call_id, status=status)

    def _response_status(self, task_id: str, revision: int, default: str) -> str:
        latest = self.db.get_task(task_id)
        if not latest or latest['revision'] != revision or latest['status'] in {
                'cancelling', 'cancelled', 'completed', 'failed'}:
            return 'stale'
        return default

    @staticmethod
    def _has_current_attempt(observations: list[dict[str, Any]]) -> bool:
        return any(item.get("attempted") is True and item.get("tool") in {
            "search_web", "extract_web", "get_weather_forecast"} for item in observations)

    async def run_task(self, task: dict[str, Any]) -> None:
        started = time.perf_counter()
        mode = self._query_mode(task)
        deadline_reason = "running_budget"
        progress_timer = None
        try:
            # The persisted wall deadline includes queue time and survives a restart.
            remaining = min(self.timeout_seconds - self.db.running_elapsed(task),
                            (datetime.fromisoformat(task['deadline_at']) - datetime.now(timezone.utc)).total_seconds())
            if remaining <= 0:
                raise asyncio.TimeoutError()
            if mode == "quick":
                spent, _ = self._quick_usage(task)
                execution_remaining = self.quick_timeout_seconds - spent
                if execution_remaining < remaining:
                    remaining, deadline_reason = execution_remaining, "quick_execution_budget"
                if remaining <= 0:
                    raise asyncio.TimeoutError()
            end = time.perf_counter() + remaining
            async def notify_progress():
                delay = (datetime.fromisoformat(task['deadline_at']) - datetime.now(timezone.utc)).total_seconds() - 30
                await asyncio.sleep(max(0, delay))
                self.db.notify_task_progress(task['id'])
            progress_timer = asyncio.create_task(notify_progress())
            # Reserve delivery time instead of letting one more search consume the minute.
            budget = min(self.timeout_seconds, self.quick_timeout_seconds) if mode == 'quick' else self.timeout_seconds
            reserve = min(15, budget / 4, remaining)
            try:
                await asyncio.wait_for(self._run_task(task), remaining - reserve)
            except asyncio.TimeoutError:
                current = self._boundary(task['id'])
                self._trace(current, 'task.finalizing', {'remaining_seconds': max(0, end - time.perf_counter())}, status='running')
                try:
                    await asyncio.wait_for(self._finalize(current), max(.001, end - time.perf_counter()))
                except (TaskCancelled, RevisionChanged):
                    raise
                except asyncio.TimeoutError:
                    raise
                except Exception as exc:
                    self._trace(current, 'task.finalize_error', {'error': _safe_error(exc, None)}, status='error')
                    self._fail_revision(task['id'], current['revision'], _safe_error(exc), code='finalization_failed')
        except RevisionChanged as changed:
            self._trace(task, 'revision.discarded', {'new_revision': changed.task['revision'], 'phase': 'finalize'}, status='stale')
            self._fail_current(task['id'], '条件在收尾期间更新，未能在时间上限内核实。', code='deadline')
        except TaskCancelled:
            return
        except asyncio.TimeoutError:
            current = self.db.get_task(task["id"]) or task
            self._trace(current, "task.timeout", {"reason": deadline_reason, "query_mode": mode},
                        step=current.get("steps"), status="failed")
            error = (f"快速查询累计执行超过{self.quick_timeout_seconds:g}秒时间上限，已停止；没有宣称完成。"
                     if deadline_reason == "quick_execution_budget" else "任务超过时间上限，已停止；没有宣称完成。")
            self._fail_current(task["id"], error, code='deadline')
        except asyncio.CancelledError:
            # Process shutdown is outside the promised continuous-online scope.
            self._trace(task, "worker.interrupted", {"reason": "worker_coroutine_cancelled"},
                        step=task.get("steps"), status="cancelled")
            raise
        except Exception as exc:
            # Unexpected setup errors still belong to the claimed revision. An
            # old failure must never terminate subsequently updated conditions.
            self._trace(task, "step.error", {"error": _safe_error(exc, None), "phase": "setup"},
                        step=task.get("steps"), status="error")
            self._fail_revision(task["id"], task["revision"], _safe_error(exc))
        finally:
            if progress_timer:
                progress_timer.cancel()
                await asyncio.gather(progress_timer, return_exceptions=True)
            if mode == "quick":
                current = self.db.get_task(task["id"]) or task
                self._trace(current, "task.execution.finished", {"query_mode": mode,
                    "elapsed_seconds": time.perf_counter() - started},
                    step=current.get("steps"), status=current["status"])

    def _fail_revision(self, task_id: str, revision: int, error: str, code='execution_failed') -> bool:
        """Return False if the failing work became stale and should be retried."""
        try:
            current = self._boundary(task_id, revision)
            closed = {**current, 'status': 'failed', 'error_code': code}
            return self.db.save_task(task_id, revision, status="failed", error=error, error_code=code,
                                     summary=task_user_detail(closed), result=partial_result(closed), question=None)
        except RevisionChanged:
            return False
        except TaskCancelled:
            return True

    def _fail_current(self, task_id: str, error: str, code='execution_failed') -> None:
        try:
            current = self._boundary(task_id)
            self._fail_revision(task_id, current['revision'], error, code=code)
        except TaskCancelled:
            pass

    async def _finalize(self, task):
        """One bounded delivery call, with no permission to perform more IO tools."""
        revision, task_id = task['revision'], task['id']
        checkpoint = task.get('checkpoint') or {}
        if not checkpoint:
            self._fail_current(task_id, '任务未能在时间上限内取得资料。', code='deadline')
            return
        messages = deepcopy(checkpoint.get('messages') or self._messages(task))
        sources = {item['url']: deepcopy(item) for item in checkpoint.get('sources', [])}
        observations = deepcopy(checkpoint.get('observations', []))
        suggestions = deepcopy(checkpoint.get('suggestions', []))
        citable = {f'source_{index}': source for index, source in enumerate(sources.values(), 1)
                   if source.get('extracted') is True or source.get('verified') is True}
        # Cancellation can cut a batch between tool calls; close the protocol group
        # explicitly without pretending those pending calls ran.
        answered = {item.get('tool_call_id') for item in messages if item.get('role') == 'tool'}
        for item in list(messages):
            for call in item.get('tool_calls') or []:
                if call['id'] not in answered:
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': json.dumps({'ok': False, 'executed': False, 'code': 'stopped_for_delivery'})})
        messages.append({'role': 'user', 'content': json.dumps({
            'instruction': render_prompt('thinker_finalize') + '\n' + render_prompt('thinker_source_selection'),
            'constraints': task.get('constraints', []),
            'revision': revision, 'citable_sources': citable}, ensure_ascii=False)})
        tools = deepcopy([tool for tool in THINKER_TOOLS if tool['function']['name'] == 'finish_task'])
        schema = tools[0]['function']['parameters']
        schema['properties'].pop('source_urls')
        schema['properties']['source_ids'] = {'type': 'array', 'items': {'type': 'string', 'enum': list(citable)}
                                             if citable else {'type': 'string'},
                                             'maxItems': min(20, len(citable)), 'uniqueItems': True}
        schema['required'] = ['summary', 'spoken_summary', 'source_ids']
        call_id = uuid4().hex
        request = {'messages': deepcopy(messages), 'tools': tools, 'thinker': False,
                   'thinking': False, 'max_tokens': 2500,
                   'tool_choice': {'type': 'function', 'function': {'name': 'finish_task'}}}
        self._save(task_id, revision, progress={'phase': 'finish_task', 'read_sources': sum(bool(s.get('extracted')) for s in sources.values())},
                   summary=PHASES['finish_task'])
        self._trace(task, 'model.started', {'request': request, 'phase': 'finalize'}, call_id=call_id, model_call_id=call_id, status='running')
        try:
            response = await self.ark.complete(**request)
        except BaseException as exc:
            self._trace(task, 'model.error', {'error': _safe_error(exc, None), 'phase': 'finalize'},
                        call_id=call_id, model_call_id=call_id, status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'error')
            raise
        response = deepcopy(response)
        meta = response.pop('_meta', {})
        self._trace(task, 'model.completed', {'message': response, 'meta': meta, 'phase': 'finalize'},
                    call_id=call_id, model_call_id=call_id, status=self._response_status(task_id, revision, 'received'))
        self._boundary(task_id, revision)
        calls = response.get('tool_calls') or []
        if len(calls) != 1 or calls[0].get('function', {}).get('name') != 'finish_task':
            raise ValueError('收尾未提交结构化结果。')
        call = calls[0]
        raw_args = json.loads(call['function']['arguments'])
        Draft202012Validator(schema).validate(raw_args)
        args = deepcopy(raw_args)
        selected = [citable[identity] for identity in args.pop('source_ids')]
        args['source_urls'] = [source['url'] for source in selected]
        if checkpoint.get('revision') != revision:
            # Old material must not silently become a result for updated conditions.
            raise ValueError('任务条件在收尾前变更，已有资料尚未核实新条件。')
        adopted = _weather_finish_guard(args, task, sources, observations)
        self._trace(task, 'tool.started', {'name': 'finish_task', 'arguments': raw_args, 'resolved_arguments': args, 'call': call},
                    call_id=call['id'], model_call_id=call_id, status='running')
        result = await self._execute_tool('finish_task', args, sources, suggestions, observations, adopted_forecasts=adopted)
        if selected:
            result['result']['summary'] += config_text("speech/thinker.toml", "source_heading") + '\n'.join(
                config_text("speech/thinker.toml", "source_item",
                            title=str(source.get('title') or source['url']), url=str(source['url'])) for source in selected)
        self._trace(task, 'tool.completed', {'name': 'finish_task', 'arguments': args, 'result': result},
                    call_id=call['id'], model_call_id=call_id, status=self._response_status(task_id, revision, 'completed'))
        self._boundary(task_id, revision)
        result['result'].update(partial=True, query_mode=self._query_mode(task))
        messages.extend([response, {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)}])
        self._save(task_id, revision, status='completed', result=result['result'], summary=result['result']['summary'],
                   transcript=messages, sources=list(sources.values()), question=None)

    async def _run_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        current = self._boundary(task_id)
        revision = -1
        messages: list[dict[str, Any]] = []
        sources: dict[str, dict[str, Any]] = {}
        suggestions: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        tool_count = 0
        _, lookup_count = self._quick_usage(current) if self._query_mode(current) == "quick" else (0, 0)
        for step in range(int(current.get("steps", 0)) + 1, self.max_steps + 1):
            phase = "boundary"
            model_call_id = None
            try:
                current = self._boundary(task_id)
                mode = self._query_mode(current)
                step_limit = self.quick_max_steps if mode == "quick" else self.max_steps
                if step > step_limit:
                    self._fail_current(task_id, "任务达到推理步骤上限，已停止；没有宣称完成。", code='step_budget')
                    return
                tools = QUICK_TOOLS if mode == "quick" else THINKER_TOOLS
                thinker = mode != "quick"
                options = {}
                if mode == "quick":
                    options["tool_choice"] = "required"
                    if step == step_limit:
                        tools = [tool for tool in QUICK_TOOLS if tool["function"]["name"] == "finish_task"]
                        options["tool_choice"] = {"type": "function", "function": {"name": "finish_task"}}
                    elif lookup_count >= 2:
                        tools = [tool for tool in QUICK_TOOLS if tool["function"]["name"] in {"get_weather_forecast", "finish_task"}]
                if revision != current["revision"]:
                    previous_revision = revision if revision >= 0 else None
                    revision = current["revision"]
                    checkpoint = current.get('checkpoint') or {}
                    restored = (checkpoint.get('goal') == current['goal'] and bool(checkpoint.get('messages'))
                                and (checkpoint.get('revision') == revision or any(
                                    item.get('role') == 'tool' for item in checkpoint['messages'])))
                    if restored:
                        messages = deepcopy(checkpoint['messages'])
                        sources = {item['url']: deepcopy(item) for item in checkpoint.get('sources', [])}
                        suggestions = deepcopy(checkpoint.get('suggestions', []))
                        observations = deepcopy(checkpoint.get('observations', []))
                        if checkpoint.get('revision') != revision:
                            for observation in observations:
                                observation['attempted'] = False
                                observation['from_revision'] = checkpoint.get('revision')
                        tool_count = checkpoint.get('tool_count', 0)
                        prior_count = checkpoint.get('constraint_count', 0)
                        additions = current.get('constraints', [])[prior_count:]
                        if additions:
                            messages.append({'role': 'user', 'content': json.dumps({
                                'revision': revision, 'additional_conditions': additions, 'current_utc': datetime.now(timezone.utc).isoformat(),
                                'instruction': render_prompt('thinker_revision')}, ensure_ascii=False)})
                    else:
                        messages = self._messages(current)
                        sources, suggestions, observations = {}, [], []
                        tool_count = 0
                    self._trace(current, "revision.started", {"previous_revision": previous_revision,
                                "checkpoint_restored": restored,
                                "source_count": len(sources)}, revision=revision, step=step, status="running")
                if (mode == "quick" and step < step_limit
                        and (current.get("context") or {}).get("query_domain") == "weather"):
                    forecast_attempted = any(item.get("tool") == "get_weather_forecast"
                                             and item.get("attempted") is True for item in observations)
                    records = _verified_forecast_records(observations)
                    domains = _required_source_domains(current)
                    if not forecast_attempted:
                        available = {"get_weather_forecast", "ask_clarification"}
                    elif not records:
                        available = {"finish_task", "ask_clarification"}
                    elif domains and lookup_count < 2 and not all(any(
                            source.get("extracted") is True and _source_in_domains(source.get("url", ""), [domain])
                            for source in sources.values()) for domain in domains):
                        available = {"lookup_web", "ask_clarification"}
                    else:
                        available = None
                    if available is not None:
                        tools = [tool for tool in QUICK_TOOLS if tool["function"]["name"] in available]
                if mode == "quick" and lookup_count >= 2 and not sources and not self._has_current_attempt(observations) and (
                        not _weather_task(current) or step == step_limit):
                    error = config_text("speech/thinker.toml", "quick_budget_exhausted")
                    self._trace(current, "task.budget_exhausted", {"reason": "quick_lookup_budget",
                        "lookup_limit": 2, "current_revision_attempts": 0}, revision=revision,
                        step=current.get("steps"), status="failed")
                    self._save(task_id, revision, status="failed", error=error, error_code='step_budget', summary=error,
                               result=None, sources=[], transcript=messages, question=None)
                    return
                finalizing = step == step_limit or tool_count >= step_limit
                if finalizing:
                    tools = [tool for tool in THINKER_TOOLS if tool['function']['name'] == 'finish_task']
                    thinker = False
                    options['thinking'] = False
                    options['max_tokens'] = 2500
                    options['tool_choice'] = {'type': 'function', 'function': {'name': 'finish_task'}}
                    messages.append({'role': 'user', 'content': render_prompt('thinker_finalize')})
                self._save(task_id, revision, status="running", steps=step, error=None,
                           summary=PHASES['finish_task' if finalizing else 'model'], question=None,
                           progress={'phase': 'finish_task' if finalizing else 'model',
                                     'read_sources': sum(bool(s.get('extracted')) for s in sources.values())},
                           checkpoint=self._checkpoint(current, revision, messages, sources, suggestions, observations, tool_count))
                self._trace(current, "step.started", {}, revision=revision, step=step, status="running")
                phase = "model"
                model_call_id = uuid4().hex
                self._trace(current, "model.started", {"request": {
                    "messages": deepcopy(messages), "tools": tools, "thinker": thinker, **options}, "query_mode": mode},
                    revision=revision, step=step, call_id=model_call_id,
                    model_call_id=model_call_id, status="running")
                try:
                    response = await self.ark.complete(deepcopy(messages), tools=tools, thinker=thinker, **options)
                except asyncio.CancelledError as exc:
                    self._trace(current, "model.error", {"error": _safe_error(exc, None),
                                "partial": getattr(exc, 'trace_partial', None)}, revision=revision,
                                step=step, call_id=model_call_id, model_call_id=model_call_id, status="cancelled")
                    raise
                except Exception as exc:
                    self._trace(current, "model.error", {"error": _safe_error(exc, None),
                                "partial": getattr(exc, 'trace_partial', None)}, revision=revision,
                                step=step, call_id=model_call_id, model_call_id=model_call_id,
                                status=self._response_status(task_id, revision, 'error'))
                    raise
                # Preserve the full response before checking whether it became
                # stale. Provider metadata is diagnostic data, never a message.
                response = deepcopy(response)
                meta = response.pop("_meta", {}) if isinstance(response, dict) else {}
                self._trace(current, "model.completed", {"message": response, "meta": meta},
                            revision=revision, step=step, call_id=model_call_id,
                            model_call_id=model_call_id, status=self._response_status(task_id, revision, 'received'))
                self._boundary(task_id, revision)
                if not isinstance(response, dict):
                    raise RuntimeError("Thinker returned a malformed message")
                message = {"role": "assistant", "content": response.get("content")}
                calls = response.get("tool_calls") or []
                if calls:
                    message["tool_calls"] = calls
                messages.append(message)
                if not calls:
                    # Ask once for a structured terminal result instead of silently
                    # treating unsupported model text as successful research.
                    messages.append({"role": "user", "content": render_prompt("thinker_retry")})
                    self._save(task_id, revision, transcript=messages)
                    continue
                if not isinstance(calls, list) or len(calls) > 8:
                    raise RuntimeError("Thinker returned too many or malformed tool calls")
                terminal: tuple[str, dict[str, Any]] | None = None
                for index, call in enumerate(calls):
                    phase = "tool"
                    self._boundary(task_id, revision)
                    function = call.get("function", {})
                    name = function.get("name", "")
                    call_id = call.get("id", f"step{step}_{index}")
                    arguments = function.get("arguments", "{}")
                    actual_attempt = False
                    self._trace(current, "tool.started", {"name": name, "call": call,
                                "arguments": arguments}, revision=revision, step=step,
                                call_id=call_id, model_call_id=model_call_id, status="running")
                    try:
                        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                        if name not in {item["function"]["name"] for item in tools}:
                            raise ValueError(f"Unknown tool: {name}")
                        errors = sorted(Draft202012Validator(TOOL_SCHEMAS[name]).iter_errors(arguments),
                                        key=lambda item: str(list(item.path)))
                        if errors:
                            raise ValueError("参数校验失败：" + errors[0].message)
                        if terminal:
                            result = {"ok": False, "error": "本轮已提交终止操作，剩余调用未执行。"}
                        else:
                            if name not in {'finish_task', 'ask_clarification'}:
                                if tool_count >= step_limit:
                                    raise ValueError('检索预算已用尽，请用已有材料finish_task。')
                                tool_count += 1
                            self._save(task_id, revision, summary=self._progress(name, arguments),
                                       progress={'phase': name, 'read_sources': sum(bool(s.get('extracted')) for s in sources.values())})
                            if name == "lookup_web":
                                lookup_count += 1
                                if lookup_count > 2:
                                    raise ValueError("快速查询最多两次lookup，请根据实际材料finish_task，不能确认就如实说明。")
                                operation = self._quick_lookup(arguments, sources, suggestions, observations,
                                    current, revision, step, model_call_id, call_id)
                            else:
                                if (mode == "quick" and name == "finish_task" and not self._has_current_attempt(observations)
                                        and not (arguments.get("reuse_prior_evidence") is True and sources
                                                 and any(source.get("extracted") or source.get("verified") for source in sources.values()))):
                                    raise ValueError("当前版本须先lookup_web或get_weather_forecast真实核实；旧版本查询不能完成新条件。必要条件不足请ask_clarification。")
                                adopted = (_weather_finish_guard(arguments, current, sources, observations)
                                           if name == "finish_task" else [])
                                operation = self._execute_tool(name, arguments, sources, suggestions, observations,
                                                               source_domains=_required_source_domains(current),
                                                               adopted_forecasts=adopted)
                            actual_attempt = True
                            result = await asyncio.wait_for(
                                operation,
                                timeout=self.tool_timeout_seconds)
                            if name == "finish_task" and result.get("ok"):
                                result["result"]["query_mode"] = mode
                                if finalizing:
                                    result['result']['partial'] = True
                            self._trace(current, "tool.completed", {"name": name,
                                        "arguments": arguments, "result": result}, revision=revision,
                                        step=step, call_id=call_id, model_call_id=model_call_id,
                                        status=self._response_status(task_id, revision, "completed" if result.get("ok") else "rejected"))
                            self._boundary(task_id, revision)
                            if name == "ask_clarification" and result.get("ok"):
                                terminal = ("waiting_clarification", {"question": arguments["question"],
                                            "summary": arguments["question"]})
                            elif name == "finish_task" and result.get("ok"):
                                terminal = ("completed", {"result": result["result"],
                                            "summary": result["result"]["summary"], "question": None})
                        if terminal and result.get("ok") is False:
                            self._trace(current, "tool.completed", {"name": name,
                                        "arguments": arguments, "result": result}, revision=revision,
                                        step=step, call_id=call_id, model_call_id=model_call_id, status="skipped")
                    except (RevisionChanged, TaskCancelled):
                        raise
                    except asyncio.CancelledError as exc:
                        self._trace(current, "tool.error", {"name": name, "arguments": arguments,
                                    "error": _safe_error(exc, None)}, revision=revision,
                                    step=step, call_id=call_id, model_call_id=model_call_id, status="cancelled")
                        raise
                    except Exception as exc:
                        result = {"ok": False, "error": _safe_error(exc)}
                        if name == "finish_task":
                            result["citable_source_urls"] = list(sources)
                            result["forecast_records"] = deepcopy(_verified_forecast_records(observations))
                        self._trace(current, "tool.error", {"name": name, "arguments": arguments,
                                    "error": _safe_error(exc, None), "result": result}, revision=revision,
                                    step=step, call_id=call_id, model_call_id=model_call_id,
                                    status=self._response_status(task_id, revision, 'error'))
                        observations.append({"tool": name, "ok": False, "attempted": actual_attempt,
                                             "error": result["error"]})
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": json.dumps(result, ensure_ascii=False)})
                    # Persist each completed tool before the next await, so timed
                    # delivery can reuse real evidence even in a cancelled batch.
                    self._save(task_id, revision, sources=list(sources.values()),
                               checkpoint=self._checkpoint(current, revision, messages, sources, suggestions, observations, tool_count))
                self._save(task_id, revision, transcript=messages, sources=list(sources.values()),
                    checkpoint={'goal': current['goal'], 'revision': revision, 'messages': messages,
                                'sources': list(sources.values()), 'suggestions': suggestions,
                                'observations': observations, 'tool_count': tool_count,
                                'constraint_count': len(current.get('constraints', []))})
                if terminal:
                    status, fields = terminal
                    self._boundary(task_id, revision)
                    self._save(task_id, revision, status=status, transcript=messages,
                               sources=list(sources.values()), **fields)
                    return
            except RevisionChanged as changed:
                self._trace(current, "revision.discarded", {"new_revision": changed.task["revision"],
                            "phase": phase, "reason": "updated_during_step"}, revision=revision,
                            step=step, call_id=model_call_id, model_call_id=model_call_id, status="stale")
                current = changed.task
                # Next iteration reconstructs the prompt. The already spent time
                # and steps still count towards the same bounded task execution.
                continue
            except TaskCancelled:
                latest = self.db.get_task(task_id) or current
                self._trace(current, "step.stopped", {"phase": phase,
                            "latest_revision": latest["revision"], "latest_status": latest["status"]},
                            revision=revision, step=step, call_id=model_call_id,
                            model_call_id=model_call_id, status="cancelled")
                raise
            except Exception as exc:
                self._trace(current, "step.error", {"phase": phase, "error": _safe_error(exc, None)},
                            revision=revision, step=step, call_id=model_call_id, model_call_id=model_call_id,
                            status=self._response_status(task_id, revision, 'error'))
                if self._fail_revision(task_id, revision, _safe_error(exc)):
                    return
                latest = self.db.get_task(task_id) or current
                self._trace(current, "revision.discarded", {"new_revision": latest["revision"],
                            "phase": phase, "reason": "error_from_old_revision", "error": _safe_error(exc, None)},
                            revision=revision, step=step, call_id=model_call_id,
                            model_call_id=model_call_id, status="stale")
                # A model/network failure from an old revision is discarded just
                # like a late successful response. Continue with new conditions.
                continue
        self._fail_current(task_id, "任务达到推理步骤上限，已停止；没有宣称完成。", code='step_budget')

    @staticmethod
    def _checkpoint(task, revision, messages, sources, suggestions, observations, tool_count):
        return {'goal': task['goal'], 'revision': revision, 'messages': messages,
                'sources': list(sources.values()), 'suggestions': suggestions,
                'observations': observations, 'tool_count': tool_count,
                'constraint_count': len(task.get('constraints', []))}

    async def _quick_lookup(self, args, sources, suggestions, observations, task, revision,
                            step, model_call_id, parent_call_id):
        async def invoke(name, arguments):
            self._boundary(task["id"], revision)
            call_id = uuid4().hex
            self._trace(task, "tool.started", {"name": name, "arguments": arguments,
                "parent_call_id": parent_call_id}, revision=revision, step=step, call_id=call_id,
                model_call_id=model_call_id, status="running")
            try:
                result = await self._execute_tool(name, arguments, sources, suggestions, observations,
                                                  source_domains=_required_source_domains(task))
            except (RevisionChanged, TaskCancelled):
                raise
            except BaseException as exc:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else self._response_status(task["id"], revision, "error")
                self._trace(task, "tool.error", {"name": name, "arguments": arguments,
                    "error": _safe_error(exc, None), "parent_call_id": parent_call_id},
                    revision=revision, step=step, call_id=call_id, model_call_id=model_call_id, status=status)
                observations.append({"tool": name, "ok": False, "attempted": True, "error": _safe_error(exc)})
                raise
            self._trace(task, "tool.completed", {"name": name, "arguments": arguments,
                "result": result, "parent_call_id": parent_call_id}, revision=revision, step=step,
                call_id=call_id, model_call_id=model_call_id,
                status=self._response_status(task["id"], revision, "completed"))
            self._boundary(task["id"], revision)
            return result

        search = await invoke("search_web", {"query": args["query"], "max_results": args.get("max_results", 3)})
        urls = []
        domains = _required_source_domains(task)
        for row in search.get("results", []):
            url = row.get("url") if isinstance(row, dict) else None
            if domains and (not isinstance(url, str) or not _source_in_domains(url, domains)):
                continue
            if url in sources and url not in urls:
                urls.append(url)
            if len(urls) == 2:
                break
        if not urls:
            return {"ok": True, "search": search, "extract": None,
                    "notice": config_text("speech/thinker.toml", "lookup_no_sources")}
        try:
            extract = await invoke("extract_web", {"urls": urls})
        except (RevisionChanged, TaskCancelled):
            raise
        except Exception as exc:
            extract = {"ok": False, "error": _safe_error(exc)}
        return {"ok": True, "search": search, "extract": extract}

    @staticmethod
    def _progress(name: str, arguments: dict[str, Any]) -> str:
        return PHASES.get(name, config_text("speech/thinker.toml", "processing"))

    async def _execute_tool(self, name: str, args: dict[str, Any], sources: dict[str, Any],
                            suggestions: list[dict[str, Any]], observations: list[dict[str, Any]], *,
                            source_domains=None, adopted_forecasts=()) -> dict[str, Any]:
        if name == "get_weather_forecast":
            if self.weather is None:
                from .weather import WeatherClient
                self.weather = WeatherClient()
            data = await self.weather.forecast(args["city"], args["date"], country_code=args.get("country_code"))
            if data.get("ok") is False:
                observations.append({"tool": name, "ok": False, "attempted": True,
                                     "needs_clarification": bool(data.get("needs_clarification"))})
                return data
            if data.get("ok") is not True or data.get("data_type") != "forecast" or data.get("date") != args["date"]:
                raise ValueError("天气工具未返回所请求日期的真实forecast记录，不能当预报。")
            low, high = data.get("temperature_min"), data.get("temperature_max")
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in (low, high)) or low > high:
                raise ValueError("天气工具的温度记录无效，不能当预报。")
            raw_forecast = (data.get("raw") or {}).get("forecast")
            if not isinstance(raw_forecast, dict) or not _forecast_raw_matches(data, raw_forecast):
                raise ValueError("天气工具的date/timezone/摄氏单位或温度与原始raw不一致，不能补造或当作已验证预报。")
            url = data.get("source_url", "")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or not data.get("queried_at") or not data.get("timezone") or data.get("unit") != "°C":
                raise ValueError("天气工具缺少真实来源、查询时间、时区或摄氏单位，不能当预报。")
            geo_url = data.get("geocoding_source_url")
            if geo_url:
                geo = urlparse(geo_url)
                if geo.scheme not in ("http", "https") or not geo.netloc:
                    raise ValueError("天气工具的地理编码来源 URL 无效。")
            record = {key: deepcopy(data[key]) for key in ("date", "location", "timezone", "temperature_min", "temperature_max", "unit", "source_url", "queried_at")}
            sources[url] = {"url": url, "title": config_text("speech/thinker.toml", "forecast.source_title"), "queried_at": data["queried_at"],
                            "kind": "forecast", "verified": True, "extracted": False,
                            "forecast_date": data["date"], "timezone": data["timezone"], "unit": data["unit"]}
            if geo_url:
                sources[geo_url] = {"url": geo_url, "title": config_text("speech/thinker.toml", "forecast.geocoding_title"), "queried_at": data["queried_at"],
                                    "kind": "geocoding", "verified": True, "extracted": False,
                                    "supports": "location"}
            observations.append({"tool": name, "ok": True, "attempted": True,
                                 "record": record, "queried_at": data["queried_at"],
                                 "forecast_verified": True, "raw_forecast": deepcopy(raw_forecast)})
            return data
        if name == "search_web":
            options = {'include_domains': source_domains} if source_domains else {}
            data = await self.tavily.search(args["query"], max_results=args.get("max_results", 5), **options)
            self._collect_sources(data, sources, "search")
            observations.append({"tool": name, "query": args["query"], "ok": True, "attempted": True,
                                 "result_count": len(data.get("results", [])), "queried_at": data.get("queried_at")})
            return {"ok": True, **data}
        if name == "extract_web":
            for url in args["urls"]:
                parsed = urlparse(url)
                if parsed.scheme not in ("https", "http") or not parsed.netloc or url not in sources:
                    raise ValueError("只能读取本任务真实搜索获得的 http(s) 来源 URL。")
            data = await self.tavily.extract(args["urls"])
            self._collect_sources(data, sources, "extract")
            observations.append({"tool": name, "ok": True, "attempted": True, "result_count": len(data.get("results", [])),
                                 "failed_results": data.get("failed_results", []), "queried_at": data.get("queried_at")})
            return {"ok": True, **data}
        if name == "load_skill":
            from .catalog import load_skill
            return load_skill(args["skill_id"])
        if name == "ask_clarification":
            return {"ok": True, "question": args["question"]}
        if name == "suggest_vehicle_action":
            if self.catalog is None:
                from .catalog import TOOLS
                self.catalog = TOOLS
            spec = self.catalog.get(args["name"])
            if spec is None:
                raise ValueError("未知模拟车辆函数；请先按分类ID用load_skill查看有效函数。")
            schema = spec.parameters if hasattr(spec, "parameters") else spec["parameters"]
            errors = list(Draft202012Validator(schema).iter_errors(args["arguments"]))
            if errors:
                raise ValueError("模拟动作参数无效：" + errors[0].message)
            suggestions.append(deepcopy(args))
            return {"ok": True, "status": "suggested_only", "executed": False,
                    "message": config_text("speech/thinker.toml", "suggestion_saved")}
        if name == "finish_task":
            urls = args.get("source_urls", list(sources)[:20])
            unknown = [url for url in urls if url not in sources]
            # Apply the same ledger constraint to prose/Markdown citations. A
            # correct structured list must not hide an invented URL in summary.
            inline_urls = [url.rstrip(".,;:，。；：！？!?）】》*`")
                           for url in find_urls(args["summary"] + "\n" + args.get("spoken_summary", ""))]
            unknown += [url for url in inline_urls if url not in sources]
            if unknown:
                raise ValueError("无法引用未经本任务真实工具查询的来源 URL。")
            used_sources = [sources[url] for url in urls]
            web_attempts = [item for item in observations if item["tool"] in ("search_web", "extract_web", "get_weather_forecast")]
            extracts = [item for item in observations if item["tool"] == "extract_web"]
            if any(source.get("kind") == "geocoding" for source in used_sources) and not any(
                    source.get("kind") == "forecast" for source in used_sources):
                raise ValueError("地理编码来源只能说明地点，不能单独支撑天气预报；请引用实际forecast来源，无法核实则如实说明。")
            if any(source.get("kind") not in {"forecast", "geocoding"} for source in used_sources) and not extracts:
                raise ValueError("已找到真实网页来源，请先extract_web读取核实；提取失败也要如实报告。")
            summary = args["summary"]
            evidence = "extracted" if used_sources and all(source.get("extracted") for source in used_sources) else "none"
            if used_sources and any(source.get("kind") == "forecast" for source in used_sources) and all(
                    source.get("kind") in {"forecast", "geocoding"} for source in used_sources):
                evidence = "forecast"
            elif used_sources and all(source.get("extracted") or source.get("verified") for source in used_sources):
                evidence = "verified" if any(source.get("kind") == "forecast" for source in used_sources) else "extracted"
            if used_sources and evidence == "none":
                evidence = "search_only"
                summary += config_text("speech/thinker.toml", "restrictions.search_only")
            elif not used_sources and web_attempts:
                summary += config_text("speech/thinker.toml", "restrictions.no_sources")
            # Produce the concise spoken form in the same finish call. The
            # shared guard only removes display syntax; legacy reports without
            # a generated spoken summary retain every statement and condition,
            # with no sentence/character cutoff or second rewrite model call.
            # Original reports and raw model/tool output remain in result/trace.
            spoken_summary = brief_result(args.get("spoken_summary") or args["summary"])
            if not spoken_summary:
                spoken_summary = brief_result(args["summary"])
            # Limits come from the evidence ledger, not from reading the prose;
            # a repeated caveat is cheaper than a dropped one.
            forecast = _forecast_sentence(list(adopted_forecasts), args.get("official_source_status"))
            if forecast:
                summary = forecast + "\n" + summary
                spoken_summary = forecast + spoken_summary
            if evidence == "search_only":
                spoken_summary += config_text("speech/thinker.toml", "spoken.search_only")
            elif not used_sources and web_attempts:
                spoken_summary += config_text("speech/thinker.toml", "spoken.no_sources")
            if suggestions:
                spoken_summary += config_text("speech/thinker.toml", "spoken.suggestions")
            queried = [source.get("queried_at") for source in used_sources if source.get("queried_at")]
            return {"ok": True, "result": {"summary": summary, "spoken_summary": spoken_summary,
                    "sources": used_sources,
                    "suggested_actions": deepcopy(suggestions), "query_times": sorted(set(queried)),
                    "web_evidence": evidence,
                    "forecast_records": [deepcopy(item["record"]) for item in observations
                        if item["tool"] == "get_weather_forecast" and item.get("ok") and item["record"]["source_url"] in urls],
                    "tool_observations": deepcopy(observations)}}
        raise ValueError("Unknown tool")

    @staticmethod
    def _collect_sources(data: dict[str, Any], sources: dict[str, Any], kind: str) -> None:
        # Dates come from the tool response, not model-generated arguments.
        queried_at = data.get("queried_at") or datetime.now(timezone.utc).isoformat()
        for row in data.get("results", []):
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                continue
            prior = sources.get(url, {})
            sources[url] = {"url": url, "title": row.get("title") or prior.get("title", ""),
                            "queried_at": queried_at, "kind": kind,
                            "extracted": kind == "extract" or prior.get("extracted", False)}

    async def aclose(self) -> None:
        for client in (self.ark, self.tavily, self.weather):
            close = getattr(client, "aclose", None)
            if close:
                await close()


async def _main() -> None:
    from .prompt_config import validate_all
    validate_all()
    from dotenv import load_dotenv
    load_dotenv()
    from .db import Database
    parser = argparse.ArgumentParser(description="Independent SQLite Thinker worker")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "data/super_xd.sqlite3"))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("THINKER_CONCURRENCY", "2")))
    args = parser.parse_args()
    db = Database(args.database)
    worker = ThinkerWorker(db, concurrency=args.concurrency,
                           max_steps=int(os.getenv("THINKER_MAX_STEPS", "20")),
                           timeout_seconds=float(os.getenv("THINKER_TIMEOUT_SECONDS", "60")))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    logger.info("Thinker started: concurrency=%s, max_steps=%s, timeout_seconds=%s",
                worker.concurrency, worker.max_steps, worker.timeout_seconds)
    try:
        await worker.run_forever()
    finally:
        await worker.aclose()
        close = getattr(db, "close", None)
        if close:
            close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
