"""Compile the fixed Talker interface into a standalone NLP run input."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from typing import Any

from backend.catalog import TOOLS, SKILLS, HOT_TOOL_NAMES
from backend.cockpit.tools import COCKPIT_TOOLS
from backend.tool_schemas.manual import MANUAL_TOOLS
from backend.tool_schemas.maps import MAPS_TOOLS
from backend.prompt_templates import load_examples, load_prompt, render_prompt
from backend.tool_text_config import tool_text
from backend.tool_schemas.web import WEB_TOOLS

from .context import ContextPrompts, ContextSelectionPolicy, ContextTurn, build_talker_messages, project_talker_snapshot
from .contracts import PreparedRun, RunBudget, RunIdentity


def function(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}}}


STR = {'type': 'string', 'minLength': 1, 'maxLength': 4000}
TASK_ID = {'type': 'string', 'minLength': 1, 'pattern': r'[\s\S]*\S[\s\S]*', 'description': tool_text('common.task_id.description')}
CONTROL_TOOLS = [
    function('delegate_vehicle_control', tool_text('delegate_vehicle_control.description'),
             {'skill_ids': {'type': 'array', 'items': {'type': 'string', 'enum': list(SKILLS)},
                            'maxItems': 10, 'uniqueItems': True,
                            'description': tool_text('delegate_vehicle_control.parameters.skill_ids.description')}}, ['skill_ids']),
    function('delegate_task', tool_text('delegate_task.description'),
             {'goal': STR,
              'spoken_goal': {'type': 'string', 'maxLength': 80, 'description': tool_text('delegate_task.parameters.spoken_goal.description')},
              'read_details': {'type': 'boolean', 'description': tool_text('delegate_task.parameters.read_details.description')},
              'query_domain': {'type': 'string', 'enum': ['weather', 'general'], 'description': tool_text('delegate_task.parameters.query_domain.description')},
              'query_mode': {'type': 'string', 'enum': ['quick', 'research'], 'description': tool_text('delegate_task.parameters.query_mode.description')},
              'required_source_domains': {'type': 'array', 'maxItems': 5, 'uniqueItems': True,
                                          'items': {'type': 'string', 'minLength': 3, 'maxLength': 253},
                                          'description': tool_text('delegate_task.parameters.required_source_domains.description')}}, ['goal']),
    function('manage_task', tool_text('manage_task.description'),
             {'action': {'type':'string','enum':['get','reply','update','cancel'], 'description':tool_text('manage_task.parameters.action.description')}, 'task_id': TASK_ID,
              'task_ids': {'type':'array','items':TASK_ID,'minItems':1,'maxItems':8,'uniqueItems':True,'description':tool_text('manage_task.parameters.task_ids.description')},
              'text': {**STR, 'description':tool_text('manage_task.parameters.text.description')},
              'read_details': {'type':'boolean','description':tool_text('manage_task.parameters.read_details.description')}}, ['action']),
]
CONTROL_BY_NAME = {tool['function']['name']: tool for tool in CONTROL_TOOLS}
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
CORE_PROMPT = render_prompt(
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
