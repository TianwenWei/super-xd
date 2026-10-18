"""Offline production import and startup checks, including a core-only environment."""
from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
from pathlib import Path
import sys

from super_xd.nlp.resources import CONFIG_ROOT, PACKAGE_ROOT

FORBIDDEN = ('super_xd.demo', 'numpy', 'sqlite3', 'dotenv', 'onnxruntime', 'websockets')
LAYER_RULES = {
    'contracts': ('engine', 'orchestration', 'policy', 'adapters', 'prompts', 'api', 'assembly'),
    'catalog': ('orchestration', 'policy', 'adapters', 'prompts', 'api', 'assembly'),
    'engine': ('orchestration', 'policy', 'adapters', 'prompts', 'api', 'assembly'),
    'policy': ('orchestration', 'adapters', 'api', 'assembly'),
}


def forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + '.') for prefix in FORBIDDEN)


def check_static_boundaries(package_root: Path = PACKAGE_ROOT) -> None:
    """Check every production source, including imports inside dormant branches."""
    for path in (package_root / 'nlp').rglob('*.py'):
        relative = path.relative_to(package_root)
        module = 'super_xd.' + '.'.join(relative.with_suffix('').parts).removesuffix('.__init__')
        layer = relative.parts[1] if len(relative.parts) > 2 else ''
        blocked = tuple('super_xd.nlp.' + name for name in LAYER_RULES.get(layer, ()))
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    package = module if path.name == '__init__.py' else module.rpartition('.')[0]
                    base = importlib.util.resolve_name('.' * node.level + base, package)
                names = [base, *(base + '.' + item.name for item in node.names)]
            if any(forbidden(name) for name in names):
                raise AssertionError(f'{relative}:{node.lineno}: production imports a Demo dependency')
            if any(name == prefix or name.startswith(prefix + '.') for name in names for prefix in blocked):
                raise AssertionError(f'{relative}:{node.lineno}: forbidden {layer} dependency: {names}')
            if layer in LAYER_RULES and any(name.split('.')[0] in {'httpx', 'requests', 'aiohttp', 'socket'} for name in names):
                raise AssertionError(f'{relative}:{node.lineno}: IO belongs in an adapter')


def check_import_boundaries() -> None:
    """Runtime guarding also covers transitive imports and startup registrations."""
    check_static_boundaries()

    class Boundary:
        def find_spec(self, fullname, path=None, target=None):
            if forbidden(fullname):
                raise AssertionError('Production import reached: ' + fullname)
            return None

    guard = Boundary()
    sys.meta_path.insert(0, guard)
    try:
        import super_xd.nlp.api.app
        import super_xd.nlp.assembly
        from super_xd.nlp.orchestration.talker import talker_tools
        from super_xd.nlp.prompts.config import validate_all
        validate_all()
        assert len(talker_tools()) == 26
        assert not any(forbidden(name) for name in sys.modules)
    finally:
        sys.meta_path.remove(guard)


def check_release_coverage() -> None:
    from super_xd.nlp.release import get_release_manifest
    files = get_release_manifest()['files']
    missing = []
    for name, module in tuple(sys.modules.items()):
        if name == 'super_xd' or name.startswith('super_xd.'):
            path = getattr(module, '__file__', None)
            if path and Path(path).suffix == '.py':
                relative = 'src/super_xd/' + Path(path).resolve().relative_to(PACKAGE_ROOT).as_posix()
                if relative not in files:
                    missing.append(relative)
    assert not missing, 'Imported production source missing from release: ' + ', '.join(sorted(missing))


async def smoke_startup() -> None:
    """Open real local clients with offline credentials; ASGI checks perform no upstream requests."""
    import os
    import httpx
    from super_xd.nlp.assembly import create_nlp_services
    from super_xd.nlp.api.app import create_app
    from super_xd.nlp.settings import NLPSettings
    from super_xd.nlp.config.services import load_services_config

    config = load_services_config(CONFIG_ROOT / 'services.toml')
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
