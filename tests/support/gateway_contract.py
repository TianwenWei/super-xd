"""Shared explicit fixtures for gateway contract tests."""

from copy import deepcopy

import json


from super_xd.nlp.contracts.run import ToolObservation

from super_xd.nlp.orchestration.gateway import NLPService

from super_xd.nlp.contracts.gateway_v1 import (
    GatewayOutput,
    ResumeRequest,
    StartRequest,
    ToolDispatch,
)

from super_xd.nlp.policy.product import ProductTalkerPolicy


from super_xd.demo.devices.simulator import execute_tool


STAMP = "2026-10-17T08:00:00+00:00"


def start_request(**changes):
    value = {
        "identity": {
            "run_id": "run-1",
            "request_id": "request-1",
            "turn_id": "turn-1",
            "context_revision": 7,
        },
        "received_at": STAMP,
        "user_text": "把音量调到 40，再查一下巴黎天气",
        "speaker_seat": "driver",
        "persona_id": "professional",
        "context": {
            "revision": 7,
            "observed_at": STAMP,
            "vehicle": {
                "revision": 5,
                "observed_at": STAMP,
                "state": {"_meta": {"speed_kph": 0}, "media": {"volume": 30}},
            },
            "capabilities": {
                "revision": 2,
                "observed_at": STAMP,
                "tools": [
                    {
                        "name": "set_volume",
                        "operation": "control",
                        "execution_location": "device",
                        "domain": "media",
                    },
                    {
                        "name": "get_weather",
                        "operation": "query",
                        "execution_location": "cloud",
                        "domain": "weather",
                    },
                ],
            },
            "environment": {
                "timezone": "Europe/Paris",
                "location": {
                    "name": "巴黎",
                    "country": "法国",
                    "source": "user_setting",
                },
            },
        },
    }
    value.update(changes)
    return StartRequest.model_validate(value)


def binding(operation="control", location="device"):
    return ToolDispatch(
        kind="tool_execution", operation=operation, execution_location=location
    )


def policy(**extra):
    return ProductTalkerPolicy(
        {"set_volume": binding(), "get_weather": binding("query", "cloud"), **extra},
        enabled_nlp_targets=["vehicle_planner", "thinker_task", "task_management"],
    )


class ScriptedModel:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
        self.requests.append({"messages": messages, "tools": tools, "options": options})
        response = self.responses.pop(0)
        if response.get("content"):
            yield {"type": "delta", "text": response["content"]}
        yield {
            "type": "message",
            "message": {"role": "assistant", **response},
            "meta": {"offline": True},
        }


def service(model, **changes):
    return NLPService(
        model,
        semantic_policy=changes.pop("semantic_policy", policy()),
        model_policy_id=changes.pop("model_policy_id", "offline-model-v1"),
        **changes,
    )


def call(name="set_volume", arguments=None, identity="a"):
    return {
        "id": identity,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments if arguments is not None else {"volume": 40}
            ),
        },
    }


async def collect(run):
    return [
        GatewayOutput.model_validate_json(event.model_dump_json())
        async for event in run.events()
    ]


def resume_request(checkpoint, observations=(), **changes):
    value = ResumeRequest(
        identity=checkpoint.start.identity,
        checkpoint=checkpoint,
        observations=list(observations),
        **changes,
    )
    return ResumeRequest.model_validate_json(value.model_dump_json())


def observation(checkpoint, index=0, status="completed", **result):
    if not result:
        request = checkpoint.run.pending[index]
        vehicle = deepcopy(checkpoint.start.context.vehicle.state)
        if request.name == "set_volume":
            execute_tool(request.name, request.arguments, vehicle)
        result = {"ok": True, "executed": True, "vehicle_state": vehicle}
    return ToolObservation(
        identity=checkpoint.start.identity,
        invocation_id=checkpoint.run.pending[index].invocation_id,
        status=status,
        result=result,
    )


def advertise(
    request,
    name,
    *,
    operation="control",
    location="device",
    action=None,
    content_kind=None,
):
    request.context.capabilities.tools.append(
        type(request.context.capabilities.tools[0])(
            name=name,
            operation=operation,
            execution_location=location,
            domain="fixture",
            action=action,
            content_kind=content_kind,
        )
    )
    return request
