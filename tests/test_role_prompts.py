"""Role entrypoints select only needed sections and validate before requests."""

import json
from pathlib import Path

import pytest

from backend import prompt_templates as prompts
from backend.persona import PERSONAS, style_prompt
from backend.types import PersonaConfig
from scripts.validate_structured_prompts import capture_case, import_runtime


def parse(text, contracts=None):
    return prompts.parse_prompt_sections(text, title='# Role', label='role.md',
                                         contracts=contracts or {'first': {'value'}, 'second': set()})


def test_document_sections_are_independent_and_preserve_template_data():
    sections = parse('# Role\n\n## [section: first]\n# Data\n{{value}}\n\n## [section: second]\nOnly this mode.')
    assert sections == {'first': '# Data\n{{value}}', 'second': 'Only this mode.'}
    with pytest.raises(TypeError):
        sections['first'] = 'changed'


@pytest.mark.parametrize('text', [
    '# Wrong\n## [section: first]\n{{value}}\n## [section: second]\ntext',
    '# Role\nInstructions outside a section\n## [section: first]\n{{value}}\n## [section: second]\ntext',
    '# Role\n## [section: first]\n{{value}}',
    '# Role\n## [section: first]\n{{value}}\n## [section: unknown]\ntext',
    '# Role\n## [section: first]\n{{value}}\n## [section: first]\n{{value}}',
    '# Role\n## [section: first]\n{{value}}\n## [section: second]\ntext\n## [section: first]\n{{value}}',
    '# Role\n## [section: first]\n{{value}}\n## [section: second]\n',
    '# Role\n## [section: first]\n{{wrong}}\n## [section: second]\ntext',
    '# Role\n## [section: first]\n{{value}}\n## [section: second]\n{{extra}}',
    '# Role\n## [section: first]\n{{value}}\n## [section: second\ntext',
    '# Role\n## [section: first]\n{{value}}\n ## [section: second]\ntext',
])
def test_invalid_role_document_fails_at_startup(text):
    with pytest.raises(ValueError):
        parse(text)


def test_only_three_role_entrypoints_and_no_unused_sections():
    assert set(prompts.ROLE_DOCUMENTS) == {'talker.md', 'reasoner_foreground.md', 'reasoner_background.md'}
    for file, (_, contracts) in prompts.ROLE_DOCUMENTS.items():
        selected = {section for path, sections in prompts.PROMPT_SECTIONS.values() if path == file for section in sections}
        assert selected == contracts.keys()
    for directory in ('talker', 'planner', 'thinker'):
        assert list((prompts.PROMPT_DIRECTORY / directory).glob('*.md')) == []


def test_all_role_sections_render_without_request_file_io(monkeypatch):
    prompts.preload_prompts()
    expected = {name: prompts.render_prompt(name, **{key: '{{untrusted}}' for key in variables})
                for name, (_, variables) in prompts.PROMPT_FILES.items()}

    def unexpected_io(*args, **kwargs):
        pytest.fail('rendering preloaded role sections must not read files')

    monkeypatch.setattr(Path, 'read_text', unexpected_io)
    monkeypatch.setattr(Path, 'open', unexpected_io)
    for name, (_, variables) in prompts.PROMPT_FILES.items():
        assert prompts.render_prompt(name, **{key: '{{untrusted}}' for key in variables}) == expected[name]


@pytest.mark.parametrize('identity', list(PERSONAS))
def test_persona_selects_exactly_one_style_without_changing_configuration(identity):
    persona = PERSONAS[identity].model_copy(deep=True)
    persona.tone = '用户修改的表达偏好'
    actual = style_prompt(persona)
    assert json.dumps(persona.model_dump(), ensure_ascii=False) in actual
    for candidate in PERSONAS:
        assert (prompts.load_prompt('persona_' + candidate) in actual) == (candidate == identity)
    custom = PersonaConfig(name='用户自定义', tone='用户自定义风格')
    custom_prompt = style_prompt(custom)
    assert json.dumps(custom.model_dump(), ensure_ascii=False) in custom_prompt
    assert not any(prompts.load_prompt('persona_' + candidate) in custom_prompt for candidate in PERSONAS)


async def test_role_and_mode_requests_do_not_include_other_sections(monkeypatch):
    runtime = import_runtime()
    fixed_time = '2026-10-14T12:00:00+00:00'
    requests = {role: await capture_case(runtime, {'id': role, 'role': role, 'text': '离线输入'}, fixed_time)
                for role in ('talker', 'thinker_quick', 'thinker_research')}
    assert requests['talker']['messages'][2:106] == prompts.load_examples()
    for role, request in requests.items():
        text = '\n'.join(message.get('content') or '' for message in request['messages'])
        assert '## [section:' not in text
        assert prompts.load_prompt('prewarm') not in text
        assert prompts.load_prompt('thinker_finalize') not in text
        assert prompts.load_prompt('thinker_retry') not in text
        assert prompts.load_prompt('planner_retry') not in text
        if role == 'thinker_quick':
            assert '# 当前模式：快速核验' in text and '# 当前模式：多步研究' not in text
            assert 'suggest_vehicle_action' not in text
        elif role == 'thinker_research':
            assert '# 当前模式：多步研究' in text and '# 当前模式：快速核验' not in text
            assert 'official_source_status' not in text
        else:
            assert '# 当前模式：' not in text
