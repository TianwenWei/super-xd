"""Shared explicit fixtures for vehicle delegation tests."""

from copy import deepcopy


from super_xd.nlp.prompts.catalog import TOOLS

from tests.support.session import call


def delegate(skills=(), id="delegate"):
    return call("delegate_vehicle_control", {"skill_ids": list(skills)}, id)


def split_plan(original, definitions):
    """Explicit source spans and intent/action links, without intent inference."""
    intents, actions, keeps = [], [], []
    for index, definition in enumerate(definitions, 1):
        text = definition["text"]
        start = original.index(text)
        identity = f"intent-{index}"
        action_ids = []
        for operation in definition.get("operations", []):
            action_id = f"action-{len(actions) + 1}"
            action_ids.append(action_id)
            actions.append(
                {
                    "id": action_id,
                    "intent_id": identity,
                    "name": operation[0],
                    "arguments": deepcopy(operation[1]),
                    "depends_on": [],
                }
            )
        paths = list(definition.get("keep_paths", []))
        keeps.extend(paths)
        intents.append(
            {
                "id": identity,
                "source_text": text,
                "text": text,
                "source_start": start,
                "source_end": start + len(text),
                "status": definition.get("status", "planned"),
                "action_ids": action_ids,
                "keep_paths": paths,
                "completed_operation_ids": definition.get(
                    "completed_operation_ids", []
                ),
                "question": "",
                "reason": definition.get("reason", ""),
            }
        )
    return {
        "ok": True,
        "status": "planned",
        "intents": intents,
        "actions": actions,
        "keep_paths": keeps,
        "question": "",
        "reason": "",
        "plan_call_id": "fake-multi-plan",
        "loaded_skills": list(
            dict.fromkeys(TOOLS[a["name"]].skill_id for a in actions)
        ),
        "executed": False,
    }
