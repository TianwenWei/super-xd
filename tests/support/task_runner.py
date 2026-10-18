"""Explicit shared task_runner test fixtures."""
from tests.support.models import scripted_result

import asyncio

from copy import deepcopy

from datetime import datetime, timedelta, timezone

import json


from super_xd.nlp.orchestration.tasks.runner import TaskRunner

from super_xd.nlp.orchestration.tasks.ports import running_elapsed


def message(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class MemoryTaskStore:
    """One fixture implements both ports over a shared caller-owned event ledger."""

    def __init__(self, *, context=None):
        now = datetime.now(timezone.utc)
        self.task = {
            "id": "task",
            "session_id": "context",
            "goal": "核实公开资料",
            "revision": 1,
            "status": "running",
            "constraints": [],
            "context": context or {},
            "steps": 0,
            "checkpoint": {},
            "running_seconds": 0,
            "running_since": now.isoformat(),
            "deadline_at": (now + timedelta(seconds=60)).isoformat(),
            "sources": [],
            "transcript": [],
            "result": None,
        }
        self.events = []

    async def read(self, task_id):
        await asyncio.sleep(0)
        return deepcopy(self.task) if task_id == self.task["id"] else None

    async def refresh(self, task_id):
        return await self.read(task_id)

    async def save(self, task_id, revision, **fields):
        await asyncio.sleep(0)
        if task_id != self.task["id"] or revision != self.task["revision"]:
            return False
        if self.task["status"] in {"completed", "failed", "cancelled"}:
            return False
        if self.task["status"] == "cancelling" and fields.get("status") != "cancelled":
            return False
        status = fields.get("status", self.task["status"])
        if status != self.task["status"]:
            self.task["running_seconds"] = running_elapsed(self.task)
            self.task["running_since"] = (
                datetime.now(timezone.utc).isoformat() if status == "running" else None
            )
        self.task.update(deepcopy(fields))
        return True

    async def expire(self, task_id, max_age_seconds):
        task = await self.read(task_id)
        if task and (
            running_elapsed(task) >= max_age_seconds
            or datetime.fromisoformat(task["deadline_at"]) <= datetime.now(timezone.utc)
        ):
            await self.save(
                task_id, task["revision"], status="failed", error_code="deadline"
            )

    async def notify_progress(self, task_id):
        return False

    async def usage(self, task_id, session_id):
        rows = [
            row
            for row in self.events
            if row.get("task_id") == task_id and row["session_id"] == session_id
        ]
        elapsed = sum(
            row["data"].get("elapsed_seconds", 0)
            for row in rows
            if row["kind"] == "task.execution.finished"
            and row["data"].get("query_mode") == "quick"
        )
        lookups = sum(
            row["kind"] == "tool.started" and row["data"].get("name") == "lookup_web"
            for row in rows
        )
        return elapsed, lookups

    async def emit(self, session_id, kind, data, **fields):
        await asyncio.sleep(0)
        event = deepcopy(
            {"session_id": session_id, "kind": kind, "data": data, **fields}
        )
        self.events.append(event)
        return event


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    async def complete(self, request):
        messages = request.messages
        self.messages.append(deepcopy(messages))
        response = self.responses.pop(0)
        return scripted_result(await response(messages) if callable(response) else response)


class MemorySearch:
    def __init__(self):
        self.calls = []

    async def search(self, query, max_results=5, **options):
        self.calls.append(("search", query))
        return {
            "results": [
                {
                    "url": "https://example.org/source",
                    "title": "来源",
                    "content": "线索",
                }
            ]
        }

    async def extract(self, urls):
        self.calls.append(("extract", urls))
        return {
            "results": [{"url": url, "raw_content": "已实际读取的资料"} for url in urls]
        }


def runner(store, model, search=None, **options):
    return TaskRunner(
        store, model, search or MemorySearch(), trace_sink=store, **options
    )
