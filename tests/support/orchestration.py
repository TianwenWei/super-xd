"""Shared explicit fixtures for orchestration tests."""
from tests.support.models import scripted_result

from copy import deepcopy

from datetime import datetime, timedelta, timezone

import json

import httpx


from super_xd.demo.devices.simulator import initial_vehicle_state

from super_xd.demo.devices.cockpit import initial_cockpit_state

from super_xd.nlp.contracts.tasks import TaskExecutionRequest

from super_xd.nlp.orchestration.gateway import NLPService

from super_xd.nlp.contracts.gateway_v1 import GatewayOutput, ResumeRequest, StartRequest

from super_xd.nlp.contracts.run import ToolObservation

from super_xd.nlp.policy.product import ProductTalkerPolicy

from super_xd.nlp.release import get_release_id


from super_xd.nlp.orchestration.vehicle_planner import VehicleControlPlanner


def call(name, args, identity="a"):
    return {
        "id": identity,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def message(name, args, identity="a"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [call(name, args, identity)],
    }


class StreamModel:
    def __init__(self, *messages):
        self.messages, self.inputs = list(messages), []

    async def stream(self, request):
        messages = request.messages
        self.inputs.append(deepcopy(messages))
        result = self.messages.pop(0)
        if result.get("content"):
            yield {"type": "delta", "text": result["content"]}
        yield {"type": "message", "message": {"role": "assistant", **result}}


class CompleteModel:
    def __init__(self, *messages):
        self.messages, self.inputs = list(messages), []

    async def complete(self, request):
        messages = request.messages
        self.inputs.append(deepcopy(messages))
        result = self.messages.pop(0)
        return scripted_result(await result() if callable(result) else deepcopy(result))


def request(text="打开后视镜加热然后折叠后视镜", tools=()):
    now = datetime.now(timezone.utc).isoformat()
    capabilities = []
    for name, arguments, operation, location in tools:
        capabilities.append(
            {
                "name": name,
                "action": arguments.get("action"),
                "content_kind": arguments.get("kind"),
                "operation": operation,
                "execution_location": location,
                "domain": "fixture",
            }
        )
    return StartRequest.model_validate(
        {
            "identity": {
                "run_id": "run",
                "request_id": "req",
                "turn_id": "turn",
                "context_revision": 1,
            },
            "conversation_id": "conversation",
            "received_at": now,
            "user_text": text,
            "speaker_seat": "driver",
            "persona_id": "professional",
            "context": {
                "revision": 1,
                "observed_at": now,
                "vehicle": {
                    "revision": 1,
                    "observed_at": now,
                    "state": initial_vehicle_state(),
                },
                "capabilities": {
                    "revision": 1,
                    "observed_at": now,
                    "tools": capabilities,
                    "features": {"gps": True, "real_navigation": True},
                },
                "environment": {"timezone": "Europe/Paris"},
                "cockpit": initial_cockpit_state(),
            },
        }
    )


def product_policy(input_):
    bindings = {}
    for capability in input_.context.capabilities.tools:
        arguments = {
            key: value
            for key, value in (
                ("action", capability.action),
                ("kind", capability.content_kind),
            )
            if value is not None
        }
        key = ProductTalkerPolicy.binding_key(
            {"name": capability.name, "arguments": arguments}
        )
        bindings[key] = {
            "kind": "tool_execution",
            "operation": capability.operation,
            "execution_location": capability.execution_location,
        }
    return ProductTalkerPolicy(
        bindings,
        enabled_nlp_targets=["vehicle_planner", "thinker_task", "task_management"],
    )


def kernel(model, input_, **options):
    return NLPService(
        model,
        model_policy_id="offline-policy",
        semantic_policy=product_policy(input_),
        **options,
    )


async def outputs(run):
    return [
        GatewayOutput.model_validate_json(item.model_dump_json())
        async for item in run.events()
    ]


def resume(service, event, result=None, *, status="completed", cancel=False):
    observations = (
        []
        if result is None
        else [
            ToolObservation(
                identity=event.identity,
                invocation_id=event.checkpoint.orchestration.groups[-1]
                .external_pending[0]
                .request.invocation_id,
                status=status,
                result=result,
            )
        ]
    )
    supplied = ResumeRequest(
        identity=event.identity,
        checkpoint=event.checkpoint,
        observations=observations,
        cancel=cancel,
    )
    return service.resume(ResumeRequest.model_validate_json(supplied.model_dump_json()))


def planner_submission(query):
    plan = {
        "status": "planned",
        "intents": [
            {
                "id": "intent",
                "source_text": query,
                "status": "planned",
                "action_ids": ["heat", "fold"],
                "keep_paths": [],
                "completed_operation_ids": [],
                "question": "",
                "reason": "",
            }
        ],
        "actions": [
            {
                "id": "heat",
                "intent_id": "intent",
                "name": "set_mirror_heating",
                "arguments": {"enabled": True},
                "depends_on": [],
            },
            {
                "id": "fold",
                "intent_id": "intent",
                "name": "set_mirror_fold",
                "arguments": {"folded": True},
                "depends_on": ["heat"],
            },
        ],
        "keep_paths": [],
        "question": "",
        "reason": "",
    }
    return message("submit_vehicle_plan", plan)


def planned_fixture():
    input_ = request(
        tools=[
            ("set_mirror_heating", {}, "control", "device"),
            ("set_mirror_fold", {}, "control", "device"),
        ]
    )
    talker = StreamModel(
        {
            "content": "这就处理。",
            "tool_calls": [
                call("delegate_vehicle_control", {"skill_ids": ["mirrors"]})
            ],
        }
    )
    planner_model = CompleteModel(planner_submission(input_.user_text))
    service = kernel(talker, input_, planner=VehicleControlPlanner(planner_model))
    return input_, talker, planner_model, service


class GatewayLedgerFixture:
    """An external HTTP ledger fixture, not an alternative production store."""

    def __init__(self):
        self.tasks, self.traces, self.requests, self.idempotency = {}, [], [], {}

    async def http(self, request):
        data = json.loads(request.content)
        self.requests.append((request.url.path, deepcopy(data)))
        if data.get("execution_id") and data.get("lease_token") != "valid-lease":
            return httpx.Response(409, json={"code": "lease_conflict"})
        if request.url.path.endswith("/traces"):
            self.traces.append(data["record"])
            return httpx.Response(200, json={"accepted": True})
        _, task_id, operation = request.url.path.rsplit("/", 2)
        task = self.tasks.get(task_id)
        if operation == "create":
            if task is None:
                task = {
                    "id": task_id,
                    "session_id": data["conversation_id"],
                    "goal": data["goal"],
                    "context": data["context"],
                    "revision": 1,
                    "status": "queued",
                    "deadline_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=60)
                    ).isoformat(),
                    "running_seconds": 0.0,
                    "running_since": None,
                    "constraints": [],
                    "steps": 0,
                    "checkpoint": {},
                    "sources": [],
                    "transcript": [],
                    "result": None,
                    "question": None,
                }
                self.tasks[task_id] = task
        elif operation == "mutate":
            if task["revision"] != data["expected_revision"]:
                return httpx.Response(409, json={"code": "revision_conflict"})
            task["revision"] += 1
            task["status"] = "cancelling" if data["action"] == "cancel" else "queued"
            task["constraints"].append(data["text"])
            task["question"] = None
        elif operation == "save":
            if task["revision"] != data["expected_revision"] or task["status"] in {
                "completed",
                "failed",
                "cancelled",
            }:
                return httpx.Response(200, json={"applied": False})
            task.update(deepcopy(data["fields"]))
            return httpx.Response(200, json={"applied": True})
        elif operation == "usage":
            return httpx.Response(
                200, json={"execution_seconds": 0.0, "lookup_count": 0}
            )
        elif operation == "notify-progress":
            return httpx.Response(200, json={"applied": False})
        elif operation == "expire":
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"task": deepcopy(task)})


class SearchFixture:
    async def search(self, query, **options):
        return {
            "results": [
                {
                    "url": "https://example.org/source",
                    "title": "实际测试资料",
                    "content": "检索证据",
                }
            ]
        }

    async def extract(self, urls):
        return {
            "results": [{"url": url, "raw_content": "真实测试正文"} for url in urls]
        }


def execution(task, **changes):
    values = {
        "conversation_id": "conversation",
        "task_id": task["id"],
        "revision": task["revision"],
        "execution_id": "execution",
        "lease_token": "valid-lease",
        "release_id": get_release_id(),
        "model_policy_id": "offline-policy",
    }
    values.update(changes)
    return TaskExecutionRequest.model_validate_json(json.dumps(values))
