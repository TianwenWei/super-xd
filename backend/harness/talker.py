"""Compile the fixed Talker interface into a standalone NLP run input."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from backend.catalog import TOOLS, SKILLS, HOT_TOOL_NAMES
from backend.cockpit.tools import COCKPIT_TOOLS
from backend.manual_search import MANUAL_TOOLS
from backend.maps_tools import MAPS_TOOLS
from backend.prompt_templates import load_examples, load_prompt, render_prompt
from backend.tool_text_config import tool_text
from backend.web_search import WEB_TOOLS

from .context import ContextPrompts, ContextSelectionPolicy, ContextTurn, build_talker_messages
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
CORE_PROMPT = render_prompt(
    'talker', hot_tool_names=', '.join(HOT_TOOL_NAMES),
    skill_index='; '.join(f'{key}:{value.name}，{value.description}' for key, value in SKILLS.items()))
CURRENT_TURN_PROMPT = render_prompt('current_turn')
TASK_SNAPSHOT_NOTICE = render_prompt('task_snapshot_notice')
TALKER_EXAMPLES = load_examples()


def talker_tools() -> list[dict[str, Any]]:
    """Provider count and per-turn snapshots never expand this interface."""
    return deepcopy([TOOLS[name].openai_tool() for name in HOT_TOOL_NAMES]
                    + CONTROL_TOOLS + MAPS_TOOLS + WEB_TOOLS + MANUAL_TOOLS + COCKPIT_TOOLS)


def prepare_talker_run(identity: RunIdentity, *, current_turn: ContextTurn,
                       turns: Sequence[ContextTurn], snapshot: Mapping[str, Any],
                       persona_prompt: str, release_id: str, budget: RunBudget | None = None,
                       policy: ContextSelectionPolicy | None = None) -> PreparedRun:
    """Compile approved prompts and facts; never construct clients or execute tools."""
    if identity.turn_id != current_turn.id:
        raise ValueError('Run identity must match the current context turn')
    messages = build_talker_messages(
        current_turn=current_turn, turns=turns, snapshot=snapshot,
        prompts=ContextPrompts(CORE_PROMPT, persona_prompt, CURRENT_TURN_PROMPT, load_prompt('session_snapshot')),
        policy=policy or ContextSelectionPolicy(),
    )
    messages[2:2] = deepcopy(TALKER_EXAMPLES)
    return PreparedRun(identity=identity.model_copy(deep=True), messages=messages, tools=talker_tools(),
                       budget=budget.model_copy(deep=True) if budget is not None else RunBudget(),
                       release_id=release_id, model_options={'role': 'talker'})
