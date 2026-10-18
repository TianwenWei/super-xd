"""Expert role templates retain strict startup and delivery-mode boundaries."""

import json
from pathlib import Path

import pytest

import super_xd.nlp.prompts.templates as prompts
from super_xd.nlp.prompts.persona import PERSONAS, style_prompt
from super_xd.nlp.contracts.catalog import PersonaConfig
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


def test_expert_role_files_and_runtime_sections_are_all_registered():
    assert prompts.ROLE_DOCUMENTS == {
        'common.md', 'talker.md', 'talker_event.md', 'reasoner.md',
        'reasoner_foreground.md', 'reasoner_background.md', 'persona.md', 'context.md',
    }
    assert prompts.ROLE_DOCUMENTS <= prompts.PROMPT_RESOURCE_FILES
    for file, (_, contracts) in prompts.SECTION_DOCUMENTS.items():
        selected = {section for path, sections in prompts.PROMPT_SECTIONS.values() if path == file for section in sections}
        assert selected == contracts.keys()


def test_expert_roles_only_offer_implemented_tools_and_neutral_research_delivery():
    from super_xd.nlp.orchestration.talker import CORE_PROMPT
    assert prompts.load_prompt('common') in CORE_PROMPT
    assert 'mode=foreground' in CORE_PROMPT and 'mode=background' in CORE_PROMPT
    for name in ('reasoner', 'reasoner_foreground', 'reasoner_background'):
        text = prompts.load_prompt(name)
        assert '{{persona' not in text
        for unsupported in ('confirm_with_user(', 'call_agent(', 'schedule_action(', 'continue_in_background('):
            assert unsupported not in text
    assert '不调用业务工具' in prompts.load_prompt('talker_event')


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
    assert actual == prompts.render_prompt('persona', persona_json=json.dumps(persona.model_dump(), ensure_ascii=False))
    custom = PersonaConfig(name='用户自定义', tone='用户自定义风格')
    custom_prompt = style_prompt(custom)
    assert json.dumps(custom.model_dump(), ensure_ascii=False) in custom_prompt
    assert persona.tone == '用户修改的表达偏好'



async def test_role_and_mode_requests_do_not_include_other_sections(monkeypatch):
    runtime = import_runtime()
    fixed_time = '2026-10-14T12:00:00+00:00'
    requests = {role: await capture_case(runtime, {'id': role, 'role': role, 'text': '离线输入'}, fixed_time)
                for role in ('talker', 'thinker_quick', 'thinker_research')}
    examples = prompts.load_examples()
    assert requests['talker']['messages'][2:2 + len(examples)] == examples
    for role, request in requests.items():
        text = '\n'.join(message.get('content') or '' for message in request['messages'])
        assert '## [section:' not in text
        assert prompts.load_prompt('prewarm') not in text
        assert prompts.load_prompt('thinker_finalize') not in text
        assert prompts.load_prompt('thinker_retry') not in text
        assert prompts.load_prompt('planner_retry') not in text
        if role == 'thinker_quick':
            assert '# 查询方式：快速核验' in text and '# 查询方式：多步研究' not in text
            assert 'suggest_vehicle_action' not in text
        elif role == 'thinker_research':
            assert '# 查询方式：多步研究' in text and '# 查询方式：快速核验' not in text
            assert 'official_source_status' not in text
        else:
            assert '# 查询方式：' not in text
