"""Composition of the local cockpit, audio and SQLite demo services."""
from dataclasses import dataclass
import json
import os
from pathlib import Path

from super_xd.demo.settings import ROOT
from super_xd.nlp.config.services import ServiceConfig, api_key, load_services_config, resolved
from super_xd.nlp.adapters.factory import create_llm, create_search, create_maps, create_routes, resolved_profile
from super_xd.demo.resources.runtime import LocalResources


def create_embedding_provider(config=None):
    from super_xd.demo.resources.embedding import build_embedding
    config = config or load_services_config()
    return build_embedding(resolved_profile(config.profile('embedding')))


def create_cockpit(config=None, *, routes=None):
    from super_xd.demo.devices.cockpit import CockpitServices
    from super_xd.nlp.adapters.media.catalogs import HTTPMediaGateway
    from super_xd.demo.resources.media import DemoMediaServices
    from super_xd.nlp.adapters.media.base import TencentMusicGateway, DeviceMediaCatalog
    config = config or load_services_config()
    files, providers, shared = {}, {}, {}
    bindings = config.media_bindings()
    for kind, profile in config.media_profiles().items():
        name, provider = bindings[kind], profile['provider']
        if provider == 'media_gateway':
            catalog = HTTPMediaGateway(provider_name=profile.get('provider_name', name), kind=kind,
                                       base_url=resolved(profile, 'base_url'), api_key=api_key(profile),
                                       timeout_seconds=profile.get('timeout_seconds', 12),
                                       require_api_key=profile.get('require_api_key', True))
        elif name in shared:
            catalog = shared[name]
        elif provider == 'tencent_gateway':
            catalog = TencentMusicGateway(base_url=resolved(profile, 'base_url'), api_key=api_key(profile),
                                          timeout_seconds=profile.get('timeout_seconds', 12),
                                          require_api_key=profile.get('require_api_key', True))
        elif provider == 'device_catalog':
            catalog = DeviceMediaCatalog()
        else:
            catalog, local_files = load_local_media(profile)
            for identifier, path in local_files.items():
                if identifier in files and files[identifier] != path:
                    raise ValueError('Local media file IDs must be unique across catalogs')
                files[identifier] = path
        shared[name] = catalog
        providers[kind] = catalog
    return CockpitServices(DemoMediaServices(providers, bindings), routes), files


def load_local_media(profile):
    from super_xd.demo.resources.media import LocalMediaCatalog
    files = {}
    entries = []
    path = Path(profile.get('catalog_path', 'data/media/catalog.json'))
    path = path if path.is_absolute() else ROOT / path
    if path.exists():
        with path.open(encoding='utf-8') as source:
            document = json.load(source)
        if not isinstance(document, dict) or set(document) != {'items'} or not isinstance(document['items'], list):
            raise ValueError('Local media catalog must contain only an items array')
        media_root = Path(os.getenv('MEDIA_ROOT', str(ROOT / 'data/media'))).resolve()
        for original in document['items']:
            item = dict(original)
            file_name = item.pop('file_path', None)
            if file_name is not None:
                target = (media_root / file_name).resolve()
                if not target.is_relative_to(media_root) or not target.is_file() or target.suffix.lower() not in {
                        '.wav', '.mp3', '.ogg', '.m4a', '.flac', '.mp4', '.webm'}:
                    raise ValueError('Local media files must be existing supported files inside MEDIA_ROOT')
                identifier = item.get('content_id')
                if not isinstance(identifier, str) or not identifier or any(c in identifier for c in '/?#'):
                    raise ValueError('Local media file content_id cannot contain path/query characters')
                files[identifier] = target
                item['source_url'] = '/api/media/files/' + identifier
            entries.append(item)
    return LocalMediaCatalog(entries), files


@dataclass
class ServiceBundle:
    config: ServiceConfig
    llm: object
    asr: object
    tts: object
    maps: object
    web: object
    embedding: object
    manual: object
    cockpit: object
    routes: object
    media_files: dict

    def info(self):
        return {'llm': self.llm.info(), 'asr': self.asr.info(), 'tts': self.tts.info(),
                'places_weather': self.maps.info(), 'web_search': self.web.info(),
                'embedding': self.embedding.info(), 'manual': self.manual.info(),
                'cockpit': self.cockpit.info(), 'capabilities': self.capabilities(),
                'config_source': self.config.source}

    def capabilities(self):
        resources = LocalResources(maps=self.maps, web=self.web, manual=self.manual, cockpit=self.cockpit)
        capabilities, _ = resources.declarations()
        return [item.model_dump() for item in capabilities]

    async def aclose(self):
        # Shared resources are closed once; request cancellation closes only streams.
        seen = set()
        for resource in (self.cockpit, self.routes, self.manual, self.embedding,
                         self.web, self.maps, self.tts, self.asr, self.llm):
            if id(resource) not in seen:
                seen.add(id(resource))
                await resource.aclose()


def create_services(config=None):
    from super_xd.demo.audio.providers import build_asr, build_tts
    from super_xd.demo.resources.manual import build_manual
    config = config or load_services_config()
    llm, asr, tts = create_llm(config), build_asr(resolved_profile(config.profile('asr'))), build_tts(config.profile('tts'))
    maps, web, embedding = create_maps(config), create_search(config), create_embedding_provider(config)
    routes = create_routes(config)
    cockpit, files = create_cockpit(config, routes=routes)
    manual = build_manual(resolved_profile(config.profile('manual')), embeddings=embedding)
    return ServiceBundle(config, llm, asr, tts, maps, web, embedding, manual, cockpit, routes, files)


@dataclass
class WorkerServices:
    """The background task process only owns model and read-service clients."""
    llm: object
    web: object
    maps: object

    async def aclose(self):
        for resource in (self.maps, self.web, self.llm):
            await resource.aclose()


def create_worker_services(config=None):
    config = config or load_services_config()
    return WorkerServices(create_llm(config), create_search(config), create_maps(config))
