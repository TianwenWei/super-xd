"""Factories for provider adapters used by the NLP service."""
from super_xd.nlp.config.services import api_key, load_services_config, resolved
from super_xd.nlp.adapters.models.service import ModelService
from super_xd.nlp.adapters.reads.services import DevicePlacesProvider, GooglePlacesProvider, GoogleWeatherProvider, HTTPGateway, MapsServices, OpenMeteoProvider, SearchServices, TavilyProvider
from super_xd.nlp.adapters.reads.routes import DisabledRoutesProvider, GatewayRoutesProvider, GoogleRoutesProvider


def resolved_profile(profile):
    for name in ('base_url', 'model', 'url'):
        value = resolved(profile, name)
        if value:
            profile[name] = value
    return profile


def create_llm(config=None):
    return ModelService(config or load_services_config())


def create_search(config=None):
    config = config or load_services_config()
    search_profile, content_profile = config.profile('web_search'), config.profile('web_content')
    def build(profile, domain):
        return TavilyProvider(profile) if profile['provider'] == 'tavily' else HTTPGateway(profile, domain=domain)
    search = build(search_profile, 'web_search')
    content = search if search_profile == content_profile else build(content_profile, 'web_content')
    return SearchServices(search, content)


def create_maps(config=None):
    from super_xd.nlp.adapters.reads.google_maps import GoogleMapsClient
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
