"""Dispatch acknowledgements describe a request, never its completion."""
from backend.catalog import TOOLS


def dispatch_feedback(name, arguments, grouped=False):
    if TOOLS[name].state_mapping.get('operation') == 'read':
        return ''
    if grouped:
        return '正在为你执行这组车控操作。'
    if name == 'set_window_position':
        seat = {'driver': '主驾', 'front_passenger': '副驾', 'rear_left': '左后', 'rear_right': '右后', 'all': '全部'}[arguments['window']]
        position = arguments['position']
        action = ('关闭' if position == 0 else '打开') + seat + '车窗' if position in (0, 100) else f'将{seat}车窗调到百分之{position}'
    elif name == 'close_all_windows':
        action = '关闭全部车窗'
    elif name == 'set_navigation_destination':
        action = '设置模拟导航目的地为' + arguments['destination']
    elif name == 'set_temperature':
        action = f'将空调温度调到{arguments["temperature"]}度'
    else:
        action = '调整车辆设置'
    return '正在为你' + action + '。'
