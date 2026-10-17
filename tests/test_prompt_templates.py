"""Prompt-file contracts: strict data substitution and stable conversation order."""
import json

import pytest

from backend import prompt_templates
from scripts.validate_structured_prompts import capture_case, import_runtime


@pytest.fixture
def template_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_templates, 'PROMPT_DIRECTORY', tmp_path)
    monkeypatch.setattr(prompt_templates, 'PROMPT_FILES', {'sample': ('sample.md', {'value'})})
    prompt_templates.load_prompt.cache_clear()
    prompt_templates._parts.cache_clear()
    yield tmp_path
    prompt_templates.load_prompt.cache_clear()
    prompt_templates._parts.cache_clear()


def write_template(directory, name, text):
    (directory / f'{name}.md').write_text(text)
    # Each test declares its own exact variable contract independently of parsing.
    if '{{other}}' in text:
        prompt_templates.PROMPT_FILES[name] = (f'{name}.md', {'value', 'other'})
    elif '{{' not in text:
        prompt_templates.PROMPT_FILES[name] = (f'{name}.md', set())


def test_template_variables_are_exact_and_nonrecursive(template_directory):
    write_template(template_directory, 'sample', '# Context\n{{value}}\n{{value}}\n{{other}}')
    result = prompt_templates.render_prompt('sample', value='{{other}}', other='untrusted data')
    assert result == '# Context\n{{other}}\n{{other}}\nuntrusted data'


@pytest.mark.parametrize('variables', [{}, {'value': 'x', 'extra': 'y'}])
def test_template_rejects_missing_or_extra_variables(template_directory, variables):
    write_template(template_directory, 'sample', '# Context\n{{value}}')
    with pytest.raises(ValueError):
        prompt_templates.render_prompt('sample', **variables)


def test_template_rejects_nonserialized_values(template_directory):
    write_template(template_directory, 'sample', '# Context\n{{value}}')
    with pytest.raises(TypeError):
        prompt_templates.render_prompt('sample', value={'instruction': 'data'})


@pytest.mark.parametrize('name', ['../sample', '/tmp/sample', 'UPPER', 'sample.txt', '', 'sample-name'])
def test_template_names_cannot_escape_prompt_directory(name):
    with pytest.raises(ValueError):
        prompt_templates.load_prompt(name)


@pytest.mark.parametrize('text', ['# Context\n{{unfinished', '# Context\n{{}}',
                                  '# Context\n{{bad-name}}', '# Context\nunexpected }}'])
def test_template_rejects_invalid_placeholders(template_directory, text):
    write_template(template_directory, 'sample', text)
    with pytest.raises(ValueError):
        prompt_templates.render_prompt('sample')


def test_template_read_is_cached(template_directory):
    write_template(template_directory, 'sample', '# First version')
    assert prompt_templates.load_prompt('sample') == '# First version'
    (template_directory / 'sample.md').unlink()
    assert prompt_templates.load_prompt('sample') == '# First version'


def test_production_templates_include_instruction_sections_and_plain_data_wrappers():
    assert prompt_templates.PROMPT_FILES
    for name, (path, _) in prompt_templates.PROMPT_FILES.items():
        assert (prompt_templates.PROMPT_DIRECTORY / path).is_file()
        text = prompt_templates.load_prompt(name)
        assert text
        assert prompt_templates._parts(name)
    for name in ('talker', 'vehicle_planner', 'thinker_research', 'thinker_quick', 'persona'):
        assert prompt_templates.load_prompt(name).startswith('# ')
    assert prompt_templates.load_prompt('session_snapshot').startswith('当前会话快照')


def test_preloaded_templates_render_without_request_time_file_io(template_directory, monkeypatch):
    write_template(template_directory, 'sample', '# Data\n{{value}}')
    prompt_templates.preload_prompts()
    def unavailable_read(*args, **kwargs):
        raise AssertionError('File IO must not occur while rendering a preloaded prompt')
    monkeypatch.setattr(type(template_directory), 'read_text', unavailable_read)
    assert prompt_templates.render_prompt('sample', value='current data') == '# Data\ncurrent data'


