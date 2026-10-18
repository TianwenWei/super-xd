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


from super_xd.nlp.resources import PACKAGE_ROOT, RESOURCE_ROOT
from super_xd.nlp.prompts.templates import PROMPT_RESOURCE_FILES

MANIFEST_VERSION = 2
SOURCE_PREFIX = 'src/super_xd/'
SOURCE_FILES = (SOURCE_PREFIX + '__init__.py',)
SOURCE_DIRECTORIES = (SOURCE_PREFIX + 'nlp',)

REQUIRED_PROMPTS = tuple('prompts/' + path for path in sorted(PROMPT_RESOURCE_FILES))


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


def build_release_manifest(root: Path, *, package_root: Path | None = None) -> dict:
    """Hash the same logical inputs in a checkout and an installed wheel."""
    root = Path(root).resolve()
    package_root = Path(package_root or root / 'src/super_xd').resolve()
    paths = set(SOURCE_FILES) | set(REQUIRED_PROMPTS) | {'config/services.toml', 'config/regions.toml'}
    folders = [(root / 'prompts', root, '', {'.md', '.toml', '.json'}),
               (package_root / 'nlp', package_root, SOURCE_PREFIX, {'.py'})]
    for folder, base, prefix, suffixes in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f'Missing or symlinked release directory: {folder.name}')
        for path in folder.rglob('*'):
            relative = path.relative_to(base)
            if (path.suffix in suffixes and path.name != 'README.md'
                    and not any(part.startswith('.') for part in relative.parts)):
                paths.add(prefix + relative.as_posix())
    files = {}
    for relative in sorted(paths):
        if relative.startswith(SOURCE_PREFIX):
            path = _safe_file(package_root, relative.removeprefix(SOURCE_PREFIX))
        else:
            path = _safe_file(root, relative)
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'manifest_version': MANIFEST_VERSION, 'algorithm': 'sha256', 'files': files}
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'release_id': 'nlp-' + hashlib.sha256(encoded).hexdigest(), **manifest}


_MANIFEST = build_release_manifest(RESOURCE_ROOT, package_root=PACKAGE_ROOT)


def get_release_id() -> str:
    return _MANIFEST['release_id']


def get_release_manifest() -> dict:
    """Return an independent snapshot without reading files or environment."""
    return deepcopy(_MANIFEST)
