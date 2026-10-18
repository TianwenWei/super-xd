"""Storage-independent Thinker reasoning, validation and checkpoint continuation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable
from typing_extensions import Unpack
from super_xd.nlp.contracts.providers import ModelRequest
from super_xd.nlp.contracts.task_state import TaskState, TaskPatch, TaskCheckpoint, TASK_PATCH
from super_xd.nlp.policy.research import (weather_task, verified_forecast_records, required_source_domains, source_in_domains, weather_finish_guard)
from super_xd.nlp.orchestration.tasks.tools import ResearchTools
from super_xd.nlp.orchestration.tasks.ports import TaskStore, TaskTraceSink, running_elapsed
from super_xd.nlp.release import get_release_id
from super_xd.nlp.engine.models import complete_model
from super_xd.nlp.engine.evidence import RETRIEVAL_TOOLS, RetrievalLimits, RetrievalProgress, allow_retrieval, annotate_evidence, record_retrieval, retrieval_feedback
from uuid import uuid4

from jsonschema import Draft202012Validator

from super_xd.nlp.prompts.templates import render_prompt
from super_xd.nlp.prompts.config import config_text
from super_xd.nlp.textutil import redact
from super_xd.nlp.orchestration.tasks.feedback import PHASES, partial_result, task_user_detail
from super_xd.nlp.prompts.research import THINKER_TOOLS, QUICK_TOOLS, research_messages, query_mode, apply_delivery_mode
from super_xd.nlp.catalog.research import TOOL_SCHEMAS

class RevisionChanged(Exception):
    def __init__(self, task: TaskState):
        self.task = task


class TaskCancelled(Exception):
    pass


def _safe_error(exc: BaseException, max_length: int | None = 500) -> str:
    """Never let provider diagnostics disclose credentials in cards or logs."""
    value = redact(str(exc))
    return f"{type(exc).__name__}: {value if max_length is None else value[:max_length]}"


class TaskRunner:
    """Bounded Thinker execution with caller-owned state, tools and trace transport.

    This class never claims work or creates service clients. A task snapshot must
    already belong to the caller's execution scope; revisions and checkpoints
    are committed through the injected store.
    """

    def __init__(self, store: TaskStore, ark: Any, tavily: Any, *,
                 trace_sink: TaskTraceSink, max_steps: int = 20,
                 timeout_seconds: float = 60, close_clients: bool = False,
                 catalog: dict[str, Any] | None = None,
                 tool_timeout_seconds: float = 35,
                 quick_max_steps: int = 4, quick_timeout_seconds: float = 60,
                 weather: Any = None,
                 forecast_verifier: Callable[[dict, dict], bool] | None = None):
        self.store, self.trace_sink = store, trace_sink
        self.ark, self.tavily, self.weather = ark, tavily, weather
        self.close_clients = close_clients
        self.forecast_verifier = forecast_verifier
        self.max_steps = max(1, min(20, max_steps))
        self.timeout_seconds = max(0.01, min(60, timeout_seconds))
        self.quick_max_steps = max(1, min(self.max_steps, 4, quick_max_steps))
        self.quick_timeout_seconds = max(0.01, min(60, quick_timeout_seconds))
        self.tool_timeout_seconds = max(0.01, tool_timeout_seconds)
        self.tools = ResearchTools(tavily, weather, forecast_verifier=forecast_verifier, catalog=catalog)

    async def _boundary(self, task_id: str, revision: int | None = None) -> dict[str, Any]:
        task = await self.store.refresh(task_id)
        if not task:
            raise RuntimeError("Task no longer exists")
        if task["status"] in ("cancelling", "cancelled"):
            await self.store.save(task_id, task["revision"], status="cancelled", summary=config_text("speech/thinker.toml", "cancelled"), question=None)
            raise TaskCancelled()
        if task["status"] in ("completed", "failed", "waiting_clarification"):
            # A caller may replay a task after it finished or suspended for input.
            # Only an accepted reply/update can make clarification runnable again.
            raise TaskCancelled()
        if task['status'] != 'waiting_clarification' and datetime.fromisoformat(task['deadline_at']) <= datetime.now(timezone.utc):
            await self.store.expire(task_id, self.timeout_seconds)
            raise TaskCancelled()
        if revision is not None and task["revision"] != revision:
            raise RevisionChanged(task)
        return task

    async def _save(self, task_id: str, revision: int, **fields: Unpack[TaskPatch]) -> None:
        fields = TASK_PATCH.validate_python(fields)
        if not await self.store.save(task_id, revision, **fields):
            raise RevisionChanged(await self._boundary(task_id))

    @staticmethod
    def _messages(task: TaskState, *, current_utc: str | None = None):
        return research_messages(task, current_utc=current_utc)

    _query_mode = staticmethod(query_mode)

    async def _quick_usage(self, task: TaskState) -> tuple[float, int]:
        return await self.store.usage(task["id"], task["session_id"])

    async def _trace(self, task: TaskState, kind: str, data: dict[str, Any], *,
               revision: int | None = None, step: int | None = None,
               call_id: str | None = None, status: str | None = None,
               model_call_id: str | None = None) -> dict[str, Any]:
        # The sink owns transport and persistence; payloads contain no client configuration.
        return await self.trace_sink.emit(task["session_id"], kind,
            {"agent": "thinker", "model_call_index": step, "model_call_id": model_call_id,
             "observed_at": datetime.now(timezone.utc).isoformat(), **data, "release_id": get_release_id()},
            task_id=task["id"], revision=revision if revision is not None else task["revision"],
            turn_id=(task.get("context") or {}).get("origin_turn_id") or
                    (task.get("context") or {}).get("turn_id"), step=step,
            call_id=call_id, status=status)

    async def _response_status(self, task_id: str, revision: int, default: str) -> str:
        latest = await self.store.read(task_id)
        if not latest or latest['revision'] != revision or latest['status'] in {
                'cancelling', 'cancelled', 'completed', 'failed'}:
            return 'stale'
        return default

    @staticmethod
    def _has_current_attempt(observations: list[dict[str, Any]]) -> bool:
        return any(item.get("attempted") is True and item.get("tool") in {
            "search_web", "extract_web", "get_weather_forecast"} for item in observations)

    async def run_task(self, task: TaskState) -> None:
        current = await self.store.refresh(task['id'])
        if not current:
            raise RuntimeError('Task no longer exists')
        if current['status'] == 'waiting_clarification':
            return
        # Replayed snapshots may predate a reply that extended the paused deadline.
        # All budgets and failure ownership must use authoritative state.
        task = current
        started = time.perf_counter()
        mode = self._query_mode(task)
        deadline_reason = "running_budget"
        progress_timer = None
        try:
            # The persisted wall deadline includes queue time and survives a restart.
            remaining = min(self.timeout_seconds - running_elapsed(task),
                            (datetime.fromisoformat(task['deadline_at']) - datetime.now(timezone.utc)).total_seconds())
            if remaining <= 0:
                raise asyncio.TimeoutError()
            if mode == "quick":
                spent, _ = await self._quick_usage(task)
                execution_remaining = self.quick_timeout_seconds - spent
                if execution_remaining < remaining:
                    remaining, deadline_reason = execution_remaining, "quick_execution_budget"
                if remaining <= 0:
                    raise asyncio.TimeoutError()
            end = time.perf_counter() + remaining
            async def notify_progress():
                delay = (datetime.fromisoformat(task['deadline_at']) - datetime.now(timezone.utc)).total_seconds() - 30
                await asyncio.sleep(max(0, delay))
                await self.store.notify_progress(task['id'])
            progress_timer = asyncio.create_task(notify_progress())
            # Reserve delivery time instead of letting one more search consume the minute.
            budget = min(self.timeout_seconds, self.quick_timeout_seconds) if mode == 'quick' else self.timeout_seconds
            reserve = min(15, budget / 4, remaining)
            try:
                await asyncio.wait_for(self._run_task(task), remaining - reserve)
            except asyncio.TimeoutError:
                current = await self._boundary(task['id'])
                await self._trace(current, 'task.finalizing', {'remaining_seconds': max(0, end - time.perf_counter())}, status='running')
                try:
                    await asyncio.wait_for(self._finalize(current), max(.001, end - time.perf_counter()))
                except (TaskCancelled, RevisionChanged):
                    raise
                except asyncio.TimeoutError:
                    raise
                except Exception as exc:
                    await self._trace(current, 'task.finalize_error', {'error': _safe_error(exc, None)}, status='error')
                    await self._fail_revision(task['id'], current['revision'], _safe_error(exc), code='finalization_failed')
        except RevisionChanged as changed:
            await self._trace(task, 'revision.discarded', {'new_revision': changed.task['revision'], 'phase': 'finalize'}, status='stale')
            await self._fail_current(task['id'], '条件在收尾期间更新，未能在时间上限内核实。', code='deadline')
        except TaskCancelled:
            return
        except asyncio.TimeoutError:
            current = await self.store.read(task["id"]) or task
            await self._trace(current, "task.timeout", {"reason": deadline_reason, "query_mode": mode},
                        step=current.get("steps"), status="failed")
            error = (f"快速查询累计执行超过{self.quick_timeout_seconds:g}秒时间上限，已停止；没有宣称完成。"
                     if deadline_reason == "quick_execution_budget" else "任务超过时间上限，已停止；没有宣称完成。")
            await self._fail_current(task["id"], error, code='deadline')
        except asyncio.CancelledError:
            # The caller owns cancellation and can later resume the saved checkpoint.
            await self._trace(task, "worker.interrupted", {"reason": "worker_coroutine_cancelled"},
                        step=task.get("steps"), status="cancelled")
            raise
        except Exception as exc:
            # Unexpected setup errors still belong to the claimed revision. An
            # old failure must never terminate subsequently updated conditions.
            await self._trace(task, "step.error", {"error": _safe_error(exc, None), "phase": "setup"},
                        step=task.get("steps"), status="error")
            await self._fail_revision(task["id"], task["revision"], _safe_error(exc))
        finally:
            if progress_timer:
                progress_timer.cancel()
                await asyncio.gather(progress_timer, return_exceptions=True)
            if mode == "quick":
                current = await self.store.read(task["id"]) or task
                await self._trace(current, "task.execution.finished", {"query_mode": mode,
                    "elapsed_seconds": time.perf_counter() - started},
                    step=current.get("steps"), status=current["status"])

    async def _fail_revision(self, task_id: str, revision: int, error: str, code='execution_failed') -> bool:
        """Return False if the failing work became stale and should be retried."""
        try:
            current = await self._boundary(task_id, revision)
            closed = {**current, 'status': 'failed', 'error_code': code}
            return await self.store.save(task_id, revision, status="failed", error=error, error_code=code,
                                     summary=task_user_detail(closed), result=partial_result(closed), question=None)
        except RevisionChanged:
            return False
        except TaskCancelled:
            return True

    async def _fail_current(self, task_id: str, error: str, code='execution_failed') -> None:
        try:
            current = await self._boundary(task_id)
            await self._fail_revision(task_id, current['revision'], error, code=code)
        except TaskCancelled:
            pass

    async def _finalize(self, task):
        """One bounded delivery call, with no permission to perform more IO tools."""
        revision, task_id = task['revision'], task['id']
        checkpoint = task.get('checkpoint') or {}
        if not checkpoint:
            await self._fail_current(task_id, '任务未能在时间上限内取得资料。', code='deadline')
            return
        self._validate_checkpoint(checkpoint)
        messages = deepcopy(checkpoint.get('messages') or self._messages(task))
        apply_delivery_mode(messages, task)
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
        request = ModelRequest(deepcopy(messages), tools=tools, role='finalizer', reasoning=False,
                   max_tokens=2500, tool_choice={'type': 'function', 'function': {'name': 'finish_task'}})
        await self._save(task_id, revision, progress={'phase': 'finish_task', 'read_sources': sum(bool(s.get('extracted')) for s in sources.values())},
                   summary=PHASES['finish_task'])
        await self._trace(task, 'model.started', {'request': asdict(request), 'phase': 'finalize'}, call_id=call_id, model_call_id=call_id, status='running')
        try:
            completed = await complete_model(self.ark, request)
        except BaseException as exc:
            await self._trace(task, 'model.error', {'error': _safe_error(exc, None), 'phase': 'finalize',
                        'partial': deepcopy(getattr(exc, 'trace_partial', None))},
                        call_id=call_id, model_call_id=call_id, status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'error')
            raise
        response, meta = completed.message, completed.metadata
        await self._trace(task, 'model.completed', {'message': response, 'meta': meta, 'phase': 'finalize',
                                                 'raw_return': completed.raw_return},
                    call_id=call_id, model_call_id=call_id, status=await self._response_status(task_id, revision, 'received'))
        await self._boundary(task_id, revision)
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
        adopted = weather_finish_guard(args, task, sources, observations, verifier=self.forecast_verifier)
        await self._trace(task, 'tool.started', {'name': 'finish_task', 'arguments': raw_args, 'resolved_arguments': args, 'call': call},
                    call_id=call['id'], model_call_id=call_id, status='running')
        result = await self._execute_tool('finish_task', args, sources, suggestions, observations, adopted_forecasts=adopted)
        if selected:
            result['result']['summary'] += config_text("speech/thinker.toml", "source_heading") + '\n'.join(
                config_text("speech/thinker.toml", "source_item",
                            title=str(source.get('title') or source['url']), url=str(source['url'])) for source in selected)
        await self._trace(task, 'tool.completed', {'name': 'finish_task', 'arguments': args, 'result': result},
                    call_id=call['id'], model_call_id=call_id, status=await self._response_status(task_id, revision, 'completed'))
        await self._boundary(task_id, revision)
        result['result'].update(partial=True, query_mode=self._query_mode(task))
        messages.extend([response, {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)}])
        await self._save(task_id, revision, status='completed', result=result['result'], summary=result['result']['summary'],
                   transcript=messages, sources=list(sources.values()), question=None)

    async def _run_task(self, task: TaskState) -> None:
        task_id = task["id"]
        current = await self._boundary(task_id)
        revision = -1
        messages: list[dict[str, Any]] = []
        sources: dict[str, dict[str, Any]] = {}
        suggestions: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        retrieval = RetrievalProgress()
        tool_count = 0
        _, lookup_count = await self._quick_usage(current) if self._query_mode(current) == "quick" else (0, 0)
        for step in range(int(current.get("steps", 0)) + 1, self.max_steps + 1):
            phase = "boundary"
            model_call_id = None
            try:
                current = await self._boundary(task_id)
                mode = self._query_mode(current)
                step_limit = self.quick_max_steps if mode == "quick" else self.max_steps
                if step > step_limit:
                    await self._fail_current(task_id, "任务达到推理步骤上限，已停止；没有宣称完成。", code='step_budget')
                    return
                tools = QUICK_TOOLS if mode == "quick" else THINKER_TOOLS
                role = 'thinker' if mode != 'quick' else 'talker'
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
                    if checkpoint:
                        self._validate_checkpoint(checkpoint)
                        retrieval = RetrievalProgress.model_validate(checkpoint.get('retrieval', {}))
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
                    await self._trace(current, "revision.started", {"previous_revision": previous_revision,
                                "checkpoint_restored": restored,
                                "source_count": len(sources)}, revision=revision, step=step, status="running")
                apply_delivery_mode(messages, current)
                if (mode == "quick" and step < step_limit
                        and (current.get("context") or {}).get("query_domain") == "weather"):
                    forecast_attempted = any(item.get("tool") == "get_weather_forecast"
                                             and item.get("attempted") is True for item in observations)
                    records = verified_forecast_records(observations, self.forecast_verifier)
                    domains = required_source_domains(current)
                    if not forecast_attempted:
                        available = {"get_weather_forecast", "ask_clarification"}
                    elif not records:
                        available = {"finish_task", "ask_clarification"}
                    elif domains and lookup_count < 2 and not all(any(
                            source.get("extracted") is True and source_in_domains(source.get("url", ""), [domain])
                            for source in sources.values()) for domain in domains):
                        available = {"lookup_web", "ask_clarification"}
                    else:
                        available = None
                    if available is not None:
                        tools = [tool for tool in QUICK_TOOLS if tool["function"]["name"] in available]
                if mode == "quick" and lookup_count >= 2 and not sources and not self._has_current_attempt(observations) and (
                        not weather_task(current) or step == step_limit):
                    error = config_text("speech/thinker.toml", "quick_budget_exhausted")
                    await self._trace(current, "task.budget_exhausted", {"reason": "quick_lookup_budget",
                        "lookup_limit": 2, "current_revision_attempts": 0}, revision=revision,
                        step=current.get("steps"), status="failed")
                    await self._save(task_id, revision, status="failed", error=error, error_code='step_budget', summary=error,
                               result=None, sources=[], transcript=messages, question=None)
                    return
                finalizing = step == step_limit or tool_count >= step_limit
                if finalizing:
                    tools = [tool for tool in THINKER_TOOLS if tool['function']['name'] == 'finish_task']
                    role = 'finalizer'
                    options['reasoning'] = False
                    options['max_tokens'] = 2500
                    options['tool_choice'] = {'type': 'function', 'function': {'name': 'finish_task'}}
                    messages.append({'role': 'user', 'content': render_prompt('thinker_finalize')})
                await self._save(task_id, revision, status="running", steps=step, error=None,
                           summary=PHASES['finish_task' if finalizing else 'model'], question=None,
                           progress={'phase': 'finish_task' if finalizing else 'model',
                                     'read_sources': sum(bool(s.get('extracted')) for s in sources.values())},
                           checkpoint=self._checkpoint(current, revision, messages, sources, suggestions, observations, tool_count, retrieval))
                await self._trace(current, "step.started", {}, revision=revision, step=step, status="running")
                phase = "model"
                model_call_id = uuid4().hex
                request = ModelRequest(deepcopy(messages), tools=tools, role=role, **options)
                await self._trace(current, "model.started", {"request": asdict(request), "query_mode": mode},
                    revision=revision, step=step, call_id=model_call_id,
                    model_call_id=model_call_id, status="running")
                try:
                    completed = await complete_model(self.ark, request)
                except asyncio.CancelledError as exc:
                    await self._trace(current, "model.error", {"error": _safe_error(exc, None),
                                "partial": getattr(exc, 'trace_partial', None)}, revision=revision,
                                step=step, call_id=model_call_id, model_call_id=model_call_id, status="cancelled")
                    raise
                except Exception as exc:
                    await self._trace(current, "model.error", {"error": _safe_error(exc, None),
                                "partial": getattr(exc, 'trace_partial', None)}, revision=revision,
                                step=step, call_id=model_call_id, model_call_id=model_call_id,
                                status=await self._response_status(task_id, revision, 'error'))
                    raise
                # Preserve the full response before checking whether it became
                # stale. Provider metadata is diagnostic data, never a message.
                response, meta = completed.message, completed.metadata
                await self._trace(current, "model.completed", {"message": response, "meta": meta,
                                                              "raw_return": completed.raw_return},
                            revision=revision, step=step, call_id=model_call_id,
                            model_call_id=model_call_id, status=await self._response_status(task_id, revision, 'received'))
                await self._boundary(task_id, revision)
                message = deepcopy(response)
                message["role"] = "assistant"
                calls = response.get("tool_calls") or []
                if calls:
                    message["tool_calls"] = calls
                messages.append(message)
                if not calls:
                    # Ask once for a structured terminal result instead of silently
                    # treating unsupported model text as successful research.
                    messages.append({"role": "user", "content": render_prompt("thinker_retry")})
                    await self._save(task_id, revision, transcript=messages)
                    continue
                if not isinstance(calls, list) or len(calls) > 8:
                    raise RuntimeError("Thinker returned too many or malformed tool calls")
                terminal: tuple[str, dict[str, Any]] | None = None
                for index, call in enumerate(calls):
                    phase = "tool"
                    await self._boundary(task_id, revision)
                    function = call.get("function", {})
                    name = function.get("name", "")
                    call_id = call.get("id", f"step{step}_{index}")
                    arguments = function.get("arguments", "{}")
                    actual_attempt = False
                    await self._trace(current, "tool.started", {"name": name, "call": call,
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
                            await self._save(task_id, revision, summary=self._progress(name, arguments),
                                       progress={'phase': name, 'read_sources': sum(bool(s.get('extracted')) for s in sources.values())})
                            limits = RetrievalLimits(max_calls_per_tool=step_limit, max_total_calls=step_limit)
                            if name in RETRIEVAL_TOOLS and not allow_retrieval(retrieval, name, limits):
                                result = retrieval_feedback(retrieval, name)
                                observations.append({'tool': name, 'ok': False, 'attempted': False,
                                                     'code': result['code']})
                            else:
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
                                    adopted = (weather_finish_guard(arguments, current, sources, observations, verifier=self.forecast_verifier)
                                               if name == "finish_task" else [])
                                    operation = self._execute_tool(name, arguments, sources, suggestions, observations,
                                                                   source_domains=required_source_domains(current),
                                                                   adopted_forecasts=adopted)
                                actual_attempt = True
                                result = await asyncio.wait_for(
                                    operation,
                                    timeout=self.tool_timeout_seconds)
                            if name == "finish_task" and result.get("ok"):
                                result["result"]["query_mode"] = mode
                                if finalizing:
                                    result['result']['partial'] = True
                            await self._trace(current, "tool.completed", {"name": name,
                                        "arguments": arguments, "result": result}, revision=revision,
                                        step=step, call_id=call_id, model_call_id=model_call_id,
                                        status=await self._response_status(task_id, revision, "completed" if result.get("ok") else "rejected"))
                            await self._boundary(task_id, revision)
                            if name == "ask_clarification" and result.get("ok"):
                                terminal = ("waiting_clarification", {"question": arguments["question"],
                                            "summary": arguments["question"]})
                            elif name == "finish_task" and result.get("ok"):
                                terminal = ("completed", {"result": result["result"],
                                            "summary": result["result"]["summary"], "question": None})
                        if terminal and result.get("ok") is False:
                            await self._trace(current, "tool.completed", {"name": name,
                                        "arguments": arguments, "result": result}, revision=revision,
                                        step=step, call_id=call_id, model_call_id=model_call_id, status="skipped")
                    except (RevisionChanged, TaskCancelled):
                        raise
                    except asyncio.CancelledError as exc:
                        await self._trace(current, "tool.error", {"name": name, "arguments": arguments,
                                    "error": _safe_error(exc, None)}, revision=revision,
                                    step=step, call_id=call_id, model_call_id=model_call_id, status="cancelled")
                        raise
                    except Exception as exc:
                        result = {"ok": False, "error": _safe_error(exc)}
                        if name == "finish_task":
                            result["citable_source_urls"] = list(sources)
                            result["forecast_records"] = deepcopy(verified_forecast_records(observations, self.forecast_verifier))
                        await self._trace(current, "tool.error", {"name": name, "arguments": arguments,
                                    "error": _safe_error(exc, None), "result": result}, revision=revision,
                                    step=step, call_id=call_id, model_call_id=model_call_id,
                                    status=await self._response_status(task_id, revision, 'error'))
                        observations.append({"tool": name, "ok": False, "attempted": actual_attempt,
                                             "error": result["error"]})
                    model_result = deepcopy(result)
                    if name in RETRIEVAL_TOOLS and actual_attempt:
                        retrieval, report = record_retrieval(retrieval,
                            invocation_id=f'{task_id}:{revision}:{model_call_id}:{call_id}',
                            tool_name=name, result=result)
                        model_result = annotate_evidence(result, name)
                        model_result['retrieval_progress'] = report.model_dump()
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": json.dumps(model_result, ensure_ascii=False)})
                    # Persist each completed tool before the next await, so timed
                    # delivery can reuse real evidence even in a cancelled batch.
                    await self._save(task_id, revision, sources=list(sources.values()),
                               checkpoint=self._checkpoint(current, revision, messages, sources, suggestions, observations, tool_count, retrieval))
                await self._save(task_id, revision, transcript=messages, sources=list(sources.values()),
                    checkpoint=self._checkpoint(current, revision, messages, sources, suggestions, observations, tool_count, retrieval))
                if terminal:
                    status, fields = terminal
                    await self._boundary(task_id, revision)
                    await self._save(task_id, revision, status=status, transcript=messages,
                               sources=list(sources.values()), **fields)
                    return
            except RevisionChanged as changed:
                await self._trace(current, "revision.discarded", {"new_revision": changed.task["revision"],
                            "phase": phase, "reason": "updated_during_step"}, revision=revision,
                            step=step, call_id=model_call_id, model_call_id=model_call_id, status="stale")
                current = changed.task
                # Next iteration reconstructs the prompt. The already spent time
                # and steps still count towards the same bounded task execution.
                continue
            except TaskCancelled:
                latest = await self.store.read(task_id) or current
                await self._trace(current, "step.stopped", {"phase": phase,
                            "latest_revision": latest["revision"], "latest_status": latest["status"]},
                            revision=revision, step=step, call_id=model_call_id,
                            model_call_id=model_call_id, status="cancelled")
                raise
            except Exception as exc:
                await self._trace(current, "step.error", {"phase": phase, "error": _safe_error(exc, None)},
                            revision=revision, step=step, call_id=model_call_id, model_call_id=model_call_id,
                            status=await self._response_status(task_id, revision, 'error'))
                if await self._fail_revision(task_id, revision, _safe_error(exc)):
                    return
                latest = await self.store.read(task_id) or current
                await self._trace(current, "revision.discarded", {"new_revision": latest["revision"],
                            "phase": phase, "reason": "error_from_old_revision", "error": _safe_error(exc, None)},
                            revision=revision, step=step, call_id=model_call_id,
                            model_call_id=model_call_id, status="stale")
                # A model/network failure from an old revision is discarded just
                # like a late successful response. Continue with new conditions.
                continue
        await self._fail_current(task_id, "任务达到推理步骤上限，已停止；没有宣称完成。", code='step_budget')

    @staticmethod
    def _validate_checkpoint(checkpoint):
        if checkpoint.get('checkpoint_version') != 1 or checkpoint.get('release_id') != get_release_id():
            raise ValueError('Task checkpoint release/version mismatch; explicit migration or a new task is required')
        TaskCheckpoint.model_validate(checkpoint)

    @staticmethod
    def _checkpoint(task, revision, messages, sources, suggestions, observations, tool_count, retrieval=None):
        return TaskCheckpoint.model_validate({'checkpoint_version': 1, 'release_id': get_release_id(),
                'goal': task['goal'], 'revision': revision, 'messages': messages,
                'sources': list(sources.values()), 'suggestions': suggestions,
                'observations': observations, 'tool_count': tool_count,
                'retrieval': retrieval.model_dump() if retrieval is not None else (task.get('checkpoint') or {}).get('retrieval', {}),
                'constraint_count': len(task.get('constraints', []))}).model_dump()

    async def _quick_lookup(self, args, sources, suggestions, observations, task, revision,
                            step, model_call_id, parent_call_id):
        async def invoke(name, arguments):
            await self._boundary(task["id"], revision)
            call_id = uuid4().hex
            await self._trace(task, "tool.started", {"name": name, "arguments": arguments,
                "parent_call_id": parent_call_id}, revision=revision, step=step, call_id=call_id,
                model_call_id=model_call_id, status="running")
            try:
                result = await self._execute_tool(name, arguments, sources, suggestions, observations,
                                                  source_domains=required_source_domains(task))
            except (RevisionChanged, TaskCancelled):
                raise
            except BaseException as exc:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else await self._response_status(task["id"], revision, "error")
                await self._trace(task, "tool.error", {"name": name, "arguments": arguments,
                    "error": _safe_error(exc, None), "parent_call_id": parent_call_id},
                    revision=revision, step=step, call_id=call_id, model_call_id=model_call_id, status=status)
                observations.append({"tool": name, "ok": False, "attempted": True, "error": _safe_error(exc)})
                raise
            await self._trace(task, "tool.completed", {"name": name, "arguments": arguments,
                "result": result, "parent_call_id": parent_call_id}, revision=revision, step=step,
                call_id=call_id, model_call_id=model_call_id,
                status=await self._response_status(task["id"], revision, "completed"))
            await self._boundary(task["id"], revision)
            return result

        search = await invoke("search_web", {"query": args["query"], "max_results": args.get("max_results", 3)})
        urls = []
        domains = required_source_domains(task)
        for row in search.get("results", []):
            url = row.get("url") if isinstance(row, dict) else None
            if domains and (not isinstance(url, str) or not source_in_domains(url, domains)):
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

    async def _execute_tool(self, name, *args, **kwargs):
        return await self.tools.execute(name, *args, **kwargs)
    async def aclose(self) -> None:
        if not self.close_clients:
            return
        for client in (self.ark, self.tavily, self.weather):
            close = getattr(client, "aclose", None)
            if close:
                await close()
