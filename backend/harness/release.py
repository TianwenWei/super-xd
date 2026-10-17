"""Startup fingerprint of the NLP release; no environment or credential reads.

This identifies deployed files, not resolved provider settings or injected test
dependencies. Keep actual provider/model metadata in traces. Deploy an immutable
directory and restart every process together; a changed release is incompatible
with an existing AgentRun checkpoint unless an explicit migration is supplied.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path


MANIFEST_VERSION = 1
SOURCE_FILES = (
    'backend/__init__.py', 'backend/catalog.py', 'backend/environment_context.py',
    'backend/model_context.py', 'backend/models.py', 'backend/nlp_app.py', 'backend/nlp_settings.py',
    'backend/persona.py', 'backend/prompt_config.py', 'backend/prompt_templates.py', 'backend/settings.py',
    'backend/speaker.py', 'backend/speech_config.py', 'backend/speech_text.py',
    'backend/task_feedback.py', 'backend/textutil.py', 'backend/tool_text_config.py',
    'backend/types.py', 'backend/google_maps.py', 'backend/weather.py',
    'backend/services/__init__.py', 'backend/services/config.py', 'backend/services/contracts.py',
    'backend/services/factory.py', 'backend/services/google_auth.py', 'backend/services/llm.py',
    'backend/services/reads.py', 'backend/services/regions.py', 'backend/services/routes.py',
)
SOURCE_DIRECTORIES = ('backend/harness', 'backend/cockpit', 'backend/tool_schemas')

REQUIRED_PROMPTS = ('prompts/talker.md', 'prompts/reasoner_foreground.md',
                    'prompts/reasoner_background.md', 'prompts/talker/examples.json')


def _safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'Release inputs must not be symlinks: {relative}')
    if not path.is_file():
        raise FileNotFoundError(f'Missing release input: {relative}')
    return path


def build_release_manifest(root: Path) -> dict:
    """Build offline or at startup. Request handlers use the cached getters."""
    root = Path(root).resolve()
    paths = set(SOURCE_FILES) | set(REQUIRED_PROMPTS) | {'config/services.toml', 'config/regions.toml'}
    for directory, suffixes in [('prompts', {'.md', '.toml', '.json'}),
                                 *((directory, {'.py'}) for directory in SOURCE_DIRECTORIES)]:
        folder = root / directory
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f'Missing or symlinked release directory: {directory}')
        for path in folder.rglob('*'):
            relative = path.relative_to(root)
            if (path.suffix in suffixes and path.name != 'README.md'
                    and not any(part.startswith('.') for part in relative.parts)):
                paths.add(relative.as_posix())
    files = {relative: hashlib.sha256(_safe_file(root, relative).read_bytes()).hexdigest()
             for relative in sorted(paths)}
    manifest = {'manifest_version': MANIFEST_VERSION, 'algorithm': 'sha256', 'files': files}
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'release_id': 'nlp-' + hashlib.sha256(encoded).hexdigest(), **manifest}


# No imports of prompt/config/business modules: these modules also import this
# fingerprint while registering their own startup contracts.
_MANIFEST = build_release_manifest(Path(__file__).resolve().parents[2])


def get_release_id() -> str:
    return _MANIFEST['release_id']


def get_release_manifest() -> dict:
    """Return an independent snapshot without reading files or environment."""
    return deepcopy(_MANIFEST)
