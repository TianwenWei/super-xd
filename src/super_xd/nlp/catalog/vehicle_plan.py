"""Vehicle plan schemas derived only from the structural capability catalog."""
from copy import deepcopy
from super_xd.nlp.catalog.vehicle import TOOLS

PLAN_FIELDS = ('status', 'intents', 'actions', 'keep_paths', 'question', 'reason')
PLAN_DIAGNOSTIC_FIELDS = frozenset({'model_observations', 'load_evidence', 'trace_events'})
PLAN_METADATA_FIELDS = {'ok', 'executed', 'loaded_skills', 'model_calls', 'plan_call_id', 'model_call_id',
                        'provenance', 'validation', 'action_ledger'} | PLAN_DIAGNOSTIC_FIELDS
ID = {'type': 'string', 'minLength': 1, 'maxLength': 100, 'pattern': r'[\s\S]*\S[\s\S]*'}
IDS = {'type': 'array', 'items': ID, 'uniqueItems': True, 'maxItems': 64}


def vehicle_state_paths(value, prefix=''):
    result = []
    if prefix:
        result.append(prefix)
    if isinstance(value, dict):
        for key, child in value.items():
            result.extend(vehicle_state_paths(child, f'{prefix}.{key}' if prefix else str(key)))
    return result



def object_schema(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}



def plan_schema(allowed_names, request_text, vehicle, *, tools=TOOLS):
    """Dynamic per-name argument schema, using the actual loaded catalog."""
    state_paths = vehicle_state_paths(vehicle)
    keep = {'type': 'array', 'items': {'type': 'string', 'enum': state_paths}, 'uniqueItems': True, 'maxItems': 64}
    intent = object_schema({
        'id': ID, 'source_text': {'type': 'string', 'minLength': 1, 'maxLength': max(1, len(request_text))},
        'source_start': {'type': 'integer', 'minimum': 0, 'maximum': len(request_text)},
        'source_end': {'type': 'integer', 'minimum': 1, 'maximum': len(request_text)},
        'status': {'type': 'string', 'enum': ['planned', 'keep', 'clarification', 'refused', 'completed']},
        'action_ids': IDS, 'completed_operation_ids': IDS, 'keep_paths': keep,
        'question': {'type': 'string'}, 'reason': {'type': 'string'},
    })
    variants = [{'description': tools[name].description,
                 **object_schema({'id': ID, 'intent_id': ID, 'name': {'type': 'string', 'enum': [name]},
                            'arguments': deepcopy(tools[name].parameters), 'depends_on': IDS})} for name in allowed_names]
    return object_schema({'status': {'type': 'string', 'enum': ['planned', 'clarification', 'refused']},
                    'intents': {'type': 'array', 'items': intent, 'minItems': 1, 'maxItems': 32},
                    'actions': {'type': 'array', 'items': {'anyOf': variants}, 'maxItems': 48},
                    'keep_paths': keep, 'question': {'type': 'string'}, 'reason': {'type': 'string'}})
