"""Configuration failures must surface at startup, before serving a turn."""
from tests.support.paths import REPO_ROOT
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
import super_xd.nlp.prompts.config as config
import super_xd.nlp.prompts.templates as templates


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
    assert summary['templates'] == len(templates.PROMPT_FILES)
    assert summary['prompts']['role_documents'] == 8
    assert summary['prompts']['runtime_sections'] == sum(
        len(contracts) for _, contracts in templates.SECTION_DOCUMENTS.values())
    examples = templates.load_examples()
    assert summary['example_messages'] == len(examples)
    assert summary['prompts']['example_tool_calls'] == sum(len(row.get('tool_calls', [])) for row in examples)
    assert summary['text_files'] == 11
    assert summary['text_entries'] > 500


def test_production_validation_does_not_import_demo_runtime():
    command = '''import sys
from super_xd.nlp.prompts.config import validate_all
assert validate_all()['prompts']['role_documents'] == 8
forbidden = {'super_xd.demo.api.app', 'super_xd.demo.session.actor', 'super_xd.demo.tasks.worker', 'super_xd.demo.tasks.database',
             'super_xd.demo.audio.volcengine_asr', 'super_xd.demo.audio.volcengine_tts',
             'super_xd.demo.audio.speech_stream'}
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', command], cwd=REPO_ROOT,
                            capture_output=True, text=True,
                            env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
    assert result.returncode == 0, result.stderr


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
