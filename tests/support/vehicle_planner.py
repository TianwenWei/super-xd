"""Shared explicit fixtures for vehicle planner tests."""
from tests.support.models import scripted_result

import asyncio

from copy import deepcopy

import json


from super_xd.demo.devices.simulator import initial_vehicle_state

from super_xd.nlp.defaults import VEHICLE_CONTROL_MODEL


def action(identity, intent, name, arguments, depends=()):
    return {
        "id": identity,
        "intent_id": intent,
        "name": name,
        "arguments": arguments,
        "depends_on": list(depends),
    }


def intent(
    identity,
    query,
    source=None,
    *,
    actions=(),
    keep=(),
    status="planned",
    completed=(),
    question="",
    reason="",
):
    source = query if source is None else source
    start = query.index(source)
    return {
        "id": identity,
        "source_text": source,
        "source_start": start,
        "source_end": start + len(source),
        "status": status,
        "action_ids": list(actions),
        "keep_paths": list(keep),
        "completed_operation_ids": list(completed),
        "question": question,
        "reason": reason,
    }


def plan(intents, actions=(), *, status="planned", keep=(), question="", reason=""):
    return {
        "status": status,
        "intents": intents,
        "actions": list(actions),
        "keep_paths": list(keep),
        "question": question,
        "reason": reason,
    }


def temperature_plan(query="把所有温区设为23度。"):
    return plan(
        [intent("temperature", query, actions=["temperature"])],
        [
            action(
                "temperature",
                "temperature",
                "set_temperature",
                {"zone": "all", "temperature": 23},
            )
        ],
    )


def tool(name, arguments, identity="submit"):
    return {
        "id": identity,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def wire_plan(value):
    value = deepcopy(value)
    for item in value.get("intents", []):
        item.pop("source_start", None)
        item.pop("source_end", None)
    return value


def response(value, *, name="submit_vehicle_plan", identity="submit"):
    if name == "submit_vehicle_plan":
        value = wire_plan(value)
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [tool(name, value, identity)],
        "_meta": {
            "model": VEHICLE_CONTROL_MODEL,
            "usage": None,
            "provider_id": identity,
        },
    }


class FakeArk:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def complete(self, request):
        messages = request.messages
        tools = request.tools
        kwargs = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
        self.requests.append(
            {"messages": deepcopy(messages), "tools": deepcopy(tools), **kwargs}
        )
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, asyncio.Event):
            await reply.wait()
            raise AssertionError("test should cancel instead of unblocking provider")
        return scripted_result(deepcopy(reply))


def names(request):
    return {tool["function"]["name"] for tool in request["tools"]}


def action_names(request):
    submit = next(
        t for t in request["tools"] if t["function"]["name"] == "submit_vehicle_plan"
    )
    return set(
        submit["function"]["parameters"]["properties"]["actions"]["items"][
            "properties"
        ]["name"]["enum"]
    )


def window_keep_plan():
    query = "副驾车窗别动，把其余三扇窗都关上。"
    keep = "windows.positions.front_passenger"
    value = plan(
        [
            intent("hold", query, "副驾车窗别动", status="keep", keep=[keep]),
            intent(
                "close",
                query,
                "把其余三扇窗都关上。",
                actions=["driver", "rear_left", "rear_right"],
            ),
        ],
        [
            action(
                seat, "close", "set_window_position", {"window": seat, "position": 0}
            )
            for seat in ("driver", "rear_left", "rear_right")
        ],
        keep=[keep],
    )
    state = initial_vehicle_state()
    state["windows"]["positions"].update(
        driver=10, front_passenger=50, rear_left=20, rear_right=30
    )
    return query, value, state
