"""Small live probe of Google Places, Routes, Weather and existing weather client.

Uses public Paris landmarks, never user location. Saves only selected evidence;
credentials and authenticated request URLs are never logged.
"""
import asyncio
from datetime import datetime, timedelta, date
import json
import os
from pathlib import Path
import re
import shlex
import time
from zoneinfo import ZoneInfo

import httpx

from backend.weather import WeatherClient


ROOT = Path(__file__).resolve().parents[1]


def read_key():
    # Read only the named assignment; do not execute unrelated shell startup code.
    config = Path.home() / '.zshrc'
    if config.exists():
        for line in reversed(config.read_text().splitlines()):
            match = re.match(r'^\s*(?:export\s+)?GOOGLE_MAPS_API_KEY\s*=\s*(.*)$', line)
            if match:
                parts = shlex.split(match.group(1), comments=True)
                if len(parts) == 1 and '$' not in parts[0] and '`' not in parts[0]:
                    return parts[0]
    return os.environ.get('GOOGLE_MAPS_API_KEY', '')


async def main():
    key = read_key()
    if not key:
        raise SystemExit('GOOGLE_MAPS_API_KEY is not available as a literal assignment or environment variable.')
    records = []
    observed_at = datetime.now(ZoneInfo('Europe/Paris')).isoformat()

    async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
        async def probe(name, method, url, extract, *, params=None, body=None, fields=None):
            started = time.perf_counter()
            record = {'name': name, 'endpoint': url, 'ok': False}
            try:
                headers = {'X-Goog-Api-Key': key}
                if fields:
                    headers['X-Goog-FieldMask'] = fields
                response = await client.request(method, url, headers=headers, params=params, json=body)
                record['http_status'] = response.status_code
                data = response.json()
                if response.is_success:
                    record['evidence'] = extract(data)
                    record['ok'] = True
                else:
                    error = data.get('error', {})
                    record['error'] = {k: error.get(k) for k in ('code', 'status', 'message')}
                    record['error']['reasons'] = [item.get('reason') for item in error.get('details', []) if item.get('reason')]
            except Exception as exc:
                # Exception strings can contain URLs; never include them.
                record['error'] = {'type': type(exc).__name__}
            record['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
            record = json.loads(json.dumps(record, ensure_ascii=False).replace(key, '[REDACTED]'))
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            return record

        def place(data):
            items = data.get('places') or []
            assert items, 'No places returned'
            selected = items[0]
            assert selected.get('id') and selected.get('location'), 'Missing place identity/location'
            return selected

        places = await asyncio.gather(*[
            probe(name, 'POST', 'https://places.googleapis.com/v1/places:searchText', place,
                  body={'textQuery': query, 'languageCode': 'zh-CN', 'regionCode': 'FR', 'pageSize': 1},
                  fields='places.id,places.displayName,places.formattedAddress,places.location')
            for name, query in [('places_louvre', 'Louvre Museum Paris France'),
                                ('places_airport', 'Paris Charles de Gaulle Airport France')]
        ])

        def route(data):
            routes = data.get('routes') or []
            assert routes, 'No routes returned'
            result = routes[0]
            assert result.get('distanceMeters', 0) > 0 and result.get('duration'), 'Invalid route metrics'
            steps = [step for leg in result.get('legs', []) for step in leg.get('steps', [])]
            return {'distance_meters': result['distanceMeters'], 'duration': result['duration'],
                    'step_count': len(steps), 'first_instructions': [s.get('navigationInstruction') for s in steps[:3]]}

        def conditions(data):
            assert isinstance(data.get('temperature', {}).get('degrees'), (int, float)), 'Missing temperature'
            assert data['temperature']['unit'] == 'CELSIUS', 'Wrong units'
            return {k: data.get(k) for k in ('currentTime', 'timeZone', 'weatherCondition', 'temperature',
                                            'feelsLikeTemperature', 'relativeHumidity', 'wind')}

        def forecast(data):
            days = data.get('forecastDays') or []
            assert len(days) == 3, 'Expected three forecast days'
            for day in days:
                assert day.get('displayDate') and day.get('minTemperature') and day.get('maxTemperature')
                assert day['minTemperature']['unit'] == day['maxTemperature']['unit'] == 'CELSIUS'
                assert day['minTemperature']['degrees'] <= day['maxTemperature']['degrees']
            today = datetime.now(ZoneInfo(data['timeZone']['id'])).date()
            returned_dates = {date(**day['displayDate']) for day in days}
            assert {today, today + timedelta(days=1)} <= returned_dates, 'Today or tomorrow is missing'
            return {'timeZone': data.get('timeZone'), 'days': [{k: d.get(k) for k in
                    ('displayDate', 'interval', 'minTemperature', 'maxTemperature')} for d in days],
                    'today_and_tomorrow_present': True}

        # These are public landmark coordinates, not the device/user location.
        location = places[0].get('evidence', {}).get('location', {'latitude': 48.8606, 'longitude': 2.3376})
        weather_params = {f'location.{k}': v for k, v in location.items()}
        weather_params.update(unitsSystem='METRIC', languageCode='zh-CN')
        jobs = [
            probe('google_weather_current', 'GET', 'https://weather.googleapis.com/v1/currentConditions:lookup',
                  conditions, params=weather_params),
            probe('google_weather_forecast', 'GET', 'https://weather.googleapis.com/v1/forecast/days:lookup',
                  forecast, params={**weather_params, 'days': 3, 'pageSize': 3}),
        ]
        if all(item['ok'] for item in places):
            jobs.append(probe('routes_louvre_to_airport', 'POST', 'https://routes.googleapis.com/directions/v2:computeRoutes',
                              route, body={'origin': {'placeId': places[0]['evidence']['id']},
                                           'destination': {'placeId': places[1]['evidence']['id']},
                                           'travelMode': 'DRIVE', 'routingPreference': 'TRAFFIC_UNAWARE',
                                           'languageCode': 'zh-CN', 'units': 'METRIC'},
                              fields='routes.distanceMeters,routes.duration,routes.legs.steps.navigationInstruction'))
        else:
            records.append({'name': 'routes_louvre_to_airport', 'ok': False, 'skipped': 'Places lookup failed'})
        await asyncio.gather(*jobs)

    weather = WeatherClient()
    started = time.perf_counter()
    record = {'name': 'existing_open_meteo_forecast', 'ok': False}
    try:
        day = datetime.now(ZoneInfo('Europe/Paris')).date().isoformat()
        result = await weather.forecast('Paris', day, 'FR')
        record['ok'] = result.get('ok') is True
        record['evidence'] = {k: result.get(k) for k in ('date', 'timezone', 'temperature_min', 'temperature_max', 'unit', 'reason', 'needs_clarification')}
    except Exception as exc:
        record['error'] = {'type': type(exc).__name__, 'message': str(exc).replace(key, '[REDACTED]')}
    finally:
        await weather.aclose()
    record['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
    records.append(record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    report = {'observed_at': observed_at, 'real_api': True, 'public_test_locations': True,
              'scope': 'Direct service calls only; no Talker integration or browser navigation simulation tested.',
              'passed': sum(r['ok'] for r in records), 'total': len(records), 'records': records}
    output = ROOT / 'docs' / 'google-maps-live.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'report': str(output), 'passed': report['passed'], 'total': report['total']}))


if __name__ == '__main__':
    asyncio.run(main())