async def test_history_precedes_dynamic_snapshot_and_latest_input(monkeypatch):
    runtime = import_runtime()
    monkeypatch.setattr(runtime.session, 'utcnow', runtime.session.utcnow)
    case = {'id': 'ordered-history', 'role': 'talker', 'text': '把它关上。',
            'history': [{'user': '打开主驾车窗。', 'assistant': '主驾车窗已打开。'}]}
    request = await capture_case(runtime, case, '2026-10-14T12:00:00+00:00')
    messages = request['messages']
    history_index = next(index for index, item in enumerate(messages) if item.get('content') == '打开主驾车窗。')
    snapshot_index = next(index for index, item in enumerate(messages) if item['role'] == 'system' and
                          (item.get('content') or '').startswith('当前会话快照'))
    current_index = next(index for index, item in enumerate(messages) if item.get('content') == case['text'])
    assert messages[0]['role'] == 'system' and messages[0]['content'].startswith('# ')
    # Fixed rules, then persona, then native examples, then history (AGENTS.md: Prompt与配置).
    persona_header = prompt_templates.load_prompt('persona').split('{{', 1)[0]
    assert messages[1]['role'] == 'system' and messages[1]['content'].startswith(persona_header)
    examples_end = next(i for i, m in enumerate(messages) if m.get('content') == '示例结束。下面才是和当前用户的真实对话。')
    assert 1 < examples_end < history_index < snapshot_index < current_index


async def test_dynamic_state_does_not_change_stable_prefix_or_tools(monkeypatch):
    runtime = import_runtime()
    monkeypatch.setattr(runtime.session, 'utcnow', runtime.session.utcnow)
    first = {'id': 'one', 'role': 'talker', 'text': '你好', 'vehicle_overrides': {'media.volume': 10}}
    second = {'id': 'two', 'role': 'talker', 'text': '现在音量多少？', 'vehicle_overrides': {'media.volume': 70}}
    before = await capture_case(runtime, first, '2026-10-14T12:00:00+00:00')
    after = await capture_case(runtime, second, '2026-10-15T12:00:00+00:00')
    assert before['messages'][:1] == after['messages'][:1]
    assert before['tools'] == after['tools']
    assert len(before['tools']) == 26
    assert before['messages'] != after['messages']


@pytest.mark.parametrize('mode', [None, 'route'])
async def test_voice_and_text_send_the_same_single_request_shape(monkeypatch, mode):
    runtime = import_runtime()
    monkeypatch.setattr(runtime.session, 'utcnow', runtime.session.utcnow)
    case = {'id': 'speech-contract', 'role': 'talker', 'text': '简单介绍秋天。', 'speech_mode': mode}
    request = await capture_case(runtime, case, '2026-10-14T12:00:00+00:00')
    assert len(request['tools']) == 26
    assert request['messages'][0]['content'].startswith(prompt_templates.load_prompt('talker').split('{{hot_tool_names}}', 1)[0])
    assert not any('response_mode' in (m.get('content') or '') for m in request['messages'])


async def test_native_assistant_and_tool_history_remains_protocol_valid(monkeypatch):
    runtime = import_runtime()
    monkeypatch.setattr(runtime.session, 'utcnow', runtime.session.utcnow)
    tool_call = {'id': 'historical-window', 'type': 'function', 'function': {
        'name': 'set_window_position', 'arguments': json.dumps({'window': 'driver', 'position': 40})}}
    history = [
        {'role': 'user', 'content': '主驾车窗开到40%。'},
        {'role': 'assistant', 'content': None, 'tool_calls': [tool_call]},
        {'role': 'tool', 'tool_call_id': 'historical-window', 'content': json.dumps({'ok': True, 'value': 40})},
        {'role': 'assistant', 'content': '主驾车窗已开到40%。'},
    ]
    case = {'id': 'native-history', 'role': 'talker', 'text': '把它关上。',
            'history': [{'user': history[0]['content'], 'assistant': history[-1]['content'], 'messages': history}]}
    request = await capture_case(runtime, case, '2026-10-14T12:00:00+00:00')
    messages = request['messages']
    assistant_index = next(index for index, item in enumerate(messages) if item.get('tool_calls') == [tool_call])
    assert messages[assistant_index]['tool_calls'] == [tool_call]
    assert messages[assistant_index + 1]['role'] == 'tool'
    assert messages[assistant_index + 1]['tool_call_id'] == tool_call['id']
    assert json.loads(messages[assistant_index + 1]['content']) == {'ok': True, 'value': 40}
    snapshot_index = next(index for index, item in enumerate(messages) if (item.get('content') or '').startswith('当前会话快照'))
    assert assistant_index + 1 < snapshot_index


def test_plain_data_wrappers_are_valid_templates(template_directory):
    write_template(template_directory, 'sample', 'Only data follows:\n{{value}}')
    assert prompt_templates.render_prompt('sample', value='serialized') == 'Only data follows:\nserialized'
