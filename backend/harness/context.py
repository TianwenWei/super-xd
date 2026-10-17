"""Build model context from records, without a session actor or live services."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any

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


def _turn_messages(turn: ContextTurn) -> list[dict[str, Any]]:
    segment = deepcopy(list(turn.messages))
    if turn.interrupted_playback:
        for record in segment:
            if record.get('role') == 'assistant' and not record.get('tool_calls'):
                record['content'] = turn.heard_text or ''
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
    return [compact_message(record, *call_context.get(record.get('tool_call_id'), (None, False)))
            for record in segment]


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
    context['vehicle'] = {skill_id: {key: value for key, value in values.items() if key != 'memories'}
                          for skill_id, values in context['vehicle'].items()}
    context['background_tasks_reference_only'] = compact_tool_result({
        'tasks': policy.select_tasks(context['background_tasks_reference_only']),
    }, 'manage_task')['tasks']
    # Select before copying so old reports and omitted memories stay out of the
    # request's allocation cost, as well as its serialized model context.
    context = deepcopy(context)
    context['interrupted_playback'] = [
        {'turn_id': previous.id, 'heard_text': previous.heard_text or '', 'status': 'interrupted'}
        for previous in history if previous.interrupted_playback
    ]
    previous_user = next((previous for previous in reversed(history)
                          if previous.id != current_turn.id and not previous.proactive), None)
    context['speaker_changed_since_previous_turn'] = bool(
        previous_user and previous_user.speaker_seat and previous_user.speaker_seat != context['speaker']['seat'])
    messages = [{'role': 'system', 'content': prompts.core},
                {'role': 'system', 'content': prompts.persona}]
    if not any(previous.id == current_turn.id for previous in history):
        history.append(current_turn)
    for previous in history:
        if previous.id == current_turn.id:
            messages.append({'role': 'system', 'content': prompts.render_snapshot(context)})
            messages.append({'role': 'system', 'content': prompts.current_turn})
        messages.extend(_turn_messages(previous))
    return messages
