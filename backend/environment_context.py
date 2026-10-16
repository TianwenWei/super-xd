"""User-configured location and a live clock, without inferred geolocation."""
from datetime import datetime
from zoneinfo import ZoneInfo


def environment_context(vehicle, current_utc):
    meta = vehicle['_meta']
    now = datetime.fromisoformat(current_utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('当前时间必须包含时区。')
    local = now.astimezone(ZoneInfo(meta['timezone']))
    name = meta.get('location', '')
    location = {'name': name, 'country': meta.get('location_country', ''), 'source': 'user_setting'} if name else None
    return {'current_location': location, 'current_utc': current_utc,
            'current_local': local.isoformat(timespec='seconds'), 'current_date': local.date().isoformat(),
            'timezone': meta['timezone']}
