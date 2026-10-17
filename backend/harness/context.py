"""Build model context from records, without a session actor or live services."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Literal

from backend.model_context import compact_message, compact_tool_result
from backend.prompt_config import substitute, template_parts


@dataclass(frozen=True)
class ContextTurn:
    id: str
    messages: Sequence[Mapping[str, Any]]
    proactive: bool = False
    speaker_seat: str | None = None
    pending_tool_group: bool = False
    interrupted_playback: bool = False
    heard_text: str = ''
    playback_status: Literal['unknown', 'not_started', 'partial', 'completed'] = 'unknown'

    def __post_init__(self):
        if self.playback_status not in {'unknown', 'not_started', 'partial', 'completed'}:
            raise ValueError('Invalid playback status')
        if not isinstance(self.heard_text, str):
            raise ValueError('Confirmed heard text must be a string')
        if self.playback_status == 'not_started' and self.heard_text:
            raise ValueError('Unstarted playback cannot contain confirmed heard text')


@dataclass(frozen=True)
class ContextPrompts:
    core: str
    persona: str
    current_turn: str
    snapshot_template: str
    _snapshot_parts: tuple[tuple[bool, str], ...] = field(init=False, repr=False)

    def __post_init__(self):
        parts = template_parts(self.snapshot_template, 'Context snapshot')
        if {value for variable, value in parts if variable} != {'speaker_name', 'snapshot_json'}:
            raise ValueError('Context snapshot requires speaker_name and snapshot_json variables')
        object.__setattr__(self, '_snapshot_parts', parts)

    def render_snapshot(self, snapshot: Mapping[str, Any]) -> str:
        return substitute(self._snapshot_parts, 'Context snapshot', {
            'speaker_name': snapshot['speaker']['display_name'],
            'snapshot_json': json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')),
        })


@dataclass(frozen=True)
class ContextSelectionPolicy:
    """Inputs are chronological; prioritization is opt-in for new consumers."""
    history_turn_limit: int = 4
    task_limit: int = 8
    prefer_user_turns: bool = False
    prefer_active_tasks: bool = False
    active_task_statuses: tuple[str, ...] = (
        'queued', 'running', 'waiting_clarification', 'cancelling', 'awaiting_confirm',
    )

    def __post_init__(self):
        for limit in (self.history_turn_limit, self.task_limit):
            if type(limit) is not int or limit < 0:
                raise ValueError('Context limits must be nonnegative integers')

    def select_turns(self, turns: Sequence[ContextTurn]) -> list[ContextTurn]:
        if not self.prefer_user_turns:
            return list(turns[-self.history_turn_limit:]) if self.history_turn_limit else []
        indices = sorted(range(len(turns)), key=lambda index: (not turns[index].proactive, index), reverse=True)
        return [turns[index] for index in sorted(indices[:self.history_turn_limit])]

    def select_tasks(self, tasks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        if not self.prefer_active_tasks:
            return list(tasks[-self.task_limit:]) if self.task_limit else []
        indices = sorted(range(len(tasks)), key=lambda index: (
            tasks[index].get('status') in self.active_task_statuses, index), reverse=True)
        return [tasks[index] for index in sorted(indices[:self.task_limit])]


def _turn_messages(turn: ContextTurn, *, vehicle_baseline=None) -> list[dict[str, Any]]:
    segment = deepcopy(list(turn.messages))
    # Generation is conversational evidence even when delivery is incomplete.
    # Playback facts travel separately; missing feedback is not an empty answer.
    if turn.pending_tool_group:
        # Retain earlier complete exchanges, but never send a partial tool group.
        cutoff = next((index for index, record in reversed(list(enumerate(segment)))
                       if record.get('tool_calls')), len(segment))
        segment = segment[:cutoff]
    call_context = {}
    for record in segment:
        for call in record.get('tool_calls', []):
            function = call.get('function', {})
            try:
                arguments = json.loads(function.get('arguments', '{}'))
            except (TypeError, ValueError):
                arguments = {}
            call_context[call['id']] = (
                function.get('name'), isinstance(arguments, dict) and arguments.get('read_details') is True,
            )
    return [compact_message(record, *call_context.get(record.get('tool_call_id'), (None, False)),
                            vehicle_baseline=vehicle_baseline)
            for record in segment]


def project_talker_snapshot(snapshot: Mapping[str, Any], tool_names: set[str],
                            vehicle_tools: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
    """Project the fixed interface, independently of the user's current intent.

    Full device declarations stay in the caller's authoritative context. Cold
    controls remain discoverable by name/skill, including unavailable and absent
    declarations, without repeating their execution metadata for the Talker.
    """
    projected = dict(snapshot)
    rows = snapshot.get('tool_capabilities', [])
    features = snapshot.get('capabilities', {})
    projected['tool_capabilities'] = [row for row in rows if row.get('name') in tool_names]
    cold = {name: spec for name, spec in vehicle_tools.items() if name not in tool_names}
    index = {}
    for name, (skill, operation) in cold.items():
        entry = index.setdefault(skill, {'available_count': 0, 'available': [], 'unavailable': [], 'missing': []})
        declarations = [row for row in rows if row.get('name') == name]
        available = any(row.get('available', True) is True and row.get('operation') == operation
                        and row.get('execution_location') == 'device' for row in declarations)
        status = ('unavailable' if features.get(name) is False else
                  'missing' if not declarations else 'available' if available else 'unavailable')
        entry[status].append(name)
        if status == 'available':
            entry['available_count'] += 1
    projected['vehicle_skill_capabilities'] = {
        skill: {key: value for key, value in entry.items() if value or key == 'available_count'}
        for skill, entry in index.items()}
    # The same explicit cold feature denial is encoded above; unrelated flags
    # such as GPS and real navigation remain verbatim for interpreting facts.
    projected['capabilities'] = {name: value for name, value in features.items() if name not in cold}
    return projected


def _compact_snapshot(context):
    """Remove structural duplication only; keep every capability and target."""
    speaker = context.get('speaker', {})
    for name, defaults in speaker.get('control_defaults', {}).items():
        if isinstance(defaults, dict) and defaults.get('tool') == name:
            defaults.pop('tool')
    capabilities = context.get('tool_capabilities')
    if isinstance(capabilities, list):
        for capability in capabilities:
            if isinstance(capability, dict):
                for field in ('action', 'content_kind'):
                    if capability.get(field) is None:
                        capability.pop(field, None)
                if capability.get('available') is True:
                    capability.pop('available')
    return context


def project_vehicle_state(vehicle: Mapping[str, Any]) -> dict[str, Any]:
    """The model's baseline omits stored memory presets, as does its snapshot."""
    return deepcopy({skill_id: {key: value for key, value in values.items() if key != 'memories'}
                     for skill_id, values in vehicle.items()})


