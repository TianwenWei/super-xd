"""Explicit offline vehicle-plan doubles; never used by production code."""

import asyncio
from copy import deepcopy
from super_xd.nlp.prompts.catalog import TOOLS


class FakeVehiclePlanner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def plan(
        self,
        request_text,
        vehicle,
        skill_ids,
        history=None,
        completed_operations=None,
        trace=None,
        speaker_seat="driver",
        capabilities=None,
    ):
        self.requests.append(
            deepcopy(
                {
                    "request_text": request_text,
                    "vehicle": vehicle,
                    "skill_ids": skill_ids,
                    "history": history,
                    "completed_operations": completed_operations,
                    "speaker_seat": speaker_seat,
                }
            )
        )
        if not self.responses:
            raise AssertionError("Unscripted test vehicle-planner request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, tuple):
            delay, response = response
            await asyncio.sleep(delay)
        if callable(response):
            response = response(request_text)
        if trace:
            trace(
                "model.started",
                {"agent": "vehicle_planner", "messages": [], "tools": []},
            )
            trace(
                "model.completed",
                {
                    "agent": "vehicle_planner",
                    "message": {"role": "assistant", "content": None},
                },
            )
        return deepcopy(response)


def planned(
    request_text, operations, *, intent_text=None, keep_paths=(), keep_text=None
):
    """One complete original intent can legitimately require many actions."""
    text = intent_text or request_text
    start = request_text.index(text)
    actions = [
        {
            "id": f"action-{i}",
            "intent_id": "intent-1",
            "name": name,
            "arguments": deepcopy(arguments),
            "depends_on": [],
        }
        for i, (name, arguments) in enumerate(operations, 1)
    ]
    result = {
        "ok": True,
        "status": "planned",
        "plan_call_id": "fake-plan-call",
        "question": "",
        "reason": "",
        "loaded_skills": list(
            dict.fromkeys(
                TOOLS[a["name"]].skill_id for a in actions if a["name"] in TOOLS
            )
        ),
        "intents": [
            {
                "id": "intent-1",
                "text": text,
                "source_text": text,
                "source_start": start,
                "source_end": start + len(text),
                "status": "planned",
                "action_ids": [a["id"] for a in actions],
                "keep_paths": [],
                "question": "",
                "reason": "",
            }
        ],
        "actions": actions,
        "keep_paths": list(keep_paths),
    }
    if keep_paths:
        if not keep_text:
            raise ValueError(
                "Offline fixtures must explicitly identify the keep intent's original text"
            )
        start = request_text.index(keep_text)
        result["intents"].append(
            {
                "id": "intent-keep",
                "source_text": keep_text,
                "text": keep_text,
                "source_start": start,
                "source_end": start + len(keep_text),
                "status": "keep",
                "action_ids": [],
                "keep_paths": list(keep_paths),
                "completed_operation_ids": [],
                "question": "",
                "reason": "明确保持原设置",
            }
        )
    return result


def plan_response(*operations, keep_paths=(), keep_text=None):
    return lambda original: planned(
        original, operations, keep_paths=keep_paths, keep_text=keep_text
    )


def expected_talker_tool_names():
    from super_xd.nlp.orchestration.talker import talker_tools

    return [item["function"]["name"] for item in talker_tools()]
