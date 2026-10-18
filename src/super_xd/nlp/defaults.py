import os
from super_xd.nlp.resources import RESOURCE_ROOT

ROOT = RESOURCE_ROOT
TALKER_MODEL = os.getenv('TALKER_MODEL', 'doubao-seed-2-0-mini-260428')
THINKER_MODEL = os.getenv('THINKER_MODEL', 'doubao-seed-2-1-pro-260915')
VEHICLE_CONTROL_MODEL = os.getenv('VEHICLE_CONTROL_MODEL', 'doubao-seed-2-1-pro-260915')
MODEL_TIMEOUT = float(os.getenv('MODEL_TIMEOUT_SECONDS', '45'))
TOOL_TIMEOUT = float(os.getenv('TOOL_TIMEOUT_SECONDS', '12'))
DEFAULT_SPEAKER_SEAT = os.getenv('DEFAULT_SPEAKER_SEAT', 'driver')
