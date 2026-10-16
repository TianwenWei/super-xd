"""Configuration failures must surface at startup, before serving a turn."""
import json
from pathlib import Path
import pytest
from backend import prompt_config as config, prompt_templates as templates


@pytest.fixture
def text_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CONFIG_DIRECTORY', tmp_path)
    monkeypatch.setattr(config, '_CONTRACTS', {})
    config.load_config.cache_clear()
    config._config_parts.cache_clear()
    yield tmp_path
    config.load_config.cache_clear()
    config._config_parts.cache_clear()


def write(directory, text):
    (directory / 'sample.toml').write_text(text, encoding='utf-8')


def test_text_contract_is_exact_and_nonrecursive(text_directory, monkeypatch):
    write(text_directory, 'message = "你好 {{name}}，{{name}}"')
    config.validate_config('sample.toml', {'message': {'name'}})
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('request-time IO'))
    assert config.config_text('sample.toml', 'message', name='{{name}}') == '你好 {{name}}，{{name}}'
    with pytest.raises(ValueError):
        config.config_text('sample.toml', 'message', wrong='x')
    with pytest.raises(TypeError):
        config.config_text('sample.toml', 'message', name=1)


@pytest.mark.parametrize('text,expected', [
    ('unexpected = "x"', {'message': set()}),
    ('message = "{{wrong}}"', {'message': {'right'}}),
    ('message = "x"\nextra = "y"', {'message': set()}),
    ('message = 12', {'message': set()}),
    ('message = "{{bad-name}}"', {'message': set()}),
    ('message = "unmatched }}"', {'message': set()}),
    ('"a.b" = "first"\n[a]\nb = "second"', {'a.b': set()}),
    ('[empty]', {}),
])
def test_invalid_config_fails_before_rendering(text_directory, text, expected):
    write(text_directory, text)
    with pytest.raises(ValueError):
        config.validate_config('sample.toml', expected)


def test_missing_file_and_key_have_no_fallback(text_directory):
    with pytest.raises(FileNotFoundError):
        config.validate_config('sample.toml', {'message': set()})
    write(text_directory, 'message = "x"')
    with pytest.raises(ValueError):
        config.config_text('sample.toml', 'missing')


@pytest.mark.parametrize('path', ['../secret.toml', '/secret.toml', 'sample.json', 'a//b.toml', 'UPPER.toml'])
def test_config_paths_are_restricted(path):
    with pytest.raises(ValueError):
        config.load_config(path)


def test_production_inventory_and_contracts():
    summary = config.validate_all()
    assert summary['templates'] == 18
    assert summary['text_files'] == 11
    assert summary['text_entries'] > 500


def test_unknown_config_file_rejected_at_startup(tmp_path, monkeypatch):
    config.validate_all()
    monkeypatch.setattr(config, 'CONFIG_DIRECTORY', tmp_path)
    (tmp_path / 'extra.toml').write_text('message="x"')
    with pytest.raises(ValueError, match='Text config files'):
        config.validate_all()


@pytest.mark.parametrize('messages', [
    [], [{'role': 'user', 'content': 'x', 'extra': True}],
    [{'role': 'tool', 'tool_call_id': 'missing', 'content': '{}'}],
    [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a', 'type': 'function',
        'function': {'name': 'set_volume', 'arguments': '[]'}}]}],
    [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a', 'type': 'function',
        'function': {'name': 'set_volume', 'arguments': '{}'}}]}, {'role': 'user', 'content': 'x'}],
])
def test_native_example_protocol_errors_fail_at_startup(messages):
    with pytest.raises(ValueError):
        templates.validate_examples(messages)


def test_native_example_results_resolve_all_calls():
    examples = json.loads(json.dumps(templates.load_examples()))
    assert templates.validate_examples(examples) == examples


@pytest.mark.parametrize('name,arguments', [
    ('invented_vehicle_tool', {}),
    ('set_volume', {'volume': 'invalid-type'}),
])
def test_invalid_native_example_tools_fail_at_startup(monkeypatch, name, arguments):
    examples = templates.validate_examples([
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'invalid-example', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)},
        }]},
        {'role': 'tool', 'tool_call_id': 'invalid-example', 'content': '{"ok":true}'},
    ])
    monkeypatch.setattr(templates, 'load_examples', lambda: examples)
    with pytest.raises(ValueError, match='Example message'):
        config.validate_all()
