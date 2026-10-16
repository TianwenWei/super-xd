"""Composition root for API and worker; services own pools for their process."""
from dataclasses import dataclass
import json
import os
from pathlib import Path

from backend.settings import ROOT
from .config import ServiceConfig, api_key, load_services_config, resolved
from .contracts import ToolCapability
from .llm import LLMService
from .reads import (DevicePlacesProvider, GooglePlacesProvider, GoogleWeatherProvider, HTTPGateway,
                    MapsServices, OpenMeteoProvider, SearchServices, TavilyProvider)
from .routes import DisabledRoutesProvider, GatewayRoutesProvider, GoogleRoutesProvider


def resolved_profile(profile):
    for name in ('base_url', 'model', 'url'):
        value = resolved(profile, name)
        if value:
            profile[name] = value
    return profile


def create_embedding_provider(config=None):
    from .embedding import build_embedding
    config = config or load_services_config()
    return build_embedding(resolved_profile(config.profile('embedding')))


def create_llm(config=None):
    return LLMService(config or load_services_config())


def create_search(config=None):
    config = config or load_services_config()
    search_profile, content_profile = config.profile('web_search'), config.profile('web_content')
    def build(profile, domain):
        return TavilyProvider(profile) if profile['provider'] == 'tavily' else HTTPGateway(profile, domain=domain)
    search = build(search_profile, 'web_search')
    content = search if search_profile == content_profile else build(content_profile, 'web_content')
    return SearchServices(search, content)


def create_maps(config=None):
    from backend.google_maps import GoogleMapsClient
    config = config or load_services_config()
    places_profile, weather_profile = config.profile('places'), config.profile('weather')
    owned = []
    google = None
    if places_profile['provider'] == 'google':
        google = GoogleMapsClient(api_key=api_key(places_profile), timeout_seconds=places_profile.get('timeout_seconds', 10))
        owned.append(google)
        places = GooglePlacesProvider(google)
    elif places_profile['provider'] == 'device_gateway':
        places = DevicePlacesProvider()
    else:
        places = HTTPGateway(places_profile, domain='places')
    if weather_profile['provider'] == 'google':
        fallback = weather_profile.get('coverage_fallback', 'open_meteo')
        if fallback not in {'open_meteo', 'none'}:
            raise ValueError('Weather coverage_fallback must be open_meteo or none')
        if (google is None or google.key != api_key(weather_profile) or fallback == 'none'
                or places_profile.get('timeout_seconds', 10) != weather_profile.get('timeout_seconds', 10)):
            google = GoogleMapsClient(api_key=api_key(weather_profile), coverage_fallback=fallback == 'open_meteo',
                                      timeout_seconds=weather_profile.get('timeout_seconds', 10))
            owned.append(google)
        weather = GoogleWeatherProvider(google, fallback=fallback)
    elif weather_profile['provider'] == 'open_meteo':
        weather = OpenMeteoProvider()
    else:
        weather = HTTPGateway(weather_profile, domain='weather')
    return MapsServices(places, weather, owned=tuple(owned))


def create_routes(config=None):
    profile = (config or load_services_config()).profile('routes')
    if profile['provider'] == 'none':
        return DisabledRoutesProvider()
    if profile['provider'] == 'google':
        return GoogleRoutesProvider(profile)
    return GatewayRoutesProvider(profile)


def create_cockpit(config=None, *, routes=None):
    from backend.cockpit import CockpitServices, HTTPMediaGateway, MediaServices, TencentMusicGateway
    from backend.cockpit.device import DeviceMediaCatalog
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
    return CockpitServices(MediaServices(providers, bindings), routes), files


def load_local_media(profile):
    from backend.cockpit import LocalMediaCatalog
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
        media = self.cockpit.media
        values = [ToolCapability('search_media', 'query', media.provider_info(kind).get('execution_location', 'cloud'),
                                 'media', content_kind=kind) for kind in ('music', 'video', 'radio')]
        values += [ToolCapability(name, 'query', location, domain) for name, location, domain in (
            ('web_search', 'cloud', 'web'), ('search_manual', 'cloud', 'manual'),
            ('search_places', self.maps.info().get('execution_location', 'cloud'), 'places'),
            ('get_weather', 'cloud', 'weather'))]
        values += [ToolCapability('control_media', 'query' if action == 'get_status' else 'control', 'device', 'media', action=action)
                   for action in ('play', 'pause', 'resume', 'stop', 'exit', 'next', 'previous', 'seek', 'volume', 'get_status')]
        values += [ToolCapability('manage_navigation', 'query' if action in {'get_status', 'route'} else 'control',
                                 'cloud' if action in {'get_status', 'route'} else 'device', 'navigation', action=action)
                   for action in ('get_status', 'route', 'start', 'set_origin', 'add_waypoint', 'remove_waypoint', 'cancel')]
        from backend.catalog import TOOLS
        values += [ToolCapability(name, 'query' if spec.state_mapping.get('operation') == 'read' else 'control',
                                  'device', 'vehicle') for name, spec in TOOLS.items()]
        return [item.to_dict() for item in values]

    async def aclose(self):
        # Shared resources are closed once; request cancellation closes only streams.
        seen = set()
        for resource in (self.cockpit, self.routes, self.manual, self.embedding,
                         self.web, self.maps, self.tts, self.asr, self.llm):
            if id(resource) not in seen:
                seen.add(id(resource))
                await resource.aclose()


def create_services(config=None):
    from .audio import build_asr, build_tts
    from .manual import build_manual
    config = config or load_services_config()
    llm, asr, tts = create_llm(config), build_asr(resolved_profile(config.profile('asr'))), build_tts(config.profile('tts'))
    maps, web, embedding = create_maps(config), create_search(config), create_embedding_provider(config)
    routes = create_routes(config)
    cockpit, files = create_cockpit(config, routes=routes)
    manual = build_manual(resolved_profile(config.profile('manual')), embeddings=embedding)
    return ServiceBundle(config, llm, asr, tts, maps, web, embedding, manual, cockpit, routes, files)
