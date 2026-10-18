"""Named, startup-validated prompts; rendering never reads configuration files."""
from functools import lru_cache
import json
from types import MappingProxyType
from super_xd.nlp.prompts.config import substitute, template_parts

from super_xd.nlp.resources import PROMPT_ROOT

PROMPT_DIRECTORY = PROMPT_ROOT
# Expert role files are readable templates, not a second section language.
ROLE_DOCUMENTS = frozenset({
    'common.md', 'talker.md', 'talker_event.md', 'reasoner.md',
    'reasoner_foreground.md', 'reasoner_background.md', 'persona.md', 'context.md',
})
SECTION_DOCUMENTS = {
    'vehicle_planner.md': ('# Vehicle planner', {'system': set(), 'retry': set()}),
    'runtime.md': ('# Runtime protocol', {
        'cold_catalog': {'catalog_json'}, 'history_attribution': {'attribution_json'},
        'current_turn': set(), 'speaker_context': {'speaker_json'},
        'task_snapshot_notice': set(), 'prewarm': set(), 'quick': set(),
        'research': {'skill_guide'}, 'source_selection': set(), 'retry': set(),
        'finalize': set(), 'revision': set(),
    }),
}
PROMPT_SECTIONS = {
    'talker_cold_catalog': ('runtime.md', ('cold_catalog',)),
    **{name: ('runtime.md', (name,)) for name in (
        'history_attribution', 'current_turn', 'speaker_context', 'task_snapshot_notice', 'prewarm')},
    'vehicle_planner': ('vehicle_planner.md', ('system',)),
    'planner_retry': ('vehicle_planner.md', ('retry',)),
    **{f'thinker_{name}': ('runtime.md', (name,)) for name in (
        'quick', 'research', 'source_selection', 'retry', 'finalize', 'revision')},
}
PROMPT_FILES = {
    'common': ('common.md', set()),
    'talker': ('talker.md', {'hot_tool_names', 'skill_index'}),
    'talker_event': ('talker_event.md', set()),
    'reasoner': ('reasoner.md', set()),
    'reasoner_foreground': ('reasoner_foreground.md', set()),
    'reasoner_background': ('reasoner_background.md', set()),
    'persona': ('persona.md', {'persona_json'}),
    'session_snapshot': ('context.md', {'snapshot_json', 'speaker_name'}),
    **{name: (file, set().union(*(SECTION_DOCUMENTS[file][1][section] for section in sections)))
       for name, (file, sections) in PROMPT_SECTIONS.items()},
    'manual_embedding_document': ('manual/embedding_document.md', set()),
    'manual_embedding_query': ('manual/embedding_query.md', set()),
}
PROMPT_RESOURCE_FILES = frozenset(path for path, _ in PROMPT_FILES.values()) | {'talker/examples.json'}


def parse_prompt_sections(text: str, *, title: str, contracts: dict, label: str):
    """Parse a deliberately small Markdown section format without regex."""
    lines = text.strip().splitlines()
    if not lines or lines[0] != title:
        raise ValueError(f'{label}: expected document title {title!r}')
    sections, current, content = {}, None, []

    def finish():
        if current is not None:
            value = '\n'.join(content).strip()
            if not value:
                raise ValueError(f'{label}: empty section {current}')
            parts = template_parts(value, f'{label}:{current}')
            actual = {value for variable, value in parts if variable}
            if actual != contracts[current]:
                raise ValueError(f'{label}:{current}: expected variables={sorted(contracts[current])}, actual={sorted(actual)}')
            sections[current] = value

    for line in lines[1:]:
        if line.lstrip().startswith('## [section'):
            if not line.startswith('## [section: ') or not line.endswith(']'):
                raise ValueError(f'{label}: invalid section header {line!r}')
            name = line[len('## [section: '):-1]
            if name not in contracts:
                raise ValueError(f'{label}: unknown section {name!r}')
            if name in sections or name == current:
                raise ValueError(f'{label}: duplicate section {name}')
            finish()
            current, content = name, []
        elif current is None:
            if line.strip():
                raise ValueError(f'{label}: text outside a named section')
        else:
            content.append(line)
    finish()
    if sections.keys() != contracts.keys():
        raise ValueError(f'{label}: missing sections={sorted(contracts.keys() - sections.keys())}')
    return MappingProxyType(sections)


@lru_cache(maxsize=None)
def _load_document(file: str):
    title, contracts = SECTION_DOCUMENTS[file]
    return parse_prompt_sections((PROMPT_DIRECTORY / file).read_text(encoding='utf-8'),
                                 title=title, contracts=contracts, label=file)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    if not isinstance(name, str) or name not in PROMPT_FILES:
        raise ValueError(f'Unknown prompt name: {name!r}')
    if name in PROMPT_SECTIONS:
        file, sections = PROMPT_SECTIONS[name]
        text = '\n\n'.join(_load_document(file)[section] for section in sections)
    else:
        text = (PROMPT_DIRECTORY / PROMPT_FILES[name][0]).read_text(encoding='utf-8').strip()
    if name in {'thinker_quick', 'thinker_research'}:
        text = '\n\n'.join((load_prompt('common'), load_prompt('reasoner'), text))
    elif name == 'vehicle_planner':
        text = load_prompt('common') + '\n\n' + text
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


def prompt_statistics() -> dict:
    """Character counts describe actual text, not estimated provider tokens."""
    examples = load_examples()
    return {
        'role_documents': len(ROLE_DOCUMENTS),
        'runtime_sections': sum(len(contracts) for _, contracts in SECTION_DOCUMENTS.values()),
        'template_characters': {name: len(load_prompt(name)) for name in PROMPT_FILES},
        'example_messages': len(examples),
        'example_tool_calls': sum(len(message.get('tool_calls', [])) for message in examples),
        'example_serialized_characters': len(json.dumps(examples, ensure_ascii=False, separators=(',', ':'))),
    }


preload_prompts()
load_examples()
