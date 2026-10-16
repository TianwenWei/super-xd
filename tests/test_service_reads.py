"""Offline read-service contract tests use explicit provider JSON fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math

import httpx
import pytest

from backend.google_maps import GoogleMapsClient
from backend.services.contracts import ProviderError
from backend.services.reads import (HTTPGateway, GoogleWeatherProvider, OpenMeteoProvider,
    DevicePlacesProvider, MapsServices, SearchServices, normalize_places, verify_forecast_record)
from backend.weather import WeatherClient


NOW = datetime(2026, 10, 14, 0, 30, tzinfo=timezone.utc)
CITY = {'id': 1, 'name': 'Honolulu', 'country': 'United States', 'country_code': 'US',
        'latitude': 21.3, 'longitude': -157.8, 'timezone': 'Pacific/Honolulu'}
FORECAST = {'timezone': CITY['timezone'],
            'daily_units': {'temperature_2m_min': '°C', 'temperature_2m_max': '°C'},
            'daily': {'time': ['2026-10-14'], 'temperature_2m_min': [21], 'temperature_2m_max': [28]}}


async def test_open_meteo_relative_date_uses_resolved_city_clock_once_and_keeps_location():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host.startswith('geocoding'):
            return httpx.Response(200, json={'results': [CITY]})
        assert request.url.params['start_date'] == '2026-10-14'  # local Oct 13 + tomorrow
        return httpx.Response(200, json=FORECAST)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenMeteoProvider(WeatherClient(http, now=NOW))
        result = await provider.get_weather('Honolulu', 'tomorrow')
        assert len(calls) == 2 and result['date'] == '2026-10-14'
        assert result['place']['name'] == 'Honolulu' and result['place']['country_code'] == 'US'
        assert result['summary'] == {'min_c': 21, 'max_c': 28}
        assert provider.info()['execution_location'] == 'cloud'


async def test_relative_date_never_adopts_a_prefix_geocoder_match():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'results': [{**CITY, 'name': 'Honolulu District'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OpenMeteoProvider(WeatherClient(http, now=NOW)).get_weather('Honolulu', 'today')
        assert result['ok'] is False and result['needs_clarification']
        assert len(calls) == 1


def gateway_forecast():
    return {'ok': True, 'data_type': 'forecast', 'location': {'name': 'Paris'}, 'date': '2026-10-14',
            'timezone': 'Europe/Paris', 'temperature_min': 12, 'temperature_max': 20, 'unit': '°C',
            'source_url': 'https://weather.example/forecast', 'queried_at': '2026-10-14T00:30:00+00:00'}


async def test_gateway_forecast_receipt_binds_actual_json_without_open_meteo_shape(monkeypatch):
    monkeypatch.setenv('GATEWAY_TEST_KEY', 'private-service-key')
    actual = gateway_forecast()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=actual))) as http:
        provider = HTTPGateway({'base_url': 'https://gateway.example', 'key_env': 'GATEWAY_TEST_KEY'}, domain='weather', client=http)
        result = await provider.forecast('Paris', actual['date'])
        evidence = result['raw']['forecast']
        assert evidence['schema'] == 'weather_receipt.v1' and evidence['response'] == actual
        assert verify_forecast_record(result, evidence)
        bad = deepcopy(result); bad['temperature_min'] = 17
        assert not verify_forecast_record(bad, evidence)
        bad_raw = deepcopy(evidence); bad_raw['response']['date'] = '2026-10-15'
        assert not verify_forecast_record(result, bad_raw)


@pytest.mark.parametrize('change', [{'source_url': ''}, {'unit': 'F'}, {'timezone': 'invalid-zone'},
                                  {'temperature_min': math.nan}, {'temperature_max': -5}, {'date': '2026-02-30'}])
async def test_gateway_rejects_unverifiable_weather_fields(monkeypatch, change):
    monkeypatch.setenv('GATEWAY_TEST_KEY', 'key')
    actual = {**gateway_forecast(), **change}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=json.dumps(actual)))) as http:
        provider = HTTPGateway({'base_url': 'https://gateway.example', 'key_env': 'GATEWAY_TEST_KEY'}, domain='weather', client=http)
        with pytest.raises(ProviderError) as caught:
            await provider.forecast('Paris', '2026-10-14')
        assert caught.value.code == 'invalid_response'


async def test_gateway_transport_error_has_typed_sanitized_trace(monkeypatch):
    monkeypatch.setenv('GATEWAY_TEST_KEY', 'secret-value')
    def handler(request):
        raise httpx.ConnectError('upstream diagnostic secret-value', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = HTTPGateway({'base_url': 'https://gateway.example', 'key_env': 'GATEWAY_TEST_KEY'}, domain='places', client=http)
        with pytest.raises(ProviderError) as caught:
            await provider.search_places('Paris')
        assert caught.value.code == 'unavailable'
        assert 'secret-value' not in str(caught.value) + json.dumps(caught.value.details)


@pytest.mark.parametrize('places', [[None], [{'place_id': 'id', 'name': '', 'location': {'latitude': 1, 'longitude': 2}}],
                                   [{'place_id': 'id', 'name': '地点', 'location': None}],
                                   [{'place_id': 'id', 'name': '地点', 'location': {'latitude': math.nan, 'longitude': 2}}]])
def test_place_normalization_rejects_invalid_structured_results(places):
    with pytest.raises(ProviderError) as caught:
        normalize_places({'places': places}, 'test')
    assert caught.value.code == 'invalid_response'


async def test_google_task_forecast_uses_google_and_retains_real_provider_details():
    calls = []
    place = {'id': 'paris', 'displayName': {'text': 'Paris'}, 'location': {'latitude': 48.8, 'longitude': 2.3},
             'addressComponents': [{'shortText': 'FR', 'types': ['country']}]}
    day = {'displayDate': {'year': 2026, 'month': 10, 'day': 14},
           'minTemperature': {'unit': 'CELSIUS', 'degrees': 12}, 'maxTemperature': {'unit': 'CELSIUS', 'degrees': 20},
           'daytimeForecast': {'weatherCondition': {'description': {'text': '多云'}}, 'precipitation': {'probability': {'percent': 10}}},
           'nighttimeForecast': {'weatherCondition': {'description': {'text': '多云'}}}}
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'places': [place]} if request.url.host == 'places.googleapis.com' else
                              {'timeZone': {'id': 'Europe/Paris'}, 'forecastDays': [day]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        google = GoogleMapsClient(http, 'test-key', now=lambda: NOW)
        result = await GoogleWeatherProvider(google).forecast('Paris', '2026-10-14', 'FR')
        assert all('open-meteo' not in request.url.host for request in calls)
        assert result['temperature_min'] == 12 and result['temperature_max'] == 20
        assert result['raw']['forecast']['response']['details'] == day
        assert verify_forecast_record(result, result['raw']['forecast'])
        mutated = deepcopy(result); mutated['timezone'] = 'America/New_York'
        assert not verify_forecast_record(mutated, result['raw']['forecast'])


async def test_shared_service_lifecycle_and_device_marker_do_not_call_network():
    class Resource:
        def __init__(self):
            self.closed = 0
        async def aclose(self):
            self.closed += 1
    resource = Resource()
    services = MapsServices(resource, resource, owned=(resource, resource))
    await services.aclose(); await services.aclose()
    assert resource.closed == 1
    search = SearchServices(resource, resource)
    await search.aclose(); await search.aclose()
    assert resource.closed == 2
    device = DevicePlacesProvider()
    assert device.info()['execution_location'] == 'device'
    with pytest.raises(ProviderError) as caught:
        await device.search_places('Paris')
    assert caught.value.code == 'device_context_required'


def test_verifier_keeps_legacy_open_meteo_receipts_compatible():
    record = gateway_forecast()
    record.update(date='2026-10-14', timezone=CITY['timezone'], temperature_min=21, temperature_max=28)
    assert verify_forecast_record(record, FORECAST)
    record['temperature_max'] = 99
    assert not verify_forecast_record(record, FORECAST)


async def test_gateway_preserves_real_clarification_and_search_filter_evidence(monkeypatch):
    monkeypatch.setenv('GATEWAY_TEST_KEY', 'secret')
    actual = {'ok': False, 'needs_clarification': True, 'reason': '请确认城市所在国家。', 'candidates': []}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=actual))) as http:
        provider = HTTPGateway({'base_url': 'https://gateway.example', 'key_env': 'GATEWAY_TEST_KEY'}, domain='weather', client=http)
        result = await provider.forecast('Paris', '2026-10-14')
        assert result['ok'] is False and result['reason'] == actual['reason']
        assert 'raw' not in result
    rows = {'results': [{'url': 'https://official.example/a', 'content': '实际摘要'},
                        {'url': 'https://unrelated.example/a', 'content': '另一摘要'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows))) as http:
        provider = HTTPGateway({'base_url': 'https://gateway.example', 'key_env': 'GATEWAY_TEST_KEY'}, domain='web_search', client=http)
        result = await provider.search('实际查询', include_domains=['official.example'])
        assert len(result['results']) == 1
        assert result['_provider_trace']['response'] == rows


@pytest.mark.parametrize('failure,code', [(httpx.ReadTimeout('private transport message'), 'timeout'),
                                        (httpx.ConnectError('private transport message'), 'unavailable')])
async def test_open_meteo_transport_errors_keep_structured_type(failure, code):
    def handler(request):
        raise failure
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderError) as caught:
            await OpenMeteoProvider(WeatherClient(http, NOW)).forecast('Paris', '2026-10-14')
        assert caught.value.code == code
        assert 'private transport message' not in str(caught.value)


@pytest.mark.parametrize('method,args', [('search', ('query',)), ('extract', (['https://source.test'],))])
async def test_tavily_missing_key_is_not_configured_before_transport(monkeypatch, method, args):
    from backend.services.reads import TavilyProvider
    monkeypatch.delenv('ABSENT_TAVILY_TEST_KEY', raising=False)
    class Forbidden:
        async def search(self, *args, **kwargs):
            pytest.fail('Missing credential must not contact the service')
        async def extract(self, *args, **kwargs):
            pytest.fail('Missing credential must not contact the service')
    provider = TavilyProvider({'key_env': 'ABSENT_TAVILY_TEST_KEY'}, client=Forbidden())
    with pytest.raises(ProviderError) as error:
        await getattr(provider, method)(*args)
    assert error.value.code == 'not_configured'


async def test_tavily_typed_error_keeps_complete_redacted_diagnostic(monkeypatch):
    from backend.services.reads import TavilyProvider
    monkeypatch.setenv('TAVILY_ERROR_TEST_KEY', 'sensitive-token')
    diagnostic = 'd' * 3000 + ' sensitive-token'
    class Failure:
        async def search(self, *args, **kwargs):
            error = RuntimeError('short failure')
            error.status_code, error.full_diagnostic = 429, diagnostic
            raise error
    provider = TavilyProvider({'key_env': 'TAVILY_ERROR_TEST_KEY'}, client=Failure())
    with pytest.raises(ProviderError) as caught:
        await provider.search('query')
    assert caught.value.code == 'rate_limit' and caught.value.retryable
    assert caught.value.details['diagnostic'] == 'd' * 3000 + ' [redacted]'


async def test_owned_tavily_transport_uses_profile_timeout():
    from backend.services.reads import TavilyProvider
    provider = TavilyProvider({'timeout_seconds': 7.5})
    try:
        assert provider.client.client.timeout.as_dict() == {'connect': 7.5, 'read': 7.5, 'write': 7.5, 'pool': 7.5}
    finally:
        await provider.aclose()


async def test_injected_tavily_transport_keeps_its_timeout_and_ownership():
    from backend.models import TavilyClient
    from backend.services.reads import TavilyProvider
    async with httpx.AsyncClient(timeout=23) as http:
        adapter = TavilyClient(http)
        provider = TavilyProvider({'timeout_seconds': 7.5}, client=adapter)
        assert http.timeout.as_dict() == {'connect': 23, 'read': 23, 'write': 23, 'pool': 23}
        await provider.aclose()
        assert not http.is_closed