def build_talker_messages(*, current_turn: ContextTurn, turns: Sequence[ContextTurn],
                          snapshot: Mapping[str, Any], prompts: ContextPrompts,
                          policy: ContextSelectionPolicy = ContextSelectionPolicy()) -> list[dict[str, Any]]:
    """Return independent messages using the supplied prompts and snapshot.

    ``snapshot`` supplies vehicle, background_tasks_reference_only and speaker
    (seat/display_name), plus any already-resolved environment/capability data.
    The explicit current record replaces any archived copy with the same ID.
    Other turn IDs must be unique. Native few-shot examples are inserted by the
    caller after the two stable system messages, as in the existing actor.
    """
    records, seen = [], set()
    for previous in turns:
        if previous.id in seen:
            if previous.id == current_turn.id:
                continue
            raise ValueError('Historical turn IDs must be unique')
        seen.add(previous.id)
        records.append(current_turn if previous.id == current_turn.id else previous)
    history = policy.select_turns(records)
    context = dict(snapshot)
    context['vehicle'] = project_vehicle_state(context['vehicle'])
    vehicle_baseline = ({'state': context['vehicle'], 'revision': context['vehicle_state_revision'],
                         'observed_at': context['vehicle_observed_at']}
                        if type(context.get('vehicle_state_revision')) is int
                        and isinstance(context.get('vehicle_observed_at'), str)
                        and context['vehicle_observed_at'] else None)
    context['background_tasks_reference_only'] = compact_tool_result({
        'tasks': policy.select_tasks(context['background_tasks_reference_only']),
    }, 'manage_task')['tasks']
    # Select before copying so old reports and omitted memories stay out of the
    # request's allocation cost, as well as its serialized model context.
    context = _compact_snapshot(deepcopy(context))
    context['playback_history'] = [
        {'turn_id': previous.id, 'heard_text': previous.heard_text,
         'status': ('partial' if previous.playback_status == 'unknown' and previous.heard_text
                    else previous.playback_status), 'interrupted': previous.interrupted_playback}
        for previous in history if any(record.get('role') == 'assistant' for record in previous.messages)
    ]
    previous_user = next((previous for previous in reversed(history)
                          if previous.id != current_turn.id and not previous.proactive), None)
    speaker_changed = bool(
        previous_user and previous_user.speaker_seat and previous_user.speaker_seat != context['speaker']['seat'])
    # Current speaker facts precede the larger state payload. History remains
    # chronological and cannot replace the current turn's source attribution.
    context = {'speaker': context.pop('speaker'), 'speaker_changed_since_previous_turn': speaker_changed,
               **{key: value for key, value in context.items() if key != 'speaker_changed_since_previous_turn'}}
    messages = [{'role': 'system', 'content': prompts.core},
                {'role': 'system', 'content': prompts.persona}]
    if not any(previous.id == current_turn.id for previous in history):
        history.append(current_turn)
    for previous in history:
        if previous.id == current_turn.id:
            messages.append({'role': 'system', 'content': prompts.render_snapshot(context)})
            messages.append({'role': 'system', 'content': prompts.current_turn})
        messages.extend(_turn_messages(previous, vehicle_baseline=vehicle_baseline))
    return messages
