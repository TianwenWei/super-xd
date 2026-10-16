"""Talker tool prose; schemas and permissions stay in their owning modules."""
from backend.prompt_config import config_text, validate_config

TOOL_TEXT_KEYS = (
    'common.task_id.description',
    'delegate_vehicle_control.description',
    'delegate_vehicle_control.parameters.skill_ids.description',
    'delegate_task.description',
    'delegate_task.parameters.spoken_goal.description',
    'delegate_task.parameters.read_details.description',
    'delegate_task.parameters.query_domain.description',
    'delegate_task.parameters.query_mode.description',
    'delegate_task.parameters.required_source_domains.description',
    'manage_task.description',
    'manage_task.parameters.action.description',
    'manage_task.parameters.task_ids.description',
    'manage_task.parameters.text.description',
    'manage_task.parameters.read_details.description',
    'search_places.description',
    'search_places.parameters.area.description',
    'get_weather.description',
    'start_poi_navigation.description',
    'web_search.description',
    'web_search.parameters.include_domains.description',
    'search_manual.description',
    'search_manual.parameters.query.description',
)
validate_config('talker/tools.toml', {key: set() for key in TOOL_TEXT_KEYS})


def tool_text(key):
    return config_text('talker/tools.toml', key)
