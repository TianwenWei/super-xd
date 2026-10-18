"""NLP orchestration tool schemas. Description values identify configured text."""
from super_xd.nlp.catalog.vehicle import SKILLS


def function(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}}}


STR = {'type': 'string', 'minLength': 1, 'maxLength': 4000}
TASK_ID = {'type': 'string', 'minLength': 1, 'pattern': r'[\s\S]*\S[\s\S]*', 'description': 'common.task_id.description'}
CONTROL_TOOLS = [
    function('delegate_vehicle_control', 'delegate_vehicle_control.description',
             {'skill_ids': {'type': 'array', 'items': {'type': 'string', 'enum': list(SKILLS)},
                            'maxItems': 10, 'uniqueItems': True,
                            'description': 'delegate_vehicle_control.parameters.skill_ids.description'}}, ['skill_ids']),
    function('delegate_task', 'delegate_task.description',
             {'goal': STR,
              'mode': {'type': 'string', 'enum': ['foreground', 'background'],
                       'description': 'delegate_task.parameters.mode.description'},
              'spoken_goal': {'type': 'string', 'maxLength': 80, 'description': 'delegate_task.parameters.spoken_goal.description'},
              'read_details': {'type': 'boolean', 'description': 'delegate_task.parameters.read_details.description'},
              'query_domain': {'type': 'string', 'enum': ['weather', 'general'], 'description': 'delegate_task.parameters.query_domain.description'},
              'query_mode': {'type': 'string', 'enum': ['quick', 'research'], 'description': 'delegate_task.parameters.query_mode.description'},
              'required_source_domains': {'type': 'array', 'maxItems': 5, 'uniqueItems': True,
                                          'items': {'type': 'string', 'minLength': 3, 'maxLength': 253},
                                          'description': 'delegate_task.parameters.required_source_domains.description'}}, ['goal']),
    function('manage_task', 'manage_task.description',
             {'action': {'type':'string','enum':['get','reply','update','cancel'], 'description':'manage_task.parameters.action.description'}, 'task_id': TASK_ID,
              'task_ids': {'type':'array','items':TASK_ID,'minItems':1,'maxItems':8,'uniqueItems':True,'description':'manage_task.parameters.task_ids.description'},
              'text': {**STR, 'description':'manage_task.parameters.text.description'},
              'read_details': {'type':'boolean','description':'manage_task.parameters.read_details.description'}}, ['action']),
]
CONTROL_BY_NAME = {tool['function']['name']: tool for tool in CONTROL_TOOLS}
