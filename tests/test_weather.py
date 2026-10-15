import copy
from datetime import datetime, timezone

import httpx
import pytest

from backend.weather import FORECAST_URL, GEOCODING_URL, WeatherClient


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
CITY = {"id": 2988507, "name": "Paris", "country": "France", "country_code": "FR",
        "admin1": "Île-de-France", "latitude": 48.85341, "longitude": 2.3488, "timezone": "Europe/Paris"}
GEO = {"results": [CITY], "generationtime_ms": 0.7, "unmodified_extra": ["geocoding"]}
FORECAST = {"timezone": "Europe/Paris", "utc_offset_seconds": 7200,
            "daily_units": {"time": "iso8601", "temperature_2m_min": "°C", "temperature_2m_max": "°C"},
            "daily": {"time": ["2026-10-14"], "temperature_2m_min": [12.3], "temperature_2m_max": [19.7]},
            "unmodified_extra": {"forecast": True}}


def client_for(geocoding=None, forecast=None, now=NOW):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=copy.deepcopy((GEO if geocoding is None else geocoding) if request.url.host.startswith("geocoding") else (FORECAST if forecast is None else forecast)))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WeatherClient(client=http, now=now), http, calls


@pytest.mark.asyncio
async def test_success_exact_date_units_public_coordinates_and_full_raw():
    weather, http, calls = client_for()
    try:
        result = await weather.forecast("Paris", "2026-10-14", "fr")
        assert result["ok"] is True
        assert result["date"] == "2026-10-14"
        assert result["timezone"] == "Europe/Paris"
        assert result["temperature_min"] == 12.3 and result["temperature_max"] == 19.7
        assert result["unit"] == "°C" and result["data_type"] == "forecast"
        assert result["raw"] == {"geocoding": GEO, "forecast": FORECAST}
        assert result["queried_at"] == NOW.isoformat()
        assert str(calls[0].url).startswith(GEOCODING_URL) and calls[0].url.params["countryCode"] == "FR"
        assert calls[1].url.params["start_date"] == calls[1].url.params["end_date"] == "2026-10-14"
        assert calls[1].url.params["daily"] == "temperature_2m_min,temperature_2m_max"
        assert calls[1].url.params["temperature_unit"] == "celsius"
        assert calls[1].url.params["timezone"] == "Europe/Paris"
        assert result["source_url"] == str(calls[1].url)
        assert str(calls[1].url).startswith(FORECAST_URL)
        assert all("archive" not in str(call.url) for call in calls)
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["2026-10-13", "2026-10-28"])
async def test_local_today_and_today_plus_fifteen_are_inclusive(target):
    forecast = copy.deepcopy(FORECAST)
    forecast["daily"]["time"] = [target]
    weather, http, _ = client_for(forecast=forecast)
    try:
        assert (await weather.forecast("Paris", target))["ok"]
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["2026-10-12", "2026-10-29", "1999-01-01"])
async def test_historical_and_out_of_window_dates_never_call_forecast(target):
    weather, http, calls = client_for()
    try:
        with pytest.raises(RuntimeError, match="历史"):
            await weather.forecast("Paris", target)
        assert len(calls) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_date_window_uses_city_local_date_not_utc_day():
    geo = {"results": [dict(CITY, name="Tokyo", timezone="Asia/Tokyo", country_code="JP")]}
    weather, http, calls = client_for(geocoding=geo, now=lambda: datetime(2026, 10, 13, 23, 30, tzinfo=timezone.utc))
    try:
        with pytest.raises(RuntimeError, match="历史"):
            await weather.forecast("Tokyo", "2026-10-13")
        assert len(calls) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_multiple_same_name_places_require_clarification_not_population_guess():
    geo = {"results": [CITY, dict(CITY, id=4717560, country="United States", country_code="US", timezone="America/Chicago", latitude=33.6609, longitude=-95.5555)]}
    weather, http, calls = client_for(geocoding=geo)
    try:
        result = await weather.forecast("Paris", "2026-10-14")
        assert result["ok"] is False and result["needs_clarification"] is True
        assert len(result["candidates"]) == 2
        assert result["raw"]["geocoding"] == geo and result["raw"]["forecast"] is None
        assert len(calls) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_country_filter_and_exact_name_do_not_choose_prefix_district():
    geo = {"results": [dict(CITY, name="Paris 12e Arrondissement", id=1), CITY,
                       dict(CITY, country_code="US", timezone="America/Chicago", latitude=33.6)]}
    weather, http, _ = client_for(geocoding=geo)
    try:
        result = await weather.forecast("Paris, France", "2026-10-14", "FR")
        assert result["ok"] is True and result["location"]["id"] == CITY["id"]
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("geo", [{}, {"results": []}, {"results": [dict(CITY, name="Parish")]}])
async def test_missing_or_only_prefix_location_never_silently_substitutes_city(geo):
    weather, http, calls = client_for(geocoding=geo)
    try:
        result = await weather.forecast("Paris", "2026-10-14")
        assert result["ok"] is False and result["needs_clarification"] is True
        assert len(calls) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_duplicate_identical_public_city_is_not_false_ambiguity():
    weather, http, _ = client_for(geocoding={"results": [CITY, CITY]})
    try:
        assert (await weather.forecast("Paris", "2026-10-14"))["ok"]
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("latitude", float("nan")), ("longitude", 181), ("latitude", True), ("timezone", "Not/AZone"), ("timezone", None), ("country_code", None)])
async def test_invalid_geocoding_fails_before_forecast(field, value):
    # JSON NaN is encoded manually because HTTPX deliberately rejects it in json=.
    geo = {"results": [dict(CITY, **{field: value})]}
    if field == "latitude" and isinstance(value, float):
        def handler(request):
            return httpx.Response(200, content='{"results":[{"name":"Paris","latitude":NaN,"longitude":2,"timezone":"Europe/Paris","country_code":"FR"}]}')
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        weather = WeatherClient(http, NOW)
    else:
        weather, http, _ = client_for(geocoding=geo)
    try:
        with pytest.raises(RuntimeError, match="地理编码"):
            await weather.forecast("Paris", "2026-10-14")
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda p: p["daily"].update(time=["2026-10-13"]),
    lambda p: p["daily"].update(time=["2026-10-14", "2026-10-15"]),
    lambda p: p["daily"].update(temperature_2m_min=[None]),
    lambda p: p["daily"].update(temperature_2m_max=["19.7"]),
    lambda p: p["daily"].update(temperature_2m_max=[True]),
    lambda p: p["daily"].update(temperature_2m_min=[20.0]),
    lambda p: p["daily"].update(temperature_2m_min=[]),
    lambda p: p["daily_units"].update(temperature_2m_min="°F"),
    lambda p: p["daily_units"].pop("temperature_2m_max"),
    lambda p: p.update(timezone="UTC"),
    lambda p: p.pop("daily"),
])
async def test_wrong_date_timezone_units_null_or_inverted_extrema_are_rejected(mutate):
    forecast = copy.deepcopy(FORECAST)
    mutate(forecast)
    weather, http, calls = client_for(forecast=forecast)
    try:
        with pytest.raises(RuntimeError, match="天气响应"):
            await weather.forecast("Paris", "2026-10-14")
        assert len(calls) == 2
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_nonfinite_forecast_is_rejected():
    def handler(request):
        if request.url.host.startswith("geocoding"):
            return httpx.Response(200, json=GEO)
        return httpx.Response(200, content='{"timezone":"Europe/Paris","daily_units":{"temperature_2m_min":"°C","temperature_2m_max":"°C"},"daily":{"time":["2026-10-14"],"temperature_2m_min":[1],"temperature_2m_max":[Infinity]}}'.encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RuntimeError, match="温度无效"):
            await WeatherClient(http, NOW).forecast("Paris", "2026-10-14")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", "status", "json", "shape", "provider"])
