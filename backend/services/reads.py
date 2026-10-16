"""Read-service adapters retain real provider evidence and never execute device actions."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta
import math
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from backend.google_maps import MapsError
from backend.models import TavilyClient, utcnow
from backend.speech_config import feedback_text
from backend.textutil import is_alpha2, is_domain
from backend.weather import WeatherClient
from .config import api_key, resolved, validate_url
from .contracts import ProviderError, redact


def _http_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _date(value):
    try:
        return isinstance(value, str) and len(value) == 10 and Date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _weather_fields(record):
    if not isinstance(record, dict):
        return False
    low, high = record.get('temperature_min'), record.get('temperature_max')
    try:
        ZoneInfo(record['timezone'])
        queried = datetime.fromisoformat(record['queried_at'])
        return (_date(record.get('date')) and record.get('unit') == '°C'
                and all(type(value) in (int, float) and math.isfinite(value) for value in (low, high))
                and low <= high and queried.tzinfo is not None and queried.utcoffset() is not None
                and _http_url(record.get('source_url')))
    except (KeyError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class WeatherReceipt:
    """Evidence is the response actually received, not a generated weather summary."""
    provider: str
    source_format: str
    response: dict

    def to_dict(self):
        return {'schema': 'weather_receipt.v1', 'provider': self.provider,
                'source_format': self.source_format, 'response': deepcopy(self.response)}


def _receipt_values(raw):
    response = raw.get('response')
    if not isinstance(response, dict):
        raise ValueError('Weather evidence has no provider response')
    source = raw.get('source_format')
    if source == 'gateway_forecast':
        if response.get('ok') is not True or response.get('data_type') != 'forecast':
            raise ValueError('Gateway did not return a forecast')
        return {key: deepcopy(response[key]) for key in ('date', 'location', 'timezone', 'temperature_min',
                'temperature_max', 'unit', 'source_url', 'queried_at')}
    if source in {'google_forecast', 'google_fallback_forecast'}:
        if response.get('ok') is not True or response.get('mode') != 'forecast':
            raise ValueError('Google adapter evidence is not a daily forecast')
        details = response['details']
        if source == 'google_forecast':
            if details['minTemperature']['unit'] != 'CELSIUS' or details['maxTemperature']['unit'] != 'CELSIUS':
                raise ValueError('Google forecast is not in Celsius')
            date = Date(**details['displayDate']).isoformat()
            low, high = details['minTemperature']['degrees'], details['maxTemperature']['degrees']
        else:
            units = response['units']
            if units.get('temperature_2m_min') != '°C' or units.get('temperature_2m_max') != '°C':
                raise ValueError('Coverage fallback is not in Celsius')
            date, low, high = details['time'], details['temperature_2m_min'], details['temperature_2m_max']
        if response.get('date') != date or response['summary'].get('min_c') != low or response['summary'].get('max_c') != high:
            raise ValueError('Provider summary differs from actual forecast values')
        return {'date': date, 'location': deepcopy(response['place']), 'timezone': response['timezone'],
                'temperature_min': low, 'temperature_max': high, 'unit': '°C',
                'source_url': response['source_url'], 'queried_at': response['queried_at']}
    raise ValueError('Unsupported weather evidence format')


def verify_forecast_record(record, raw_forecast):
    """Validate typed evidence; keep legacy OM checkpoints readable during migration."""
    if not isinstance(record, dict) or not isinstance(raw_forecast, dict):
        return False
    if raw_forecast.get('schema') == 'weather_receipt.v1':
        try:
            values = _receipt_values(raw_forecast)
            return (_weather_fields(values) and _weather_fields(record)
                    and all(record.get(key) == value for key, value in values.items()))
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
    # Existing persisted observations contain the original OM daily response.
    low, high = record.get('temperature_min'), record.get('temperature_max')
    daily, units = raw_forecast.get('daily') or {}, raw_forecast.get('daily_units') or {}
    return (isinstance(daily, dict) and isinstance(units, dict) and _date(record.get('date'))
            and all(type(value) in (int, float) and math.isfinite(value) for value in (low, high))
            and low <= high and record.get('unit') == '°C' and bool(record.get('timezone'))
            and raw_forecast.get('timezone') == record['timezone'] and daily.get('time') == [record['date']]
            and daily.get('temperature_2m_min') == [low] and daily.get('temperature_2m_max') == [high]
            and units.get('temperature_2m_min') == units.get('temperature_2m_max') == '°C')


def _failure(exc, provider, secrets=()):
    if isinstance(exc, ProviderError):
        return exc
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else getattr(exc, 'status_code', None)
    typed = getattr(exc, 'code', getattr(exc, 'provider_code', None))
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or typed == 'timeout':
        code = 'timeout'
    elif status in (401, 403) or typed in {'permission_denied', 'auth'}:
        code = 'auth'
    elif status == 429 or typed == 'rate_limit':
        code = 'rate_limit'
    elif isinstance(exc, (httpx.RequestError, OSError)) or typed in {'connection_error', 'upstream_error', 'unavailable'} or isinstance(status, int) and status >= 500:
        code = 'unavailable'
    elif typed == 'not_configured':
        code = 'not_configured'
    elif typed in {'unsupported_location', 'date_out_of_range', 'date_unavailable'}:
        code = 'unsupported_capability'
    else:
        code = 'invalid_response'
    message = redact(str(exc), secrets)
    diagnostic = redact(getattr(exc, 'full_diagnostic', str(exc)), secrets)
    return ProviderError(code, message or 'Read service request failed', provider=provider,
                         retryable=code in {'timeout', 'rate_limit', 'unavailable'},
                         details={'status_code': status, 'provider_code': typed, 'exception_type': type(exc).__name__, 'diagnostic': diagnostic})


def _search_rows(result, provider, *, extracted=False):
    if not isinstance(result, dict) or not isinstance(result.get('results'), list):
        raise ProviderError('invalid_response', 'Search/extract results must be an array', provider=provider)
    for row in result['results']:
        if not isinstance(row, dict) or not _http_url(row.get('url')):
            raise ProviderError('invalid_response', 'A search/extract source must have an HTTP(S) URL', provider=provider)
        fields = ('title', 'content', 'raw_content')
        if any(key in row and row[key] is not None and not isinstance(row[key], str) for key in fields):
            raise ProviderError('invalid_response', 'Search/extract content fields must be text', provider=provider)
        if extracted and not isinstance(row.get('raw_content', row.get('content')), str):
            raise ProviderError('invalid_response', 'Extracted source is missing actual page content', provider=provider)
    return result


class HTTPGateway:
    def __init__(self, profile, *, domain, client=None):
        self.profile, self.domain = deepcopy(profile), domain
        self.provider = profile.get('provider_name', domain + '_gateway')
        self.base_url = validate_url(resolved(profile, 'base_url'))
        self.key = api_key(profile)
        self.client = client if client is not None else httpx.AsyncClient(timeout=profile.get('timeout_seconds', 12), follow_redirects=False)
        self.owns_client, self._closed = client is None, False

    def info(self):
        return {'provider': self.provider, 'configured': bool(self.base_url) and (
            not self.profile.get('require_api_key', True) or bool(self.key)),
            'execution_location': 'cloud', 'capabilities': {'domain': self.domain}, 'availability': 'not_verified'}

    async def request(self, operation, payload):
        if self._closed:
            raise ProviderError('unavailable', 'Read gateway is closed', provider=self.provider)
        if not self.info()['configured']:
            raise ProviderError('not_configured', f'{self.domain} gateway is not configured', provider=self.provider)
        try:
            response = await self.client.post(self.base_url + '/v1/' + self.domain + '/' + operation,
                json=payload, headers={'Authorization': 'Bearer ' + self.key} if self.key else {})
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            raise _failure(exc, self.provider, (self.key,)) from None
        if not isinstance(result, dict):
            raise ProviderError('invalid_response', 'Gateway response must be an object', provider=self.provider)
        if result.get('ok') is False and result.get('needs_clarification') is not True:
            error = result.get('error') or {}
            code = error.get('code') if isinstance(error, dict) else None
            if code not in {'timeout', 'auth', 'rate_limit', 'unavailable', 'unsupported_capability', 'not_configured'}:
                code = 'invalid_response'
            raise ProviderError(code, 'Gateway reported a failed operation', provider=self.provider,
                                details={'provider_response': redact(result, (self.key,))})
        if result.get('needs_clarification') is True and (not isinstance(result.get('reason'), str) or not result['reason'].strip()):
            raise ProviderError('invalid_response', 'Clarification response is missing its question', provider=self.provider)
        return redact({**result, 'provider': self.provider, 'queried_at': result.get('queried_at', utcnow()),
                       '_provider_trace': {'operation': operation, 'request': payload, 'response': result}}, (self.key,))

    async def search(self, query, max_results=5, *, include_domains=None):
        result = _search_rows(await self.request('search', {'query': query, 'max_results': max_results,
                    'include_domains': include_domains}), self.provider)
        if include_domains:
            if any(not is_domain(domain) for domain in include_domains):
                raise ProviderError('invalid_request', 'Search include_domains must be valid domains', provider=self.provider)
            result['results'] = [row for row in result['results'] if any(
                (urlparse(row['url']).hostname or '').lower() == domain.lower()
                or (urlparse(row['url']).hostname or '').lower().endswith('.' + domain.lower()) for domain in include_domains)]
            result['request_filters'] = {'include_domains': include_domains, 'include_domains_mode': 'restrict'}
        return result

    async def extract(self, urls):
        result = _search_rows(await self.request('extract', {'urls': urls}), self.provider, extracted=True)
        if any(row['url'] not in urls for row in result['results']):
            raise ProviderError('invalid_response', 'Extraction returned an unrequested URL', provider=self.provider)
        return result

    async def search_places(self, query, max_results=5, *, cities_only=False, near=None):
        result = await self.request('search', {'query': query, 'max_results': max_results, 'cities_only': cities_only, 'near': near})
        return normalize_places(result, self.provider, self.profile.get('coordinate_system', 'WGS84'))

    async def _forecast(self, payload, requested):
        result = await self.request('forecast', payload)
        if result.get('needs_clarification') is True:
            return {**result, 'ok': False}
        actual = result['_provider_trace']['response']
        if not _weather_fields(actual) or actual.get('data_type') != 'forecast' or actual.get('ok') is not True:
            raise ProviderError('invalid_response', 'Weather gateway omitted valid date/unit/location/source fields', provider=self.provider)
        location = actual.get('location')
        if not (isinstance(location, str) and location.strip() or isinstance(location, dict) and isinstance(location.get('name'), str) and location['name'].strip()):
            raise ProviderError('invalid_response', 'Weather gateway omitted the resolved location', provider=self.provider)
        offsets = {'today': 0, 'tomorrow': 1, 'day_after_tomorrow': 2}
        target = (datetime.fromisoformat(actual['queried_at']).astimezone(ZoneInfo(actual['timezone'])).date()
                  + timedelta(days=offsets[requested])).isoformat() if requested in offsets else requested
        if actual['date'] != target:
            raise ProviderError('invalid_response', 'Weather gateway returned a different requested date', provider=self.provider)
        evidence = WeatherReceipt(self.provider, 'gateway_forecast', actual).to_dict()
        result['raw'] = {**(result.get('raw') if isinstance(result.get('raw'), dict) else {}), 'forecast': evidence}
        return result

    async def get_weather(self, location, date):
        if date == 'now':
            raise ProviderError('unsupported_capability', 'Weather gateway currently provides daily forecasts', provider=self.provider)
        result = await self._forecast({'location': location, 'date': date}, date)
        if result.get('needs_clarification'):
            return {**result, 'kind': 'weather'}
        return {**result, 'kind': 'weather', 'mode': 'forecast', 'place': deepcopy(result['location']) if isinstance(result['location'], dict) else {'name': result['location']},
                'summary': {'min_c': result['temperature_min'], 'max_c': result['temperature_max']}}

    async def forecast(self, city, date, country_code=None):
        return await self._forecast({'city': city, 'date': date, 'country_code': country_code}, date)

    async def route(self, origin, destination, waypoints):
        return await self.request('compute', {'origin': origin, 'destination': destination, 'waypoints': waypoints})

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        if self.owns_client:
            await self.client.aclose()


class TavilyProvider:
    def __init__(self, profile, client=None):
        self.profile = {'key_env': 'TAVILY_API_KEY', **profile}
        self.client = client if client is not None else TavilyClient(
            key_env=self.profile['key_env'], timeout_seconds=self.profile.get('timeout_seconds', 35))
        self._owns_client, self._closed = client is None, False

    def info(self):
        return {'provider': 'Tavily', 'configured': bool(api_key(self.profile)), 'execution_location': 'cloud',
                'capabilities': {'search': True, 'extract': True}}

    async def search(self, query, max_results=5, *, include_domains=None):
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'Tavily API key is missing', provider='Tavily')
        options = {'include_domains': include_domains} if include_domains is not None else {}
        try:
            return _search_rows(await self.client.search(query, max_results, **options), 'Tavily')
        except Exception as exc:
            raise _failure(exc, 'Tavily', (api_key(self.profile),)) from None

    async def extract(self, urls):
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'Tavily API key is missing', provider='Tavily')
        try:
            return _search_rows(await self.client.extract(urls), 'Tavily', extracted=True)
        except Exception as exc:
            raise _failure(exc, 'Tavily', (api_key(self.profile),)) from None

    async def aclose(self):
        if not self._closed and self._owns_client:
            self._closed = True
            await self.client.aclose()


class SearchServices:
    def __init__(self, search, content):
        self.search_provider, self.content_provider = search, content
        self._closed = False

    def info(self):
        return {**self.search_provider.info(), 'content': self.content_provider.info()}

    async def search(self, query, max_results=5, *, include_domains=None):
        options = {'include_domains': include_domains} if include_domains is not None else {}
        return await self.search_provider.search(query, max_results, **options)

    async def extract(self, urls):
        return await self.content_provider.extract(urls)

    async def web_search(self, query, max_results=3, include_domains=None):
        result = _search_rows(await self.search(query, max_results, include_domains=include_domains), self.info()['provider'])
        return {**result, 'ok': True, 'kind': 'web_search', 'provider': self.info()['provider'],
                'query': query, 'evidence': 'search_snippets', 'notice': feedback_text('web_search.notice')}

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        await self.search_provider.aclose()
        if self.content_provider is not self.search_provider:
            await self.content_provider.aclose()


def normalize_places(result, provider, coordinate_system='WGS84'):
    result = deepcopy(result)
    if not isinstance(result, dict) or not isinstance(result.get('places'), list):
        raise ProviderError('invalid_response', 'Place results must be an array', provider=provider)
    seen = set()
    for place in result['places']:
        if not isinstance(place, dict):
            raise ProviderError('invalid_response', 'Place entries must be objects', provider=provider)
        identifier = place.get('provider_id', place.get('place_id'))
        if not isinstance(identifier, str) or not identifier or identifier in seen or not isinstance(place.get('name'), str) or not place['name'].strip():
            raise ProviderError('invalid_response', 'Place identity/name is invalid or duplicated', provider=provider)
        seen.add(identifier)
        declared = place.get('coordinate_system', coordinate_system)
        if declared not in {'WGS84', 'GCJ02', 'BD09'} or declared != coordinate_system:
            raise ProviderError('invalid_response', 'Place coordinate system differs from the configured provider', provider=provider)
        place.update(provider=provider, provider_id=identifier, place_id=identifier,
                     place_ref=provider + ':' + identifier, coordinate_system=declared)
        coordinates = place.get('location')
        if not isinstance(coordinates, dict) or not all(type(coordinates.get(key)) in (int, float) and math.isfinite(coordinates[key])
                for key in ('latitude', 'longitude')) or not (-90 <= coordinates['latitude'] <= 90 and -180 <= coordinates['longitude'] <= 180):
            raise ProviderError('invalid_response', 'Place coordinates are invalid', provider=provider)
    return result


class GooglePlacesProvider:
    def __init__(self, client):
        self.client = client

    def info(self):
        return {'provider': 'google', 'configured': bool(self.client.key), 'coordinate_system': 'WGS84', 'execution_location': 'cloud'}

    async def search_places(self, *args, **kwargs):
        try:
            return normalize_places(await self.client.search_places(*args, **kwargs), 'google')
        except Exception as exc:
            raise _failure(exc, 'google', (self.client.key,)) from None

    async def aclose(self):
        pass  # The shared transport belongs to the bundle.


class GoogleWeatherProvider:
    def __init__(self, client, *, fallback='open_meteo'):
        self.client, self.fallback = client, fallback

    def info(self):
        return {'provider': 'Google Weather', 'configured': bool(self.client.key), 'execution_location': 'cloud',
                'coverage_fallback': self.fallback, 'task_forecast': 'Google Weather'}

    async def get_weather(self, location, date):
        try:
            return await self.client.get_weather(location, date)
        except Exception as exc:
            raise _failure(exc, 'Google Weather', (self.client.key,)) from None

    async def forecast(self, city, date, country_code=None):
        if not _date(date) or country_code is not None and not is_alpha2(country_code):
            raise ProviderError('invalid_request', 'Forecast date/country code is invalid', provider='Google Weather')
        result = await self.get_weather(city + (' ' + country_code.upper() if country_code else ''), date)
        if result.get('ok') is False:
            return result
        if country_code and result.get('place', {}).get('country_code') != country_code.upper():
            return {'ok': False, 'needs_clarification': True, 'reason': feedback_text('maps.city_clarification'),
                    'candidates': [result.get('place')], 'provider': 'Google Weather'}
        source = 'google_forecast' if result.get('provider') == 'Google Weather' else 'google_fallback_forecast'
        evidence = WeatherReceipt(result.get('provider'), source, result).to_dict()
        try:
            record = _receipt_values(evidence)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _failure(exc, 'Google Weather') from None
        if not _weather_fields(record) or record['date'] != date:
            raise ProviderError('invalid_response', 'Google forecast does not match the requested date/source', provider='Google Weather')
        return {'ok': True, 'data_type': 'forecast', 'provider': result['provider'], **record, 'raw': {'forecast': evidence}}

    async def aclose(self):
        pass  # The shared Google transport belongs to MapsServices.owned.


class OpenMeteoProvider:
    def __init__(self, client=None):
        self.client = client if client is not None else WeatherClient()
        self._owns_client = client is None

    def info(self):
        return {'provider': 'Open-Meteo', 'configured': True, 'execution_location': 'cloud',
                'capabilities': {'forecast': True, 'current': False}}

    async def forecast(self, city, date, country_code=None):
        try:
            return await self.client.forecast(city, date, country_code)
        except Exception as exc:
            raise _failure(exc, 'Open-Meteo') from None

    async def get_weather(self, location, date):
        if date == 'now':
            raise ProviderError('unsupported_capability', 'This weather profile provides daily forecasts', provider='Open-Meteo')
        try:
            result = await self.client.forecast(location, date, relative=True)
        except Exception as exc:
            raise _failure(exc, 'Open-Meteo') from None
        if result.get('needs_clarification'):
            return {**result, 'kind': 'weather', 'provider': 'Open-Meteo'}
        if not verify_forecast_record(result, (result.get('raw') or {}).get('forecast')):
            raise ProviderError('invalid_response', 'Weather service returned unverifiable forecast data', provider='Open-Meteo')
        return {'ok': True, 'kind': 'weather', 'provider': 'Open-Meteo', 'mode': 'forecast',
                'date': result['date'], 'timezone': result['timezone'], 'place': deepcopy(result['location']) if isinstance(result['location'], dict) else {'name': result['location']},
                'summary': {'min_c': result['temperature_min'], 'max_c': result['temperature_max']},
                'details': result.get('raw'), 'queried_at': result['queried_at'],
                'source_url': result['source_url'], 'notice': feedback_text('maps.forecast_interval')}

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()


class MapsServices:
    def __init__(self, places, weather, *, owned=()):
        self.places, self.weather, self.owned = places, weather, owned
        self._closed = False

    def info(self):
        return {'configured': self.places.info()['configured'], 'places': self.places.info()['provider'],
                'execution_location': self.places.info().get('execution_location', 'cloud'),
                'weather': self.weather.info()['provider'], 'weather_fallback': self.weather.info().get('coverage_fallback'),
                'providers': {'places': self.places.info(), 'weather': self.weather.info()}, 'navigation': 'simulated_device'}

    async def search_places(self, *args, **kwargs):
        return await self.places.search_places(*args, **kwargs)

    async def get_weather(self, location, date):
        return await self.weather.get_weather(location, date)

    async def forecast(self, city, date, country_code=None):
        return await self.weather.forecast(city, date, country_code)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        seen = set()
        for resource in (self.places, self.weather, *self.owned):
            if id(resource) not in seen:
                seen.add(id(resource))
                await resource.aclose()


class DevicePlacesProvider:
    """Marker only: the session-bound device gateway performs this query."""
    def info(self):
        return {'provider': 'device_gateway', 'configured': True, 'execution_location': 'device',
                'device_required': True, 'availability': 'session_bound'}

    async def search_places(self, *args, **kwargs):
        raise ProviderError('device_context_required', 'Device queries require a bound session gateway', provider='device_gateway')

    async def aclose(self):
        pass
