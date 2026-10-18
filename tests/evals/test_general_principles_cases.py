"""Keep behavioral evaluation requirements explicit and separate from few-shots."""
from tests.support.paths import REPO_ROOT
import json
from pathlib import Path


ROOT = REPO_ROOT


def test_general_principles_cases_have_fixtures_and_review_rubrics():
    cases = json.loads((ROOT / 'evals/cases/general_principles.json').read_text())
    assert len({case['id'] for case in cases}) == len(cases)
    assert {'authorization', 'evidence_scope', 'playback', 'retrieval_progress'} <= {
        tag for case in cases for tag in case['tags']}
    examples = (ROOT / 'prompts/talker/examples.json').read_text()
    for case in cases:
        for turn in case['turns']:
            assert turn['expect']['response_rubric']
            assert turn['input'] not in examples
            if 'playback' in turn:
                assert turn['playback']['status'] in {'partial', 'unknown', 'completed', 'not_started'}
        for name, results in case.get('lookup_fixtures', {}).items():
            assert name in {'get_weather', 'search_manual', 'search_places', 'web_search'}
            assert results and all(isinstance(result, dict) for result in results)


def test_capability_variants_use_valid_distinct_catalog_contracts():
    from super_xd.nlp.prompts.catalog import TOOLS
    from jsonschema import Draft202012Validator
    cases = json.loads((ROOT / 'evals/cases/general_principles.json').read_text())
    variants = [case for case in cases if {'capability_semantics', 'speaker_binding'} & set(case['tags'])]
    assert len(variants) == 4
    for case in variants:
        for turn in case['turns']:
            assert turn['expect']['commands_exact']
            for command in turn['expect']['commands']:
                Draft202012Validator(TOOLS[command['name']].parameters).validate(command['arguments'])


def test_query_scope_heldout_is_separate_and_accepts_clarification():
    from evals.harness import grade
    cases = json.loads((ROOT / 'evals/cases/heldout_query_scope.json').read_text())
    examples = (ROOT / 'prompts/talker/examples.json').read_text()
    for case in cases:
        assert 'heldout' in case['tags'] and 'query_scope' in case['tags']
        for turn in case['turns']:
            assert turn['input'] not in examples
            assert turn['expect']['response_rubric']
            assert 'talker_calls' not in turn['expect']  # Asking for the missing entity is valid.
            assert grade(turn['expect'], {'commands': [], 'talker_calls': [], 'lookups': [], 'tasks_created': 0}) == []


def test_consultation_allows_bounded_read_only_verification_but_forbids_control():
    from evals.harness import grade
    cases = json.loads((ROOT / 'evals/cases/general_principles.json').read_text())
    expect = next(case for case in cases if case['id'] == 'principle_media_advice_does_not_control')['turns'][0]['expect']
    observed = {'commands': [], 'talker_calls': [{'name': 'search_manual', 'arguments': {'query': '功能区别'}}],
                'lookups': ['search_manual'], 'tasks_created': 0}
    assert grade(expect, observed) == []
    observed['talker_calls'].append({'name': 'control_media', 'arguments': {'action': 'pause'}})
    observed['cockpit_commands'] = [{'domain': 'media', 'action': 'pause', 'arguments': {}}]
    assert grade(expect, observed)
    observed['talker_calls'].pop()
    observed['cockpit_commands'].clear()
    observed['talker_calls'].append({'name': 'control_media', 'arguments': {'action': 'get_status'}})
    assert grade(expect, observed) == []
    observed['lookups'].append('search_manual')
    assert grade(expect, observed)


def test_expert_prompt_regressions_are_generalized_and_separate_from_examples():
    cases = json.loads((ROOT / 'evals/cases/expert_prompt_contracts.json').read_text())
    examples = (ROOT / 'prompts/talker/examples.json').read_text()
    assert len({case['id'] for case in cases}) == len(cases)
    assert {'authorization', 'capability_boundary', 'speaker_binding'} <= {tag for case in cases for tag in case['tags']}
    for case in cases:
        for turn in case['turns']:
            assert turn['expect']['response_rubric']
            assert turn['input'] not in examples
