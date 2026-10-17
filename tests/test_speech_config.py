"""Fixed speech comes from text configuration without changing execution inputs."""
from copy import deepcopy
from pathlib import Path

import pytest

from backend.catalog import TOOLS
from backend.demo.vehicle_simulator import execute_tool, initial_vehicle_state
from backend.persona import PERSONAS, render
from backend.speech_config import feedback_text, places_sentence, receipt_detail
from backend.task_feedback import task_user_detail


@pytest.mark.parametrize('name', tuple(TOOLS))
def test_receipt_templates_cover_the_full_vehicle_catalog_without_mutating_receipts(name):
    arguments = deepcopy(TOOLS[name].example)
    result = execute_tool(name, arguments, initial_vehicle_state())
    before_arguments, before_result = deepcopy(arguments), deepcopy(result)
    if result['ok']:
        assert isinstance(receipt_detail(name, arguments, result), str)
    assert arguments == before_arguments and result == before_result


def test_rendering_never_reopens_configuration_files(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Rendering must use the startup text cache')
    monkeypatch.setattr(Path, 'open', forbidden)
    assert receipt_detail('set_window_position', {'window': 'driver', 'position': 50}, {})
    assert task_user_detail({'status': 'running', 'progress': {'phase': 'extract_web', 'read_sources': 2}})
    assert places_sentence(['甲', '乙'])
    for persona in PERSONAS.values():
        assert render(persona, 'success', '空调调到二十三度了。')
        assert render(persona, 'delegated', '核对行程资料')


def test_position_and_confirmed_temperature_are_template_variables(monkeypatch):
    import backend.speech_config as module
    original = module.config_text
    def custom(file, key, **variables):
        if file == 'speech/receipts.toml' and key == 'set_temperature.text':
            return f"自定义回执：{variables['zone']}，{variables['temperature']}。"
        return original(file, key, **variables)
    monkeypatch.setattr(module, 'config_text', custom)
    arguments = {'zone': 'front_passenger', 'temperature': 24}
    assert receipt_detail('set_temperature', arguments, {}) == '自定义回执：副驾，二十四。'
    assert arguments == {'zone': 'front_passenger', 'temperature': 24}


def test_variable_text_is_not_interpreted_as_another_template():
    marker = '{{not_a_config_variable}}'
    assert marker in feedback_text('places.single', name=marker)


def test_missing_or_extra_spoken_template_variables_fail_immediately():
    with pytest.raises(ValueError):
        feedback_text('places.single')
    with pytest.raises(ValueError):
        feedback_text('places.single', name='地点', unexpected='内容')


def test_every_function_has_independently_editable_receipt_wording():
    from backend.speech_config import RECEIPTS_FIELDS
    for name in TOOLS:
        assert any(key.startswith(name + '.') for key in RECEIPTS_FIELDS)


def test_configured_task_summaries_are_bound_values_not_sql(tmp_path, monkeypatch):
    from backend import db
    original = db.feedback_text
    queued = "排队：用户的'条件'；保留引号。"
    running = "分析：'条件'。"
    def configured(key, **variables):
        return {'task.db.queued': queued, 'task.db.running': running}.get(key) or original(key, **variables)
    monkeypatch.setattr(db, 'feedback_text', configured)
    database = db.Database(tmp_path / 'summaries.sqlite3')
    task = database.create_task('session', '实际目标')
    assert task['summary'] == queued and task['revision'] == 1
    running_task = database.claim_next()
    assert running_task['summary'] == running and running_task['id'] == task['id']
    assert database.get_task(task['id'])['goal'] == '实际目标'
