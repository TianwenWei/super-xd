import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TALKER_MODEL = os.getenv('TALKER_MODEL', 'doubao-seed-2-0-mini-260428')
THINKER_MODEL = os.getenv('THINKER_MODEL', 'doubao-seed-2-1-pro-260915')
VEHICLE_CONTROL_MODEL = os.getenv('VEHICLE_CONTROL_MODEL', 'doubao-seed-2-1-pro-260915')
DATABASE_PATH = os.getenv('DATABASE_PATH', str(ROOT / 'data' / 'super_xd.sqlite3'))
MODEL_TIMEOUT = float(os.getenv('MODEL_TIMEOUT_SECONDS', '45'))
TOOL_TIMEOUT = float(os.getenv('TOOL_TIMEOUT_SECONDS', '12'))
DEFAULT_SPEAKER_SEAT = os.getenv('DEFAULT_SPEAKER_SEAT', 'driver')


def environment_defaults():
    zone = os.getenv('DEFAULT_TIMEZONE', 'UTC')
    ZoneInfo(zone)
    return {'location': os.getenv('DEFAULT_LOCATION', '').strip(),
            'location_country': os.getenv('DEFAULT_LOCATION_COUNTRY', '').strip(),
            'timezone': zone}


def allowed_origins():
    from urllib.parse import urlparse
    origins = {'http://localhost:5173', 'http://127.0.0.1:5173',
               'http://localhost:8000', 'http://127.0.0.1:8000'}
    for key in ('FRONTEND_ORIGIN', 'API_ORIGIN'):
        value = os.getenv(key)
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError(f'{key} must be an exact HTTP(S) origin')
            origins.add(value)
    return origins
