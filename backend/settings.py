import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
TALKER_MODEL = os.getenv('TALKER_MODEL', 'doubao-seed-2-0-mini-260428')
THINKER_MODEL = os.getenv('THINKER_MODEL', 'doubao-seed-2-1-pro-260915')
VEHICLE_CONTROL_MODEL = os.getenv('VEHICLE_CONTROL_MODEL', 'doubao-seed-2-1-pro-260915')
DATABASE_PATH = os.getenv('DATABASE_PATH', str(ROOT / 'data' / 'super_xd.sqlite3'))
MODEL_TIMEOUT = float(os.getenv('MODEL_TIMEOUT_SECONDS', '45'))
TOOL_TIMEOUT = float(os.getenv('TOOL_TIMEOUT_SECONDS', '12'))
