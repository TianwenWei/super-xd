"""Render the compact planner interface without changing its validation rules."""
from copy import deepcopy
from super_xd.nlp.catalog.vehicle_plan import ID, IDS, object_schema, plan_schema as structural_plan_schema
from super_xd.nlp.prompts.catalog import TOOLS
from super_xd.nlp.prompts.config import config_text, validate_config

validate_config('planner/tools.toml', {
    key: set() for key in (
        'submit_vehicle_plan.description', 'load_skill.description',
        'submit_vehicle_plan.parameters.actions.arguments',
        'submit_vehicle_plan.parameters.intents.source_text',
        'submit_vehicle_plan.parameters.keep_paths',
        'submit_vehicle_plan.parameters.intents.keep_paths',
    )
})


def plan_schema(allowed_names, request_text, vehicle):
    return structural_plan_schema(allowed_names, request_text, vehicle, tools=TOOLS)


def submission_schema(allowed_names, request_text, vehicle):
    """Compact provider wire schema; internal validation stays state-exact.

    Actions carry a plain ``arguments`` object. A per-function anyOf made the
    Pro model emit malformed calls as text (8/12 planned, P90 50s vs 12/12,
    P90 12s compact); the exact per-function schemas are supplied as data in
    ``action_schemas`` and enforced by validate_vehicle_plan.
    """
    schema = plan_schema(allowed_names, request_text, vehicle)
    schema['properties']['actions']['items'] = object_schema({
        'id': ID, 'intent_id': ID, 'name': {'type': 'string', 'enum': list(allowed_names)},
        'arguments': {'type': 'object', 'description': config_text('planner/tools.toml', 'submit_vehicle_plan.parameters.actions.arguments')},
        'depends_on': IDS})
    schema['properties']['keep_paths'] = deepcopy(schema['properties']['keep_paths'])
    intent = schema['properties']['intents']['items']
    for name in ('source_start', 'source_end'):
        intent['properties'].pop(name)
        intent['required'].remove(name)
    intent['properties']['source_text']['description'] = config_text('planner/tools.toml', 'submit_vehicle_plan.parameters.intents.source_text')
    for keep in (schema['properties']['keep_paths'], intent['properties']['keep_paths']):
        keep['items'].pop('enum')
        keep['items']['minLength'] = 1
    schema['properties']['keep_paths']['description'] = config_text('planner/tools.toml', 'submit_vehicle_plan.parameters.keep_paths')
    intent['properties']['keep_paths']['description'] = config_text('planner/tools.toml', 'submit_vehicle_plan.parameters.intents.keep_paths')
    return schema
