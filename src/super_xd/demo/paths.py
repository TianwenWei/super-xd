"""Resolve writable Demo paths without freezing configuration before .env loads."""
import os
from pathlib import Path

def workspace_root() -> Path:
    # Bootstrap must not import production resources before local .env is read.
    checkout = Path(__file__).resolve().parents[3]
    default = checkout if (checkout / 'pyproject.toml').is_file() else Path.cwd()
    return Path(os.environ.get('SUPER_XD_WORKSPACE', str(default))).expanduser().resolve()
