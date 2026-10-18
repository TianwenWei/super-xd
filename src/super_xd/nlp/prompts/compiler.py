"""Project validated Gateway facts into provider messages."""
from copy import deepcopy
import json
from typing import Any

from super_xd.nlp.contracts.gateway_v1 import HistoryTurn, ToolRecord, AssistantRecord
from super_xd.nlp.prompts.context import ContextTurn
from super_xd.nlp.prompts.templates import render_prompt


def compile_history_turn(turn: HistoryTurn) -> ContextTurn:
    records: list[dict[str, Any]] = []
    if turn.source_task is not None:
        attribution = turn.source_task.model_dump(include={'id', 'goal', 'revision', 'status'})
        records.append({'role': 'system', 'content': render_prompt('history_attribution',
                       attribution_json=json.dumps(attribution, ensure_ascii=False))})
    for item in turn.records:
        if isinstance(item, ToolRecord):
            records.append({'role': 'tool', 'tool_call_id': item.tool_call_id,
                            'content': json.dumps(item.result, ensure_ascii=False)})
        elif isinstance(item, AssistantRecord):
            record: dict[str, Any] = {'role': 'assistant', 'content': item.content}
            record.update(deepcopy(item.provider_metadata))
            if item.tool_calls:
                record['tool_calls'] = [{'id': call.id, 'type': 'function', 'function': {
                    'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)},
                    **({'extra_content': deepcopy(call.extra_content)} if call.extra_content else {})}
                    for call in item.tool_calls]
            records.append(record)
        else:
            records.append(item.model_dump())
    return ContextTurn(turn.turn_id, records, proactive=turn.proactive,
                       speaker_seat=turn.speaker_seat, pending_tool_group=turn.pending_tool_group,
                       interrupted_playback=turn.interrupted_playback, heard_text=turn.heard_text,
                       playback_status=turn.playback_status)
