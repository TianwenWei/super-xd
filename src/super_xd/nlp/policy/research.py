"""Pure source and forecast evidence rules for read-only research."""

from datetime import datetime, timezone
from copy import deepcopy
import math
from typing import Any
from urllib.parse import urlparse
from super_xd.nlp.textutil import is_domain, find_urls


def weather_task(task):
    """The delegating Talker declares the domain; the goal text is never parsed."""
    return (task.get("context") or {}).get("query_domain") == "weather"


def forecast_raw_matches(record, raw, verifier):
    if verifier is None:
        raise RuntimeError("Forecast evidence verifier is not configured")
    return verifier(record, raw)


def verified_forecast_records(observations, verifier):
    return [
        item["record"]
        for item in observations
        if item.get("tool") == "get_weather_forecast"
        and item.get("ok") is True
        and item.get("forecast_verified") is True
        and forecast_raw_matches(
            item.get("record", {}), item.get("raw_forecast", {}), verifier
        )
    ]


def required_source_domains(task):
    """Official domains named by the Talker in structured task context."""
    domains = (task.get("context") or {}).get("required_source_domains") or []
    return list(
        dict.fromkeys(domain.lower() for domain in domains if is_domain(domain))
    )


def source_in_domains(url, domains):
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == domain.lower() or host.endswith("." + domain.lower())
        for domain in domains
    )


def weather_finish_guard(args, task, sources, observations, *, verifier=None):
    """Check structured forecast claims against what the tools actually returned.

    Numbers the user hears come from ``forecast_claims`` (rendered by
    ``_forecast_sentence``), so only those structured fields need checking.
    """
    if not weather_task(task):
        return []
    records = verified_forecast_records(observations, verifier)
    adopted = []
    for claim in args.get("forecast_claims") or []:
        match = next(
            (
                record
                for record in records
                if record["date"] == claim["date"]
                and record["source_url"] == claim["source_url"]
                and abs(record["temperature_min"] - claim["temperature_min"]) < 1e-6
                and abs(record["temperature_max"] - claim["temperature_max"]) < 1e-6
            ),
            None,
        )
        if match is None:
            raise ValueError(
                "forecast_claims必须与本版本get_weather_forecast实际返回的日期、来源和最低/最高温度完全一致。"
            )
        adopted.append(match)
    domains = required_source_domains(task)
    status = args.get("official_source_status")
    if domains and status not in {"verified", "not_verified"}:
        raise ValueError(
            "任务指定了官方来源，finish_task须填写official_source_status。"
        )
    if status == "verified" and not any(
        source.get("extracted") is True
        and source_in_domains(source.get("url", ""), domains)
        for source in sources.values()
    ):
        raise ValueError(
            "official_source_status=verified需要本任务真实读取到指定官方域名的页面；未读到请填not_verified。"
        )
    return adopted


def collect_sources(data: dict[str, Any], sources: dict[str, Any], kind: str) -> None:
    # Dates come from the tool response, not model-generated arguments.
    queried_at = data.get("queried_at") or datetime.now(timezone.utc).isoformat()
    for row in data.get("results", []):
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if (
            not isinstance(url, str)
            or urlparse(url).scheme not in ("http", "https")
            or not urlparse(url).netloc
        ):
            continue
        prior = sources.get(url, {})
        sources[url] = {
            "url": url,
            "title": row.get("title") or prior.get("title", ""),
            "queried_at": queried_at,
            "kind": kind,
            "extracted": kind == "extract" or prior.get("extracted", False),
        }


def validate_forecast_response(data, arguments, verifier):
    """Only externally verified, sourced raw records can support forecast claims."""
    if (
        data.get("ok") is not True
        or data.get("data_type") != "forecast"
        or data.get("date") != arguments["date"]
    ):
        raise ValueError("天气工具未返回所请求日期的真实forecast记录，不能当预报。")
    low, high = data.get("temperature_min"), data.get("temperature_max")
    if (
        any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in (low, high)
        )
        or low > high
    ):
        raise ValueError("天气工具的温度记录无效，不能当预报。")
    raw_forecast = (data.get("raw") or {}).get("forecast")
    if not isinstance(raw_forecast, dict) or not forecast_raw_matches(
        data, raw_forecast, verifier
    ):
        raise ValueError(
            "天气工具的date/timezone/摄氏单位或温度与原始raw不一致，不能补造或当作已验证预报。"
        )
    url = data.get("source_url", "")
    parsed = urlparse(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or not data.get("queried_at")
        or not data.get("timezone")
        or data.get("unit") != "°C"
    ):
        raise ValueError("天气工具缺少真实来源、查询时间、时区或摄氏单位，不能当预报。")
    geo_url = data.get("geocoding_source_url")
    if geo_url:
        geo = urlparse(geo_url)
        if geo.scheme not in ("http", "https") or not geo.netloc:
            raise ValueError("天气工具的地理编码来源 URL 无效。")
    record = {
        key: deepcopy(data[key])
        for key in (
            "date",
            "location",
            "timezone",
            "temperature_min",
            "temperature_max",
            "unit",
            "source_url",
            "queried_at",
        )
    }
    return record


def adopt_result_sources(args, sources, observations):
    """Adopt only provenance-backed references and declare evidence limits."""
    urls = args.get("source_urls", list(sources)[:20])
    unknown = [url for url in urls if url not in sources]
    # Apply the same ledger constraint to prose/Markdown citations. A
    # correct structured list must not hide an invented URL in summary.
    inline_urls = [
        url.rstrip(".,;:，。；：！？!?）】》*`")
        for url in find_urls(args["summary"] + "\n" + args.get("spoken_summary", ""))
    ]
    unknown += [url for url in inline_urls if url not in sources]
    if unknown:
        raise ValueError("无法引用未经本任务真实工具查询的来源 URL。")
    used_sources = [sources[url] for url in urls]
    web_attempts = [
        item
        for item in observations
        if item["tool"] in ("search_web", "extract_web", "get_weather_forecast")
    ]
    extracts = [item for item in observations if item["tool"] == "extract_web"]
    if any(source.get("kind") == "geocoding" for source in used_sources) and not any(
        source.get("kind") == "forecast" for source in used_sources
    ):
        raise ValueError(
            "地理编码来源只能说明地点，不能单独支撑天气预报；请引用实际forecast来源，无法核实则如实说明。"
        )
    if (
        any(
            source.get("kind") not in {"forecast", "geocoding"}
            for source in used_sources
        )
        and not extracts
    ):
        raise ValueError(
            "已找到真实网页来源，请先extract_web读取核实；提取失败也要如实报告。"
        )
    evidence = (
        "extracted"
        if used_sources and all(source.get("extracted") for source in used_sources)
        else "none"
    )
    if (
        used_sources
        and any(source.get("kind") == "forecast" for source in used_sources)
        and all(
            source.get("kind") in {"forecast", "geocoding"} for source in used_sources
        )
    ):
        evidence = "forecast"
    elif used_sources and all(
        source.get("extracted") or source.get("verified") for source in used_sources
    ):
        evidence = (
            "verified"
            if any(source.get("kind") == "forecast" for source in used_sources)
            else "extracted"
        )
    return urls, used_sources, web_attempts, evidence
