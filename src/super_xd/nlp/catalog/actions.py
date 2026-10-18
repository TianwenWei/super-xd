"""Static domain actions; deployment bindings and availability remain separate."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    operation: Literal['query', 'control']
    execution_location: Literal['cloud', 'device']


MEDIA_ACTIONS = ('play', 'pause', 'resume', 'stop', 'exit', 'next', 'previous', 'seek', 'volume', 'get_status')
NAVIGATION_ACTIONS = ('start', 'add_waypoint', 'remove_waypoint', 'cancel', 'get_status', 'route', 'set_origin')
MEDIA_DEFINITIONS = tuple(ActionDefinition(name, 'query' if name == 'get_status' else 'control', 'device')
                          for name in MEDIA_ACTIONS)
NAVIGATION_DEFINITIONS = tuple(ActionDefinition(name, 'query' if name in {'get_status', 'route'} else 'control',
                                               'cloud' if name in {'get_status', 'route'} else 'device')
                               for name in NAVIGATION_ACTIONS)
