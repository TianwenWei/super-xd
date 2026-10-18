import json

import httpx
import pytest

from super_xd.nlp.config.services import ServiceConfig
from super_xd.nlp.contracts.providers import ProviderError
from super_xd.demo.assembly import create_cockpit
from super_xd.nlp.adapters.reads.routes import GatewayRoutesProvider, GoogleRoutesProvider
from tests.support.service_config import document

PLACE = {'provider': 'google', 'provider_id': 'real-place', 'coordinate_system': 'wgs84',
         'location': {'latitude': 48.86, 'longitude': 2.3}}


async def test_google_routes_preserves_actual_response_and_does_not_claim_execution(monkeypatch):
    monkeypatch.setenv('ROUTE_TEST_KEY', 'secret')
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'routes': [{'distanceMeters': 3456, 'duration': '123.5s',
            'polyline': {'encodedPolyline': 'actual-encoded-route'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleRoutesProvider({'key_env': 'ROUTE_TEST_KEY'}, client)
        result = await provider.route(PLACE, PLACE, [PLACE])
        assert result['distance_meters'] == 3456 and result['duration_seconds'] == 123.5
        assert result['polyline'] == 'actual-encoded-route' and result['executed'] is False
        assert result['route']['duration'] == '123.5s'
        payload = json.loads(requests[0].content)
        assert payload['intermediates'][0]['location']['latLng'] == PLACE['location']
        assert requests[0].headers['X-Goog-FieldMask'] == 'routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline'


@pytest.mark.parametrize('origin,code', [(None, 'origin_required'),
    ({**PLACE, 'coordinate_system': 'GCJ02'}, 'unsupported_coordinate_system'),
    ({**PLACE, 'location': None}, 'invalid_place'),
    ({**PLACE, 'location': []}, 'invalid_place'),
    ({**PLACE, 'location': {'latitude': 100, 'longitude': 2}}, 'invalid_place')])
async def test_google_routes_rejects_unsourced_or_incompatible_origin_before_network(monkeypatch, origin, code):
    monkeypatch.setenv('ROUTE_TEST_KEY', 'secret')
    def handler(request):
        pytest.fail('Rejected coordinates must not reach the provider')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await GoogleRoutesProvider({'key_env': 'ROUTE_TEST_KEY'}, client).route(origin, PLACE, [])
        assert error.value.code == code


async def test_routes_permission_failure_is_explicit(monkeypatch):
    monkeypatch.setenv('ROUTE_TEST_KEY', 'secret')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
        with pytest.raises(ProviderError) as error:
            await GoogleRoutesProvider({'key_env': 'ROUTE_TEST_KEY'}, client).route(PLACE, PLACE, [])
        assert error.value.code == 'service_unavailable' and '403' in str(error.value)
        assert error.value.details['status_code'] == 403


async def test_route_malformed_polyline_is_a_typed_observed_failure(monkeypatch):
    monkeypatch.setenv('ROUTE_TEST_KEY', 'secret')
    actual = {'routes': [{'distanceMeters': 30, 'duration': '10s', 'polyline': []}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=actual))) as client:
        with pytest.raises(ProviderError) as error:
            await GoogleRoutesProvider({'key_env': 'ROUTE_TEST_KEY'}, client).route(PLACE, PLACE, [])
        assert error.value.code == 'invalid_response'
        assert error.value.details['response'] == actual


@pytest.mark.parametrize('response,ok', [
    ({'ok': True, 'coordinate_system': 'GCJ02', 'distance_meters': 10, 'duration_seconds': 3}, True),
    ({'ok': True, 'coordinate_system': 'GCJ02', 'distance_meters': 10}, False),
    ({'ok': True, 'coordinate_system': 'WGS84', 'distance_meters': 10, 'duration_seconds': 3}, False),
])
async def test_route_gateway_validates_real_measurements_and_coordinate_system(response, ok):
    place = {**PLACE, 'coordinate_system': 'GCJ02'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
        provider = GatewayRoutesProvider({'base_url': 'https://routes.test', 'require_api_key': False,
                                           'coordinate_system': 'GCJ02'}, client)
        if ok:
            actual = await provider.route(place, place, [])
            assert actual['distance_meters'] == 10 and actual['executed'] is False
            assert actual['_provider_trace']['response'] == response
        else:
            with pytest.raises(ProviderError):
                await provider.route(place, place, [])


async def test_default_media_bindings_are_independent_and_never_fall_back(monkeypatch):
    for key in ('MEDIA_PROFILE', 'MEDIA_MUSIC_PROFILE', 'MEDIA_VIDEO_PROFILE', 'MEDIA_RADIO_PROFILE',
                'TENCENT_MUSIC_GATEWAY_URL', 'TENCENT_MUSIC_GATEWAY_KEY', 'VIDEO_CP_URL', 'VIDEO_CP_KEY', 'RADIO_CP_URL', 'RADIO_CP_KEY'):
        monkeypatch.delenv(key, raising=False)
    cockpit, files = create_cockpit(ServiceConfig(document()))
    try:
        info = cockpit.media.info()
        assert info['providers']['music']['provider'] == 'tencent_gateway'
        assert info['providers']['video']['provider'] == 'video_cp'
        assert info['providers']['radio']['provider'] == 'radio_cp'
        assert not any(value['configured'] for value in info['providers'].values()) and not files
        for kind in ('music', 'video', 'radio'):
            with pytest.raises(ProviderError) as error:
                await cockpit.media.search(kind=kind, query='test')
            assert error.value.code == 'not_configured'
    finally:
        await cockpit.aclose()


async def test_media_environment_override_changes_only_the_selected_cp(monkeypatch):
    monkeypatch.delenv('MEDIA_PROFILE', raising=False)
    monkeypatch.setenv('MEDIA_VIDEO_PROFILE', 'device')
    cockpit, _ = create_cockpit(ServiceConfig(document()))
    try:
        assert cockpit.media.provider_info('video')['execution_location'] == 'device'
        assert cockpit.media.provider_info('music')['execution_location'] == 'cloud'
        assert cockpit.media.provider_info('radio')['execution_location'] == 'cloud'
    finally:
        await cockpit.aclose()
