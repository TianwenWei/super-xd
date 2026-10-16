"""Offline checks for the eval runner itself; live cases need a real model."""
from evals.harness import grade, load_cases, run_all, subset, summarize, persona_signature
from test_session import FakeArk, call
from vehicle_fakes import FakeVehiclePlanner


def observed(**overrides):
    base = {'commands': [], 'talker_calls': [], 'vehicle': {'a': {'b': 1}}, 'vehicle_before': {'a': {'b': 1}},
            'tasks_created': 0, 'route': 'chat', 'error': None}
    return {**base, **overrides}


def test_persona_signature_uses_number_value_but_keeps_boolean_and_strings_distinct():
    def signature(value):
        return persona_signature({'turns': [{'talker_calls': [{'name': 'set', 'arguments': {'value': value}}]}]})
    assert signature(24) == signature(24.0)
    assert signature(True) != signature(1)
    assert signature('24') != signature(24)


def test_cases_load_with_unique_ids_and_known_expectations():
    known = {'commands', 'commands_exact', 'no_commands', 'forbid_commands', 'talker_calls', 'talker_call_options',
             'forbid_talker_calls', 'forbid_lookups', 'no_talker_calls', 'state', 'unchanged', 'tasks_created', 'route', 'allow_error', 'manual_pages_any'}
    cases = load_cases()
    assert len(cases) >= 20
    for case in cases:
        for turn in case['turns']:
            assert set(turn.get('expect', {})) <= known, case['id']


def test_subset_compares_structure_not_text():
    assert subset({'zone': 'driver'}, {'zone': 'driver', 'temperature': 23})
    assert subset(23, 23.0)
    assert not subset(True, 1)
    assert not subset({'zone': 'all'}, {'zone': 'driver'})


def test_accepted_tool_options_still_require_the_expected_city_date_and_tool():
    expect = {'talker_call_options': [[
        {'name': 'get_weather', 'arguments': {'location': 'Paris France', 'date': 'today'}},
        {'name': 'get_weather', 'arguments': {'location': 'Paris 法国', 'date': 'today'}},
    ]]}
    assert grade(expect, observed(talker_calls=[{'name': 'get_weather', 'arguments': {'location': 'Paris 法国', 'date': 'today'}}])) == []
    for item in [
        {'name': 'get_weather', 'arguments': {'location': 'Lyon France', 'date': 'today'}},
        {'name': 'get_weather', 'arguments': {'location': 'Paris France', 'date': 'tomorrow'}},
        {'name': 'web_search', 'arguments': {'location': 'Paris France', 'date': 'today'}},
    ]:
        assert grade(expect, observed(talker_calls=[item]))
    assert grade(expect, observed())


def test_grade_reports_missing_forbidden_and_state():
    expect = {'commands': [{'name': 'set_volume', 'arguments': {'volume': 35}}],
              'forbid_commands': [{'name': 'set_ambient_light'}], 'state': {'a.b': 2}, 'unchanged': ['a.b']}
    failures = grade(expect, observed(commands=[{'name': 'set_ambient_light', 'arguments': {'enabled': False}}]))
    assert len(failures) == 3
    good = observed(commands=[{'name': 'set_volume', 'arguments': {'volume': 35}}], vehicle={'a': {'b': 2}},
                    vehicle_before={'a': {'b': 2}})
    assert grade(expect, good) == []


async def test_runner_drives_simulated_device_and_checks_persona_invariance():
    case = {'id': 'volume', 'personas': ['professional', 'playful'],
            'turns': [{'input': '音量调到35', 'expect': {'commands': [{'name': 'set_volume', 'arguments': {'volume': 35}}],
                                                       'state': {'media.volume': 35}}}]}

    class PerRun:
        """Each run gets the same scripted decision, like a deterministic model."""
        def __init__(self):
            self.inner = None

        async def stream(self, messages, tools):
            async for item in FakeArk([{'tool_calls': [call('set_volume', {'volume': 35})]}]).stream(messages, tools):
                yield item

    results = await run_all([case], PerRun(), concurrency=2, planner=FakeVehiclePlanner())
    summary = summarize(results)
    assert {k: summary[k] for k in ('total', 'passed', 'pass_rate', 'failed')} == {'total': 2, 'passed': 2, 'pass_rate': 1.0, 'failed': {}}
    assert summary['latency']['all']['turns'] == 2
    assert results[0]['turns'][0]['commands'] == [{'name': 'set_volume', 'arguments': {'volume': 35}, 'source': 'talker'}]


def test_persona_signature_ignores_free_text_but_not_structured_arguments():
    from evals.harness import persona_signature
    def run(calls):
        return {'turns': [{'talker_calls': calls, 'commands': []}]}
    a = run([{'name': 'web_search', 'arguments': {'query': '最新的Python版本'}}])
    b = run([{'name': 'web_search', 'arguments': {'query': '现在最新的Python正式版'}}])
    c = run([{'name': 'set_temperature', 'arguments': {'zone': 'driver', 'temperature': 23}}])
    d = run([{'name': 'set_temperature', 'arguments': {'zone': 'all', 'temperature': 23}}])
    assert persona_signature(a) == persona_signature(b)
    assert persona_signature(c) != persona_signature(d)
