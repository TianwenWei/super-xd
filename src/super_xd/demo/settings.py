"""Local Demo paths and browser settings, independent of production configuration."""
import os
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from super_xd.demo.paths import workspace_root

ROOT = workspace_root()
DATABASE_PATH = os.getenv('DATABASE_PATH', str(ROOT / 'data/super_xd.sqlite3'))


def environment_defaults():
    zone = os.getenv('DEFAULT_TIMEZONE', 'UTC')
    ZoneInfo(zone)
    return {'location': os.getenv('DEFAULT_LOCATION', '').strip(),
            'location_country': os.getenv('DEFAULT_LOCATION_COUNTRY', '').strip(),
            'timezone': zone}


def allowed_origins():
    origins = {'http://localhost:5173', 'http://127.0.0.1:5173',
               'http://localhost:8000', 'http://127.0.0.1:8000'}
    for key in ('FRONTEND_ORIGIN', 'API_ORIGIN'):
        value = os.getenv(key)
        if value:
            parsed = urlparse(value)
            if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment):
                raise ValueError(f'{key} must be an exact HTTP(S) origin')
            origins.add(value)
    return origins
