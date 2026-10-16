"""Dispatch acknowledgements describe a request, never its completion."""
from backend.catalog import TOOLS
from backend.speech_config import feedback_text


def dispatch_feedback(name, arguments, grouped=False):
    if TOOLS[name].state_mapping.get('operation') == 'read':
        return ''
    if grouped:
        return feedback_text('dispatch.grouped')
    if name == 'set_window_position':
        seat = feedback_text('dispatch.seat.' + arguments['window'])
        position = arguments['position']
        if position in (0, 100):
            action = feedback_text('dispatch.set_window_position.closed' if position == 0 else 'dispatch.set_window_position.open', seat=seat)
        else:
            action = feedback_text('dispatch.set_window_position.partial', seat=seat, position=position)
    elif name == 'close_all_windows':
        action = feedback_text('dispatch.close_all_windows')
    elif name == 'set_navigation_destination':
        action = feedback_text('dispatch.set_navigation_destination', destination=arguments['destination'])
    elif name == 'set_temperature':
        action = feedback_text('dispatch.set_temperature', temperature=arguments['temperature'])
    else:
        action = feedback_text('dispatch.fallback')
    return feedback_text('dispatch.text', action=action)
