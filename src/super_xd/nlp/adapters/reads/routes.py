"""Real route queries, kept separate from navigation-device execution receipts."""
from copy import deepcopy
import math

import httpx

from super_xd.nlp.adapters.common import utcnow
from super_xd.nlp.config.services import api_key
from super_xd.nlp.contracts.providers import ProviderError, redact
from super_xd.nlp.adapters.reads.services import HTTPGateway


def coordinates(place, provider, coordinate_system='WGS84'):
    if not isinstance(place, dict):
        raise ProviderError('invalid_place', 'A route endpoint must be a sourced place', provider=provider)
    if str(place.get('coordinate_system', 'WGS84')).upper() != coordinate_system.upper():
        raise ProviderError('unsupported_coordinate_system', 'Route coordinates must match the provider', provider=provider)
    location = place.get('location', place)
    if not isinstance(location, dict):
        raise ProviderError('invalid_place', 'Route location must contain coordinates', provider=provider)
    values = {key: location.get(key) for key in ('latitude', 'longitude')}
    if not all(type(v) in (int, float) and math.isfinite(v) for v in values.values()) or not (
            -90 <= values['latitude'] <= 90 and -180 <= values['longitude'] <= 180):
        raise ProviderError('invalid_place', 'Route coordinates are invalid', provider=provider)
    return values


class GatewayRoutesProvider(HTTPGateway):
    def __init__(self, profile, client=None):
        super().__init__(profile, domain='routes', client=client)

    async def route(self, origin, destination, waypoints):
        if origin is None:
            raise ProviderError('origin_required', 'Choose a real origin before querying a route', provider=self.provider)
        if not isinstance(waypoints, list) or len(waypoints) > 10:
            raise ProviderError('invalid_request', 'At most ten route waypoints are supported', provider=self.provider)
        coordinate_system = self.profile.get('coordinate_system', 'WGS84')
        for place in (origin, destination, *waypoints):
            if not isinstance(place, dict) or 'coordinate_system' not in place:
                raise ProviderError('unsupported_coordinate_system', 'Route coordinates must match the configured gateway', provider=self.provider)
            coordinates(place, self.provider, coordinate_system)
        result = await self.request('compute', {'origin': origin, 'destination': destination, 'waypoints': waypoints})
        if result.get('ok') is not True or any(type(result.get(key)) not in (int, float)
                or not math.isfinite(result[key]) or result[key] < 0 for key in ('distance_meters', 'duration_seconds')):
            raise ProviderError('invalid_response', 'Route gateway omitted actual distance and duration', provider=self.provider)
        if result.get('coordinate_system') != coordinate_system:
            raise ProviderError('invalid_response', 'Route gateway returned a different coordinate system', provider=self.provider)
        return {**result, 'executed': False, 'evidence': 'provider_route_response'}


class DisabledRoutesProvider:
    def info(self):
        return {'provider': None, 'configured': False, 'execution_location': 'cloud'}

    async def route(self, origin, destination, waypoints):
        raise ProviderError('not_configured', 'Route queries are not configured', provider='routes')

    async def aclose(self):
        pass


class GoogleRoutesProvider:
    def __init__(self, profile, client=None):
        self.key = api_key(profile)
        self.client = client or httpx.AsyncClient(timeout=profile.get('timeout_seconds', 12), follow_redirects=False)
        self.owns_client = client is None

    def info(self):
        return {'provider': 'google_routes', 'configured': bool(self.key), 'availability': 'not_verified',
                'coordinate_system': 'WGS84', 'execution_location': 'cloud'}

    @staticmethod
    def waypoint(place):
        return {'location': {'latLng': coordinates(place, 'google_routes')}}

    async def route(self, origin, destination, waypoints):
        if origin is None:
            raise ProviderError('origin_required', 'Choose a real origin before querying a route', provider='google_routes')
        if not self.key:
            raise ProviderError('not_configured', 'Google Routes credential is missing', provider='google_routes')
        if not isinstance(waypoints, list) or len(waypoints) > 10:
            raise ProviderError('invalid_request', 'At most ten route waypoints are supported', provider='google_routes')
        body = {'origin': self.waypoint(origin), 'destination': self.waypoint(destination),
                'intermediates': [self.waypoint(place) for place in waypoints], 'travelMode': 'DRIVE',
                'routingPreference': 'TRAFFIC_UNAWARE', 'computeAlternativeRoutes': False,
                'languageCode': 'zh-CN', 'units': 'METRIC'}
        response = None
        data = None
        def diagnostics():
            actual = data
            if actual is None and response is not None:
                try:
                    actual = response.json()
                except ValueError:
                    actual = response.text.replace(self.key, '[redacted]')[:4096]
            return redact({'request': body, 'response': actual,
                           'status_code': response.status_code if response is not None else None}, (self.key,))
        try:
            response = await self.client.post('https://routes.googleapis.com/directions/v2:computeRoutes',
                json=body, headers={'X-Goog-Api-Key': self.key,
                    'X-Goog-FieldMask': 'routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline'})
            response.raise_for_status()
            data = response.json()
            route = data['routes'][0]
            duration = route['duration']
            if not isinstance(duration, str) or not duration.endswith('s'):
                raise ValueError('Invalid duration')
            seconds, distance = float(duration[:-1]), route['distanceMeters']
            if not math.isfinite(seconds) or seconds < 0 or type(distance) is not int or distance < 0:
                raise ValueError('Invalid route measurements')
            polyline = route.get('polyline', {})
            if not isinstance(polyline, dict) or ('encodedPolyline' in polyline and not isinstance(polyline['encodedPolyline'], str)):
                raise ValueError('Invalid route polyline')
        except httpx.HTTPStatusError as error:
            raise ProviderError('service_unavailable', f'Google Routes returned HTTP {error.response.status_code}; verify Routes API access',
                                provider='google_routes', details=diagnostics()) from None
        except httpx.TimeoutException:
            raise ProviderError('timeout', 'Route query timed out', provider='google_routes', retryable=True, details=diagnostics()) from None
        except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError('invalid_response', 'Route service returned invalid route data', provider='google_routes', details=diagnostics()) from None
        return redact({'ok': True, 'provider': 'google_routes', 'configured': True, 'coordinate_system': 'WGS84',
                'distance_meters': distance, 'duration_seconds': seconds, 'polyline': polyline.get('encodedPolyline'),
                'waypoint_order': list(range(len(waypoints))), 'queried_at': utcnow(), 'route': deepcopy(route),
                'source_url': 'https://routes.googleapis.com/directions/v2:computeRoutes',
                'executed': False, 'evidence': 'provider_route_response', '_provider_trace': diagnostics()}, (self.key,))

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()
