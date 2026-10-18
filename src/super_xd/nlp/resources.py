"""Locate immutable application resources in an editable checkout or installed wheel."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CHECKOUT_ROOT = PACKAGE_ROOT.parent.parent


def resource_root() -> Path:
    explicit = os.environ.get('SUPER_XD_RESOURCE_ROOT')
    if explicit:
        root = Path(explicit).expanduser().resolve()
    elif (PACKAGE_ROOT / 'resources').is_dir():
        root = PACKAGE_ROOT / 'resources'
    elif (CHECKOUT_ROOT / 'pyproject.toml').is_file() and PACKAGE_ROOT.parent.name == 'src':
        root = CHECKOUT_ROOT
    else:
        raise FileNotFoundError('Super XD resources are missing from the installed package')
    for required in ('prompts', 'config'):
        if not (root / required).is_dir():
            raise FileNotFoundError(f'Missing Super XD resource directory: {root / required}')
    return root


RESOURCE_ROOT = resource_root()
PROMPT_ROOT = RESOURCE_ROOT / 'prompts'
CONFIG_ROOT = RESOURCE_ROOT / 'config'