async def test_transport_and_format_errors_are_explicit_without_fallback(failure):
    calls = []
    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        if failure == "network":
            raise httpx.ConnectError("private diagnostic", request=request)
        if failure == "status":
            return httpx.Response(503, json={"detail": "private diagnostic"})
        if failure == "json":
            return httpx.Response(200, content="<html>not JSON</html>")
        return httpx.Response(200, json=[] if failure == "shape" else {"error": True, "reason": "private diagnostic"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RuntimeError) as error:
            await WeatherClient(http, NOW).forecast("Paris", "2026-10-14")
        assert "private diagnostic" not in str(error.value)
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("city,target,code", [("", "2026-10-14", None), ("Paris", "tomorrow", None), ("Paris", "2026-02-30", None), ("Paris", "2026-10-14", "France")])
async def test_invalid_input_does_not_make_network_request(city, target, code):
    weather, http, calls = client_for()
    try:
        with pytest.raises(RuntimeError):
            await weather.forecast(city, target, code)
        assert not calls
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_external_http_client_is_not_closed_and_owned_client_is_closed():
    weather, http, _ = client_for()
    await weather.aclose()
    assert not http.is_closed
    await http.aclose()
    owned = WeatherClient(now=NOW)
    await owned.aclose()
    await owned.aclose()
    assert owned.client.is_closed
