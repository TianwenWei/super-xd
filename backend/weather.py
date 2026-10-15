"""Public-city Open-Meteo forecast, with explicit place/date/unit validation.

Only current forecasts are requested. Historical weather, climate averages,
browser geolocation and user GPS are not used as fallback data sources.
"""
from __future__ import annotations

from datetime import date as Date, datetime, timezone
import math
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_FORECAST_DAY_OFFSET = 15  # Local today through today + 15, inclusive: 16 days.


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _name(value):
    return "".join(char for char in unicodedata.normalize("NFKD", value).casefold()
                   if not unicodedata.combining(char) and not char.isspace())


class WeatherClient:
    def __init__(self, client=None, now=None):
        self.client = client if client is not None else httpx.AsyncClient(timeout=12, follow_redirects=False)
        self._owns_client = client is None
        self._now_source = now
        self._closed = False

    def _now(self):
        value = self._now_source() if callable(self._now_source) else self._now_source
        if value is None:
            value = datetime.now(timezone.utc)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("天气查询时钟必须包含时区。")
        return value.astimezone(timezone.utc)

    async def _json(self, url, params):
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise RuntimeError("天气服务请求超时。") from None
        except httpx.HTTPStatusError as error:
            raise RuntimeError(f"天气服务请求失败（HTTP {error.response.status_code}）。") from None
        except httpx.RequestError:
            raise RuntimeError("天气服务连接失败。") from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("天气服务返回了无效JSON。") from None
        if not isinstance(body, dict):
            raise RuntimeError("天气服务响应格式无效。")
        if body.get("error"):
            raise RuntimeError("天气服务拒绝了本次请求。")
        return body, str(response.url)

    @staticmethod
    def _location(item):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise RuntimeError("城市地理编码缺少有效名称。")
        latitude, longitude = item.get("latitude"), item.get("longitude")
        if not _number(latitude) or not -90 <= latitude <= 90 or not _number(longitude) or not -180 <= longitude <= 180:
            raise RuntimeError("城市地理编码坐标无效。")
        city_timezone = item.get("timezone")
        try:
            if not isinstance(city_timezone, str) or not city_timezone:
                raise ValueError
            ZoneInfo(city_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise RuntimeError("城市地理编码缺少有效IANA时区。") from None
        country_code = item.get("country_code")
        if not isinstance(country_code, str) or not re.fullmatch(r"[A-Za-z]{2}", country_code):
            raise RuntimeError("城市地理编码国家代码无效。")
        result = {"id": item.get("id"), "name": item["name"], "latitude": latitude,
                  "longitude": longitude, "timezone": city_timezone, "country_code": country_code.upper()}
        for key in ("country", "admin1", "admin2", "admin3", "admin4"):
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                raise RuntimeError("城市地理编码行政区格式无效。")
            result[key] = value
        return result

    async def forecast(self, city: str, date: str, country_code: str | None = None) -> dict:
        if self._closed:
            raise RuntimeError("天气客户端已关闭。")
        if not isinstance(city, str) or not 1 <= len(city.strip()) <= 200:
            raise RuntimeError("请提供有效城市名称。")
        city = city.strip()
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise RuntimeError("天气日期必须为YYYY-MM-DD。")
        try:
            requested = Date.fromisoformat(date)
        except ValueError:
            raise RuntimeError("天气日期无效。") from None
        if country_code is not None:
            if not isinstance(country_code, str) or not re.fullmatch(r"[A-Za-z]{2}", country_code):
                raise RuntimeError("国家代码必须为两个字母。")
            country_code = country_code.upper()
        params = {"name": city, "count": 10, "format": "json",
                  "language": "zh" if re.search(r"[\u3400-\u9fff]", city) else "en"}
        if country_code is not None:
            params["countryCode"] = country_code
        geocoding, geocoding_source_url = await self._json(GEOCODING_URL, params)
        results = geocoding.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("城市地理编码结果格式无效。")
        candidates, seen = [], set()
        for item in results:
            candidate = self._location(item)
            if country_code and candidate["country_code"] != country_code:
                continue
            fingerprint = tuple(candidate.get(key) for key in ("name", "country_code", "admin1", "admin2", "admin3", "admin4", "latitude", "longitude", "timezone"))
            if fingerprint not in seen:
                seen.add(fingerprint)
                candidates.append(candidate)
        # The geocoder also returns prefix matches: do not silently choose a
        # different city or a district when the requested exact city is present.
        exact = [candidate for candidate in candidates if _name(candidate["name"]) == _name(city.split(",", 1)[0])]
        if exact:
            candidates = exact
        raw = {"geocoding": geocoding, "forecast": None}
        if len(candidates) != 1 or not exact:
            return {"ok": False, "needs_clarification": True,
                    "reason": "请明确城市所在的国家或行政区。" if candidates else "没有找到可确认的城市，请提供国家或更完整城市名称。",
                    "candidates": candidates, "date": date, "queried_at": self._now().isoformat(),
                    "source_url": geocoding_source_url, "geocoding_source_url": geocoding_source_url, "raw": raw}
        location = candidates[0]
        today = self._now().astimezone(ZoneInfo(location["timezone"])).date()
        if not 0 <= (requested - today).days <= MAX_FORECAST_DAY_OFFSET:
            raise RuntimeError("仅支持城市当地今天至未来十五天的预报，不使用历史或均温替代。")
        forecast, source_url = await self._json(FORECAST_URL, {
            "latitude": location["latitude"], "longitude": location["longitude"],
            "start_date": date, "end_date": date, "temperature_unit": "celsius",
            "daily": "temperature_2m_min,temperature_2m_max", "timezone": location["timezone"],
        })
        if forecast.get("timezone") != location["timezone"]:
            raise RuntimeError("天气响应时区与城市不一致。")
        daily, units = forecast.get("daily"), forecast.get("daily_units")
        if not isinstance(daily, dict) or not isinstance(units, dict) or daily.get("time") != [date]:
            raise RuntimeError("天气响应日期缺失或与请求不一致。")
        if units.get("temperature_2m_min") != "°C" or units.get("temperature_2m_max") != "°C":
            raise RuntimeError("天气响应温度单位不是摄氏度。")
        minimum, maximum = daily.get("temperature_2m_min"), daily.get("temperature_2m_max")
        if (not isinstance(minimum, list) or not isinstance(maximum, list) or len(minimum) != 1 or len(maximum) != 1
                or not _number(minimum[0]) or not _number(maximum[0]) or minimum[0] > maximum[0]):
            raise RuntimeError("天气响应最低或最高温度无效。")
        raw["forecast"] = forecast
        return {"ok": True, "location": location, "date": date, "timezone": location["timezone"],
                "temperature_min": minimum[0], "temperature_max": maximum[0], "unit": "°C",
                "data_type": "forecast", "queried_at": self._now().isoformat(),
                "source_url": source_url, "geocoding_source_url": geocoding_source_url, "raw": raw}

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self.client.aclose()
