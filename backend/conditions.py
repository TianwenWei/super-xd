"""Literal missing-condition refusals; never select a tool or invent location."""
import re


_NEAREST_AIRPORT = re.compile(
    r'(?:请|帮我|麻烦你|麻烦|现在|先|我要|我想|\s)*导航(?:去|到|前往)?'
    r'(?:离我最近的|最近的|最近|附近的|附近)(?:一个)?机场'
    r'(?:吧|一下|好吗)?(?:[，,]\s*(?:谢谢|快点|麻烦了))?[。！？!?\s]*$')
_EXPLICIT_ORIGIN = re.compile(
    r'\s*(?:我(?:现在)?在[^，,。！？!?;；\n]{1,80}|'
    r'从[^，,。！？!?;；\n]{1,80}出发|'
    r'以[^，,。！？!?;；\n]{1,80}为(?:出发地|起点))[。！？!?\s]*$')


def validate_required_origin(text, tool_name, arguments, recent_user_texts=()):
    """Refuse a literal nearest-airport action with no stated origin.

    This covers the location-free navigation forms above, not arbitrary place
    recognition or route planning. A directly preceding explicit user origin
    is sufficient; mentions in research/pages are not a location source.
    """
    if tool_name not in {'delegate_task', 'set_navigation_destination', 'search_places', 'start_poi_navigation'} or not isinstance(text, str):
        return None
    match = _NEAREST_AIRPORT.fullmatch(text)
    if not match:
        return None
    preceding = list(recent_user_texts)[-1:]
    if any(isinstance(value, str) and _EXPLICIT_ORIGIN.fullmatch(value) for value in preceding):
        return None
    return {'kind': 'missing_required_origin', 'executed': False, 'retry_model': True,
            'needs_clarification': False, 'required_condition': '出发城市或地址',
            'evidence': [{'start': match.start(), 'end': match.end(), 'text': match.group(),
                          'meaning': 'nearest_airport_requires_origin'}],
            'reason': '没有可用GPS或用户提供的出发城市/地址，不能查询或选择最近机场、登记任务或启动导航。工具参数中的猜测地点和网页/POI结果不构成用户提供的出发地。请直接只问用户现在在哪个城市或地址，不承诺已接手导航。',
            'spoken_reason': '你现在在哪个城市或地址？'}
