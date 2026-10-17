"""Offline production import and startup checks, including a core-only environment."""
from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ('backend.demo', 'backend.session', 'backend.worker', 'backend.db', 'backend.app',
             'backend.asr', 'backend.tts', 'backend.speech_stream', 'backend.manual_search',
             'backend.manual_embeddings', 'backend.web_search', 'backend.local_environment',
             'numpy', 'sqlite3', 'dotenv', 'onnxruntime', 'websockets')


def forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + '.') for prefix in FORBIDDEN)


def check_import_boundaries() -> None:
    """AST includes dormant imports; runtime checks include transitive imports."""
    paths = [ROOT / 'backend' / name for name in ('nlp_app.py', 'nlp_settings.py', 'catalog.py')]
    paths += list((ROOT / 'backend/harness').glob('*.py')) + list((ROOT / 'backend/tool_schemas').glob('*.py'))
    for path in paths:
        module = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    base = importlib.util.resolve_name('.' * node.level + base, module.rpartition('.')[0])
                names = [base, *(base + '.' + item.name for item in node.names)]
            if any(forbidden(name) for name in names):
                raise AssertionError(f'{path.relative_to(ROOT)}:{node.lineno}: production imports a Demo dependency')

    class Boundary:
        def find_spec(self, fullname, path=None, target=None):
            if forbidden(fullname):
                raise AssertionError('Production import reached: ' + fullname)
            return None

    guard = Boundary()
    sys.meta_path.insert(0, guard)
    try:
        import backend.nlp_app
        import backend.harness.assembly
        from backend.harness.talker import talker_tools
        from backend.prompt_config import validate_all
        validate_all()
        assert len(talker_tools()) == 26
        assert not any(forbidden(name) for name in sys.modules)
    finally:
        sys.meta_path.remove(guard)


def check_release_coverage() -> None:
    from backend.harness.release import get_release_manifest
    files = get_release_manifest()['files']
    missing = []
    for name, module in tuple(sys.modules.items()):
        if name == 'backend' or name.startswith('backend.'):
            path = getattr(module, '__file__', None)
            if path and Path(path).suffix == '.py':
                relative = Path(path).resolve().relative_to(ROOT).as_posix()
                if relative not in files:
                    missing.append(relative)
    assert not missing, 'Imported production source missing from release: ' + ', '.join(sorted(missing))


async def smoke_startup() -> None:
    """Open real local clients with offline credentials; ASGI checks perform no upstream requests."""
    import os
    import httpx
    from backend.harness.assembly import create_nlp_services
    from backend.nlp_app import create_app
    from backend.nlp_settings import NLPSettings
    from backend.services.config import load_services_config

    config = load_services_config(ROOT / 'config/services.toml')
    for profile in config.document['llm']['profiles'].values():
        if profile.get('key_env'):
            os.environ[profile['key_env']] = 'offline-startup-fixture'
    settings = NLPSettings(gateway_url='http://127.0.0.1:9')
    bundle = await create_nlp_services(config, settings=settings)

    async def factory():
        return bundle

    app = create_app(bundle_factory=factory, settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://nlp.test') as client:
            assert (await client.get('/health/live')).status_code == 200
            readiness = await client.get('/health/ready')
            assert readiness.status_code == 200 and readiness.json()['gateway'] == 'not_probed'
            assert (await client.get('/metrics')).json()['models'] == {}
    assert bundle.closed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke-startup', action='store_true')
    options = parser.parse_args()
    check_import_boundaries()
    if options.smoke_startup:
        asyncio.run(smoke_startup())
    check_release_coverage()
    print('Production imports, 26 tools and release coverage passed' + ('; offline startup passed' if options.smoke_startup else ''))
