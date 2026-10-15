import pytest
from backend.conditions import validate_required_origin


@pytest.mark.parametrize('text', ['导航去最近的机场。', '帮我导航到附近的机场', '请导航去离我最近的机场，快点'])
@pytest.mark.parametrize('tool', ['delegate_task', 'set_navigation_destination'])
def test_nearest_navigation_without_origin_is_not_registered_or_executed(text, tool):
    guard = validate_required_origin(text, tool, {})
    assert guard['kind'] == 'missing_required_origin'
    assert guard['executed'] is False and guard['retry_model'] is True
    for item in guard['evidence']:
        assert text[item['start']:item['end']] == item['text']
    assert not any(key in guard for key in ('replacement_tool', 'arguments', 'destination'))


@pytest.mark.parametrize('text', ['我在巴黎。', '从巴黎出发', '以巴黎为起点'])
def test_preceding_explicit_origin_is_preserved(text):
    assert validate_required_origin('导航去最近的机场。', 'delegate_task', {}, [text]) is None


@pytest.mark.parametrize('text', ['我研究过巴黎的机场。', '巴黎天气不错。', '我在巴黎，但后来已经离开了。'])
def test_research_mentions_or_changed_origin_are_not_location(text):
    assert validate_required_origin('导航去最近的机场。', 'delegate_task', {}, [text])


@pytest.mark.parametrize('text', ['我在巴黎，导航去最近的机场。', '导航去上海虹桥站。',
                                  '先不查机场了，导航去上海虹桥站。', '导航到最近机场咖啡馆。'])
def test_guard_does_not_route_other_literal_destinations(text):
    assert validate_required_origin(text, 'set_navigation_destination', {}) is None


def test_old_origin_is_not_reused_after_new_user_context():
    assert validate_required_origin('导航去最近的机场。', 'delegate_task', {}, ['我在巴黎。', '现在位置不确定。'])
    assert validate_required_origin('导航去最近的机场。', 'set_volume', {'volume': 20}) is None
