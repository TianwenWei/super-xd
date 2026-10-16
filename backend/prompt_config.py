"""Cached, strict text configuration; execution rules never come from TOML."""
from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType
import tomllib

from jsonschema import Draft202012Validator, ValidationError

CONFIG_DIRECTORY = Path(__file__).resolve().parent.parent / 'prompts'
_CONTRACTS = {}
CONFIG_FILES = frozenset({
    'talker/tools.toml', 'planner/tools.toml', 'thinker/tools.toml', 'vehicle/functions.toml',
    'speech/receipts.toml', 'speech/persona.toml', 'speech/feedback.toml',
    'speech/thinker.toml', 'speech/vehicle.toml',
})


def template_parts(text, label):
    parts, remaining = [], text
    while remaining:
        literal, opening, remaining = remaining.partition('{{')
        if '}}' in literal:
            raise ValueError(f'Unexpected closing placeholder in {label}')
        parts.append((False, literal))
        if not opening:
            break
        variable, closing, remaining = remaining.partition('}}')
        if not closing or not variable or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in variable):
            raise ValueError(f'Invalid placeholder in {label}')
        parts.append((True, variable))
    return tuple(parts)


def substitute(parts, label, variables):
    expected = {value for is_variable, value in parts if is_variable}
    missing, extra = expected - variables.keys(), variables.keys() - expected
    if missing or extra:
        raise ValueError(f'{label}: missing={sorted(missing)}, extra={sorted(extra)}')
    if any(not isinstance(value, str) for value in variables.values()):
        raise TypeError(f'{label}: template variables must be serialized strings')
    return ''.join(variables[value] if is_variable else value for is_variable, value in parts)


def _config_path(file):
    if not isinstance(file, str) or not file.endswith('.toml'):
        raise ValueError('Text configuration must be a relative .toml path')
    segments = file[:-5].split('/')
    if any(not segment or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in segment) for segment in segments):
        raise ValueError('Invalid text configuration path')
    return CONFIG_DIRECTORY / file


@lru_cache(maxsize=None)
def load_config(file):
    with _config_path(file).open('rb') as source:
        document = tomllib.load(source)
    leaves = {}

    def flatten(node, prefix=''):
        for key, value in node.items():
            name = f'{prefix}.{key}' if prefix else key
            if isinstance(value, dict):
                if not value:
                    raise ValueError(f'{file}:{name}: empty table')
                flatten(value, name)
            elif isinstance(value, str):
                if name in leaves:
                    raise ValueError(f'{file}:{name}: ambiguous dotted text key')
                template_parts(value, f'{file}:{name}')
                leaves[name] = value
            else:
                raise ValueError(f'{file}:{name}: only text leaves are allowed')
    flatten(document)
    return MappingProxyType(leaves)


@lru_cache(maxsize=None)
def _config_parts(file, key):
    try:
        text = load_config(file)[key]
    except KeyError:
        raise ValueError(f'{file}: missing text key {key}') from None
    return template_parts(text, f'{file}:{key}')


def config_text(file, key, **variables):
    return substitute(_config_parts(file, key), f'{file}:{key}', variables)


def validate_config(file, expected):
    values = load_config(file)
    missing, extra = expected.keys() - values.keys(), values.keys() - expected.keys()
    if missing or extra:
        raise ValueError(f'{file}: missing={sorted(missing)}, extra={sorted(extra)}')
    for key, variables in expected.items():
        actual = {value for is_variable, value in _config_parts(file, key) if is_variable}
        if actual != set(variables):
            raise ValueError(f'{file}:{key}: expected variables={sorted(variables)}, actual={sorted(actual)}')
    contract = {key: frozenset(variables) for key, variables in expected.items()}
    if file in _CONTRACTS and _CONTRACTS[file] != contract:
        raise ValueError(f'{file}: conflicting text contracts')
    _CONTRACTS[file] = contract


def validate_example_tools(messages, tools):
    """Editable native examples must stay within the same published tool schemas."""
    validators = {tool['function']['name']: Draft202012Validator(tool['function']['parameters'])
                  for tool in tools}
    for index, message in enumerate(messages):
        for call in message.get('tool_calls', []):
            function = call['function']
            name = function['name']
            if name not in validators:
                raise ValueError(f'Example message {index}: unknown Talker tool {name!r}')
            arguments = json.loads(function['arguments'])
            try:
                validators[name].validate(arguments)
            except ValidationError as error:
                raise ValueError(f'Example message {index}: invalid {name} arguments: {error.message}') from None


def validate_all():
    """Both service entry points check every module's text before accepting work."""
    from importlib import import_module
    for module in ('catalog', 'tool_text_config', 'speech_config', 'vehicle_planner', 'worker'):
        import_module(f'backend.{module}')
    from backend.prompt_templates import PROMPT_FILES, preload_prompts, load_examples
    actual = {path.relative_to(CONFIG_DIRECTORY).as_posix()
              for path in CONFIG_DIRECTORY.rglob('*.toml')}
    if actual != CONFIG_FILES:
        raise ValueError(f'Text config files: missing={sorted(CONFIG_FILES - actual)}, extra={sorted(actual - CONFIG_FILES)}')
    if _CONTRACTS.keys() != CONFIG_FILES:
        raise ValueError('Every text configuration must have a registered key/variable contract')
    expected_prompts = {path for path, _ in PROMPT_FILES.values()} | {'talker/examples.json'}
    actual_prompts = {path.relative_to(CONFIG_DIRECTORY).as_posix()
                     for path in CONFIG_DIRECTORY.rglob('*')
                     if path.suffix in {'.md', '.json'} and path.name != 'README.md'}
    if actual_prompts != expected_prompts:
        raise ValueError(f'Prompt files: missing={sorted(expected_prompts - actual_prompts)}, extra={sorted(actual_prompts - expected_prompts)}')
    for file in CONFIG_FILES:
        validate_config(file, _CONTRACTS[file])
    preload_prompts()
    # Importing Session only after module contracts are registered avoids a
    # template/session import cycle and never constructs a running actor.
    from backend.catalog import HOT_TOOL_NAMES, TOOLS
    from backend.session import CONTROL_TOOLS, MAPS_TOOLS, WEB_TOOLS, MANUAL_TOOLS
    examples = load_examples()
    tools = ([TOOLS[name].openai_tool() for name in HOT_TOOL_NAMES]
             + CONTROL_TOOLS + MAPS_TOOLS + WEB_TOOLS + MANUAL_TOOLS)
    validate_example_tools(examples, tools)
    return {'templates': len(PROMPT_FILES), 'example_messages': len(load_examples()),
            'text_files': len(CONFIG_FILES), 'text_entries': sum(len(load_config(file)) for file in CONFIG_FILES)}


def preload_configs():
    # Imports/startup own disk reads; rendering only sees compiled immutable text.
    for path in sorted(CONFIG_DIRECTORY.rglob('*.toml')):
        file = path.relative_to(CONFIG_DIRECTORY).as_posix()
        for key in load_config(file):
            _config_parts(file, key)


preload_configs()
