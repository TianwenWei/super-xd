"""Read-tool dispatch through injected ports, separate from task progression."""

from copy import deepcopy
from typing import Any, Callable
from urllib.parse import urlparse
from jsonschema import Draft202012Validator

from super_xd.nlp.orchestration.tasks.ports import ResearchSearch, ResearchForecast
from super_xd.nlp.orchestration.tasks.results import finish_task_result
from super_xd.nlp.policy.research import collect_sources, validate_forecast_response
from super_xd.nlp.prompts.config import config_text


class ResearchTools:
    def __init__(
        self,
        search: ResearchSearch,
        weather: ResearchForecast | None,
        *,
        forecast_verifier: Callable[[dict, dict], bool] | None,
        catalog=None,
    ):
        self.search, self.weather = search, weather
        self.forecast_verifier, self.catalog = forecast_verifier, catalog

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        sources: dict[str, Any],
        suggestions: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        *,
        source_domains=None,
        adopted_forecasts=(),
    ) -> dict[str, Any]:
        if name == "get_weather_forecast":
            if self.weather is None:
                raise RuntimeError("Weather tool is not configured")
            if self.forecast_verifier is None:
                raise RuntimeError("Forecast evidence verifier is not configured")
            data = await self.weather.forecast(
                args["city"], args["date"], country_code=args.get("country_code")
            )
            if data.get("ok") is False:
                observations.append(
                    {
                        "tool": name,
                        "ok": False,
                        "attempted": True,
                        "needs_clarification": bool(data.get("needs_clarification")),
                    }
                )
                return data
            record = validate_forecast_response(data, args, self.forecast_verifier)
            raw_forecast = data["raw"]["forecast"]
            url, geo_url = data["source_url"], data.get("geocoding_source_url")
            sources[url] = {
                "url": url,
                "title": config_text("speech/thinker.toml", "forecast.source_title"),
                "queried_at": data["queried_at"],
                "kind": "forecast",
                "verified": True,
                "extracted": False,
                "forecast_date": data["date"],
                "timezone": data["timezone"],
                "unit": data["unit"],
            }
            if geo_url:
                sources[geo_url] = {
                    "url": geo_url,
                    "title": config_text(
                        "speech/thinker.toml", "forecast.geocoding_title"
                    ),
                    "queried_at": data["queried_at"],
                    "kind": "geocoding",
                    "verified": True,
                    "extracted": False,
                    "supports": "location",
                }
            observations.append(
                {
                    "tool": name,
                    "ok": True,
                    "attempted": True,
                    "record": record,
                    "queried_at": data["queried_at"],
                    "forecast_verified": True,
                    "raw_forecast": deepcopy(raw_forecast),
                }
            )
            return data
        if name == "search_web":
            options = {"include_domains": source_domains} if source_domains else {}
            data = await self.search.search(
                args["query"], max_results=args.get("max_results", 5), **options
            )
            collect_sources(data, sources, "search")
            observations.append(
                {
                    "tool": name,
                    "query": args["query"],
                    "ok": True,
                    "attempted": True,
                    "result_count": len(data.get("results", [])),
                    "queried_at": data.get("queried_at"),
                }
            )
            return {"ok": True, **data}
        if name == "extract_web":
            for url in args["urls"]:
                parsed = urlparse(url)
                if (
                    parsed.scheme not in ("https", "http")
                    or not parsed.netloc
                    or url not in sources
                ):
                    raise ValueError("只能读取本任务真实搜索获得的 http(s) 来源 URL。")
            data = await self.search.extract(args["urls"])
            collect_sources(data, sources, "extract")
            observations.append(
                {
                    "tool": name,
                    "ok": True,
                    "attempted": True,
                    "result_count": len(data.get("results", [])),
                    "failed_results": data.get("failed_results", []),
                    "queried_at": data.get("queried_at"),
                }
            )
            return {"ok": True, **data}
        if name == "load_skill":
            from super_xd.nlp.prompts.catalog import load_skill

            return load_skill(args["skill_id"])
        if name == "ask_clarification":
            return {"ok": True, "question": args["question"]}
        if name == "suggest_vehicle_action":
            if self.catalog is None:
                from super_xd.nlp.catalog.vehicle import TOOLS

                self.catalog = TOOLS
            spec = self.catalog.get(args["name"])
            if spec is None:
                raise ValueError(
                    "未知模拟车辆函数；请先按分类ID用load_skill查看有效函数。"
                )
            schema = (
                spec.parameters if hasattr(spec, "parameters") else spec["parameters"]
            )
            errors = list(Draft202012Validator(schema).iter_errors(args["arguments"]))
            if errors:
                raise ValueError("模拟动作参数无效：" + errors[0].message)
            suggestions.append(deepcopy(args))
            return {
                "ok": True,
                "status": "suggested_only",
                "executed": False,
                "message": config_text("speech/thinker.toml", "suggestion_saved"),
            }
        if name == "finish_task":
            return finish_task_result(
                args,
                sources,
                suggestions,
                observations,
                adopted_forecasts=adopted_forecasts,
            )
        raise ValueError("Unknown tool")
