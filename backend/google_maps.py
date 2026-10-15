"""Google Places / Weather, with explicit coverage fallback and bounded requests.

No model chooses coordinates, no implicit device location and no vehicle writes.
Every answer preserves provider, requested local date, units and query time.
"""
from datetime import date as Date, datetime, timedelta, timezone
import math
import os
from zoneinfo import ZoneInfo

import httpx


class MapsError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


WMO_CONDITIONS = {
    0: '晴', 1: '大部晴朗', 2: '局部多云', 3: '阴', 45: '雾', 48: '雾凇',
    51: '小毛毛雨', 53: '中等毛毛雨', 55: '强毛毛雨', 56: '小冻毛毛雨', 57: '强冻毛毛雨',
    61: '小雨', 63: '中雨', 65: '大雨', 66: '小冻雨', 67: '强冻雨',
    71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
    80: '小阵雨', 81: '中等阵雨', 82: '强阵雨', 85: '小阵雪', 86: '强阵雪',
    95: '雷暴', 96: '雷暴伴小冰雹', 99: '雷暴伴大冰雹',
}


NEAR_RADIUS_M = 50000  # Bias radius; a city's airports can be 30+ km from its centre.


class GoogleMapsClient:
    def __init__(self, client=None, api_key=None, now=None):
        self.key = os.getenv('GOOGLE_MAPS_API_KEY', '') if api_key is None else api_key
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_client = client is None
        self.now = now or (lambda: datetime.now(timezone.utc))

    def info(self):
        return {'configured': bool(self.key), 'places': 'Google Places API (New)',
                'weather': 'Google Weather API', 'weather_fallback': 'Open-Meteo',
                'navigation': 'POI destination simulation; no route calculation'}

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()

    async def _request(self, method, url, *, params=None, body=None, fields=None, google=True):
        if google and not self.key:
            raise MapsError('not_configured', 'GOOGLE_MAPS_API_KEY 未配置，未调用Google服务。')
        headers = {'X-Goog-Api-Key': self.key} if google else {}
        if fields:
            headers['X-Goog-FieldMask'] = fields
        try:
            response = await self.client.request(method, url, params=params, json=body, headers=headers)
        except httpx.TimeoutException:
            raise MapsError('timeout', '地点或天气服务超时，请稍后再试。') from None
        except httpx.RequestError:
            raise MapsError('connection_error', '地点或天气服务连接失败。') from None
        try:
            data = response.json()
        except ValueError:
            raise MapsError('invalid_response', '服务未返回有效JSON。') from None
        if not isinstance(data, dict):
            raise MapsError('invalid_response', '服务响应格式无效。')
        if not response.is_success:
            # Provider error strings can echo credentials or URLs. Expose only
            # typed reasons, never request headers/URLs or raw error bodies.
            reasons = {item.get('reason') for item in data.get('error', {}).get('details', [])
                       if isinstance(item, dict)} if isinstance(data.get('error'), dict) else set()
            if response.status_code == 404 and url.startswith('https://weather.googleapis.com/'):
                raise MapsError('unsupported_location', 'Google天气不支持该地点。')
            if 'SERVICE_DISABLED' in reasons:
                raise MapsError('service_disabled', '该Google API尚未启用，请检查项目的API配置。')
            if response.status_code in (401, 403):
                raise MapsError('permission_denied', 'Google API密钥权限或结算配置未通过验证。')
            raise MapsError('upstream_error', f'服务请求失败（HTTP {response.status_code}）。')
        return data

    async def search_places(self, query, max_results=5, *, cities_only=False, near=None):
        body = {'textQuery': query, 'languageCode': 'zh-CN', 'pageSize': max_results}
        if cities_only:
            body.update(includedType='locality', strictTypeFiltering=True)
        anchor = None
        if near:
            # Text search has no GPS; bias it to the place the user named instead
            # of mixing that name into the query (which returned the landmark itself).
            found = await self.search_places(near, max_results=1)
            if not found['places']:
                return {'ok': False, 'code': 'area_not_found', 'provider': 'Google Maps', 'kind': 'places',
                        'reason': f'没有找到“{near}”这个位置，请换个说法。', 'places': []}
            anchor = found['places'][0]
            body['locationBias'] = {'circle': {'center': anchor['location'], 'radius': NEAR_RADIUS_M}}
        data = await self._request('POST', 'https://places.googleapis.com/v1/places:searchText', body=body,
            fields='places.id,places.displayName,places.formattedAddress,places.location,places.googleMapsUri,places.addressComponents,places.attributions')
        places = []
        for item in data.get('places', []):
            loc = item.get('location', {})
            if not (item.get('id') and item.get('displayName', {}).get('text') and
                    finite(loc.get('latitude')) and -90 <= loc['latitude'] <= 90 and
                    finite(loc.get('longitude')) and -180 <= loc['longitude'] <= 180):
                raise MapsError('invalid_response', '地点响应缺少有效名称、ID或坐标。')
            country = next((p.get('shortText') for p in item.get('addressComponents', []) if 'country' in p.get('types', [])), None)
            places.append({'place_id': item['id'], 'name': item['displayName']['text'],
                           'address': item.get('formattedAddress', ''), 'location': loc,
                           'country_code': country, 'maps_url': item.get('googleMapsUri'),
                           'attributions': item.get('attributions', [])})
        if anchor:
            places = [place for place in places if place['place_id'] != anchor['place_id']]
        return {'ok': True, 'provider': 'Google Maps', 'kind': 'places', 'query': query,
                'near': {'name': anchor['name'], 'place_id': anchor['place_id']} if anchor else None,
                'queried_at': self.now().isoformat(), 'places': places,
                'notice': '搜索相关性排序，不代表驾车距离最近。' if places else '没有找到匹配地点，请提供更明确的位置或名称。'}

    @staticmethod
    def target_date(value, today):
        target = today if value in ('today', 'now') else today + timedelta(days=1) if value == 'tomorrow' else Date.fromisoformat(value)
        if not 0 <= (target - today).days <= 9:
            raise MapsError('date_out_of_range', '天气直查支持当地今天至未来九天；历史日期不使用预报替代。')
        return target

    async def get_weather(self, location, date):
        found = await self.search_places(location, max_results=3, cities_only=True)
        places = found['places']
        if len(places) != 1:
            return {'ok': False, 'code': 'ambiguous_location', 'needs_clarification': True,
                    'reason': '请明确城市和国家或地区。', 'candidates': places,
                    'provider': 'Google Maps', 'kind': 'weather'}
        place = places[0]
        coords = place['location']
        params = {f'location.{k}': v for k, v in coords.items()}
        params.update(unitsSystem='METRIC', languageCode='zh-CN')
        current = date == 'now'
        if not current:
            # Before 07:00, the first returned forecast day can be yesterday.
            # Ten records plus displayDate selection avoids indexing errors.
            params.update(days=10, pageSize=10)
        endpoint = 'currentConditions:lookup' if current else 'forecast/days:lookup'
        try:
            data = await self._request('GET', 'https://weather.googleapis.com/v1/' + endpoint, params=params)
        except MapsError as exc:
            if exc.code != 'unsupported_location':
                raise
            return await self._fallback_weather(place, date)
        try:
            zone = data['timeZone']['id']
            today = self.now().astimezone(ZoneInfo(zone)).date()
            target = self.target_date(date, today)
            if current:
                temp = data['temperature']
                assert temp['unit'] == 'CELSIUS' and finite(temp['degrees'])
                summary = {'temperature_c': temp['degrees'],
                           'condition': data['weatherCondition']['description']['text'],
                           'observed_at': data['currentTime']}
                selected = data
            else:
                matching = [day for day in data['forecastDays'] if Date(**day['displayDate']) == target]
                if len(matching) != 1:
                    raise MapsError('date_unavailable', 'Google未返回所请求日期的预报，未使用邻近日期替代。')
                selected = matching[0]
                lo, hi = selected['minTemperature'], selected['maxTemperature']
                assert lo['unit'] == hi['unit'] == 'CELSIUS' and finite(lo['degrees']) and finite(hi['degrees']) and lo['degrees'] <= hi['degrees']
                summary = {'min_c': lo['degrees'], 'max_c': hi['degrees'],
                           'day_condition': selected['daytimeForecast']['weatherCondition']['description']['text'],
                           'night_condition': selected['nighttimeForecast']['weatherCondition']['description']['text'],
                           'rain_probability_percent': selected['daytimeForecast']['precipitation']['probability']['percent']}
        except (KeyError, TypeError, ValueError, AssertionError):
            raise MapsError('invalid_response', 'Google天气缺少有效日期、时区或摄氏温度。') from None
        return {'ok': True, 'kind': 'weather', 'provider': 'Google Weather', 'place': place,
                'date': target.isoformat(), 'timezone': zone, 'mode': 'current' if current else 'forecast',
                'summary': summary, 'details': selected, 'queried_at': self.now().isoformat(),
                'source_url': 'https://weather.googleapis.com/v1/' + endpoint,
                'notice': '每日预报区间为当地07:00至次日07:00。' if not current else ''}

    async def _fallback_weather(self, place, date):
        # A single real forecast request resolves both target time zone and data.
        coords = place['location']
        url = 'https://api.open-meteo.com/v1/forecast'
        params = {'latitude': coords['latitude'], 'longitude': coords['longitude'], 'timezone': 'auto',
                  'forecast_days': 10, 'temperature_unit': 'celsius',
                  'daily': 'temperature_2m_min,temperature_2m_max,weather_code,precipitation_probability_max',
                  'current': 'temperature_2m,weather_code'}
        data = await self._request('GET', url, params=params, google=False)
        try:
            zone = data['timezone']
            today = self.now().astimezone(ZoneInfo(zone)).date()
            target = self.target_date(date, today)
            if date == 'now':
                selected = data['current']
                assert data['current_units']['temperature_2m'] == '°C' and finite(selected['temperature_2m'])
                summary = {'temperature_c': selected['temperature_2m'], 'weather_code': selected['weather_code'],
                           'observed_at_local': selected['time']}
                units = data['current_units']
            else:
                daily = data['daily']
                index = daily['time'].index(target.isoformat())
                selected = {k: v[index] for k, v in daily.items()}
                lo, hi = selected['temperature_2m_min'], selected['temperature_2m_max']
                units = data['daily_units']
                assert units['temperature_2m_min'] == units['temperature_2m_max'] == '°C'
                assert finite(lo) and finite(hi) and lo <= hi
                summary = {'min_c': lo, 'max_c': hi, 'weather_code': selected['weather_code'],
                           'rain_probability_percent': selected['precipitation_probability_max']}
        except (KeyError, TypeError, ValueError, AssertionError, IndexError):
            raise MapsError('invalid_response', '备用天气未返回所请求日期的有效天气数据。') from None
        summary['condition'] = WMO_CONDITIONS.get(selected['weather_code'], '未知天气状况')
        return {'ok': True, 'kind': 'weather', 'provider': 'Open-Meteo', 'place': place,
                'date': target.isoformat(), 'timezone': zone, 'mode': 'current' if date == 'now' else 'forecast',
                'summary': summary, 'details': selected, 'units': units,
                'source_url': str(httpx.URL(url, params=params)), 'queried_at': self.now().isoformat(),
                'fallback_reason': 'google_unsupported_location',
                'notice': 'Google天气不覆盖该地点；已改用Open-Meteo天气数据。'}
