"""Compile the fixed Talker interface into a standalone NLP run input."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from typing import Any

from super_xd.nlp.catalog.vehicle import HOT_TOOL_NAMES
from super_xd.nlp.prompts.catalog import TOOLS, SKILLS, COCKPIT_TOOLS, MANUAL_TOOLS, MAPS_TOOLS, WEB_TOOLS, CONTROL_TOOLS
from super_xd.nlp.prompts.templates import load_examples, load_prompt, render_prompt

from super_xd.nlp.prompts.context import ContextPrompts, ContextSelectionPolicy, ContextTurn, build_talker_messages, project_talker_snapshot
from super_xd.nlp.contracts.run import PreparedRun, RunBudget, RunIdentity


def talker_tools() -> list[dict[str, Any]]:
    """Provider count and per-turn snapshots never expand this interface."""
    return deepcopy([TOOLS[name].openai_tool() for name in HOT_TOOL_NAMES]
                    + CONTROL_TOOLS + MAPS_TOOLS + WEB_TOOLS + MANUAL_TOOLS + COCKPIT_TOOLS)


# Fixed, complete semantic descriptions belong to the cached role prefix.
# Per-run availability remains in the separate projected context.
_TALKER_TOOL_NAMES = {tool['function']['name'] for tool in talker_tools()}
COLD_CAPABILITY_CATALOG = {
    skill_id: {name: TOOLS[name].description for name in skill.tool_names if name not in _TALKER_TOOL_NAMES}
    for skill_id, skill in SKILLS.items()
}
CORE_PROMPT = load_prompt('common') + '\n\n' + render_prompt(
    'talker', hot_tool_names=', '.join(HOT_TOOL_NAMES),
    skill_index='; '.join(f'{key}:{value.name}，{value.description}' for key, value in SKILLS.items())) + '\n\n' + render_prompt(
        'talker_cold_catalog', catalog_json=json.dumps(COLD_CAPABILITY_CATALOG, ensure_ascii=False, separators=(',', ':')))
CURRENT_TURN_PROMPT = render_prompt('current_turn')
TASK_SNAPSHOT_NOTICE = render_prompt('task_snapshot_notice')
TALKER_EXAMPLES = load_examples()


def prepare_talker_run(identity: RunIdentity, *, current_turn: ContextTurn,
                       turns: Sequence[ContextTurn], snapshot: Mapping[str, Any],
                       persona_prompt: str, release_id: str, budget: RunBudget | None = None,
                       policy: ContextSelectionPolicy | None = None) -> PreparedRun:
    """Compile approved prompts and facts; never construct clients or execute tools."""
    if identity.turn_id != current_turn.id:
        raise ValueError('Run identity must match the current context turn')
    tools = talker_tools()
    projected = project_talker_snapshot(snapshot, {tool['function']['name'] for tool in tools},
        {name: (spec.skill_id, 'query' if spec.state_mapping.get('operation') == 'read' else 'control')
         for name, spec in TOOLS.items()})
    messages = build_talker_messages(
        current_turn=current_turn, turns=turns, snapshot=projected,
        prompts=ContextPrompts(CORE_PROMPT, persona_prompt, CURRENT_TURN_PROMPT, load_prompt('session_snapshot')),
        policy=policy or ContextSelectionPolicy(),
    )
    messages[2:2] = deepcopy(TALKER_EXAMPLES)
    return PreparedRun(identity=identity.model_copy(deep=True), messages=messages, tools=tools,
                       budget=budget.model_copy(deep=True) if budget is not None else RunBudget(),
                       release_id=release_id, model_options={'role': 'talker'})
