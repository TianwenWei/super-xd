"""Control-message validation and session capture leases for demo audio."""
import json

from super_xd.demo.settings import allowed_origins

ALLOWED_ORIGINS = allowed_origins()
active_captures = {}


class ASRInputError(ValueError):
    pass


def control_message(event):
    text = event.get('text')
    if not isinstance(text, str) or len(text) > 4096:
        raise ASRInputError('无效语音控制消息。')
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        raise ASRInputError('语音控制消息不是有效JSON。') from None
    if not isinstance(body, dict):
        raise ASRInputError('语音控制消息必须是对象。')
    return body
