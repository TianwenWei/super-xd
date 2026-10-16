"""Named, startup-validated prompts; rendering never reads configuration files."""
from functools import lru_cache
import json
from pathlib import Path
from backend.prompt_config import substitute, template_parts

PROMPT_DIRECTORY = Path(__file__).resolve().parent.parent / 'prompts'
PROMPT_FILES = {
    'talker': ('talker/system.md', {'hot_tool_names', 'skill_index'}),
    'history_attribution': ('talker/history_attribution.md', {'attribution_json'}),
    'current_turn': ('talker/current_turn.md', set()),
    'speaker_context': ('talker/speaker.md', {'speaker_json'}),
    'session_snapshot': ('talker/snapshot.md', {'snapshot_json', 'speaker_name'}),
    'task_snapshot_notice': ('talker/task_snapshot_notice.md', set()),
    'persona': ('talker/persona.md', {'persona_json'}),
    'prewarm': ('talker/prewarm.md', set()),
    'vehicle_planner': ('planner/system.md', set()),
    'planner_retry': ('planner/retry.md', set()),
    'thinker_quick': ('thinker/quick.md', set()),
    'thinker_research': ('thinker/research.md', {'skill_guide'}),
    'thinker_source_selection': ('thinker/source_selection.md', set()),
    'thinker_retry': ('thinker/retry.md', set()),
    'thinker_finalize': ('thinker/finalize.md', set()),
    'thinker_revision': ('thinker/revision.md', set()),
    'manual_embedding_document': ('manual/embedding_document.md', set()),
    'manual_embedding_query': ('manual/embedding_query.md', set()),
}


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    if not isinstance(name, str) or name not in PROMPT_FILES:
        raise ValueError(f'Unknown prompt name: {name!r}')
    text = (PROMPT_DIRECTORY / PROMPT_FILES[name][0]).read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError(f'Prompt {name} must not be empty')
    return text


@lru_cache(maxsize=None)
def _parts(name: str) -> tuple[tuple[bool, str], ...]:
    parts = template_parts(load_prompt(name), name)
    actual = {value for flag, value in parts if flag}
    expected = PROMPT_FILES[name][1]
    if actual != expected:
        raise ValueError(f'Prompt {name}: expected variables={sorted(expected)}, actual={sorted(actual)}')
    return parts


def render_prompt(name: str, **variables: str) -> str:
    return substitute(_parts(name), f'Prompt {name}', variables)


def validate_examples(messages):
    if not isinstance(messages, list) or not messages:
        raise ValueError('Talker examples must be a nonempty message array')
    pending, seen = set(), set()
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('Example message must be an object')
        role = message.get('role')
        allowed = {'role', 'content'}
        if role == 'assistant':
            allowed.add('tool_calls')
        elif role == 'tool':
            allowed.add('tool_call_id')
        elif role not in {'system', 'user'}:
            raise ValueError(f'Invalid example role: {role!r}')
        if message.keys() - allowed or 'content' not in message or not (
                isinstance(message['content'], str) or role == 'assistant' and message['content'] is None):
            raise ValueError('Invalid example message fields/content')
        if role == 'tool':
            identifier = message.get('tool_call_id')
            if not isinstance(identifier, str) or identifier not in pending:
                raise ValueError('Example tool result must match a pending tool call')
            pending.remove(identifier)
        else:
            if pending:
                raise ValueError('Example tool calls must receive results before the next message')
            calls = message.get('tool_calls', [])
            if not isinstance(calls, list):
                raise ValueError('Example tool_calls must be an array')
            for call in calls:
                if not isinstance(call, dict) or set(call) != {'id', 'type', 'function'}:
                    raise ValueError('Invalid example tool call fields')
                identifier, function = call['id'], call['function']
                if not isinstance(identifier, str) or not identifier or identifier in seen or call['type'] != 'function':
                    raise ValueError('Example tool call IDs must be unique nonempty strings')
                if not isinstance(function, dict) or set(function) != {'name', 'arguments'} or not (
                        isinstance(function['name'], str) and function['name'] and isinstance(function['arguments'], str)):
                    raise ValueError('Invalid example function fields')
                if not isinstance(json.loads(function['arguments']), dict):
                    raise ValueError('Example arguments must serialize a JSON object')
                seen.add(identifier)
                pending.add(identifier)
    if pending:
        raise ValueError('Example tool calls are missing results')
    return messages


@lru_cache(maxsize=None)
def load_examples():
    return validate_examples(json.loads((PROMPT_DIRECTORY / 'talker/examples.json').read_text(encoding='utf-8')))


def preload_prompts() -> None:
    # File IO belongs to startup, never the first request in the actor loop.
    for name in PROMPT_FILES:
        _parts(name)


preload_prompts()
load_examples()
