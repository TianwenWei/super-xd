"""Build a task result only from the supplied, accepted evidence ledger."""
from copy import deepcopy
from super_xd.nlp.prompts.config import config_text, validate_config
from super_xd.nlp.prompts.speech_text import speech_text, spoken_number
from super_xd.nlp.policy.research import adopt_result_sources


def _forecast_sentence(records, official_status):
    parts = [config_text("speech/thinker.toml", "forecast.record",
                         location=str((record.get('location') or {}).get('name', '')),
                         date=str(record['date']),
                         minimum=spoken_number(record['temperature_min']),
                         maximum=spoken_number(record['temperature_max'])) for record in records]
    sentence = (config_text("speech/thinker.toml", "forecast.record_separator").join(parts)
                + config_text("speech/thinker.toml", "forecast.source_suffix")) if parts else ""
    if official_status == "not_verified":
        sentence += config_text("speech/thinker.toml", "forecast.official_not_verified")
    return sentence


def finish_task_result(args, sources, suggestions, observations, *, adopted_forecasts=()):
    urls, used_sources, web_attempts, evidence = adopt_result_sources(args, sources, observations)
    summary = args['summary']
    if used_sources and evidence == "none":
        evidence = "search_only"
        summary += config_text("speech/thinker.toml", "restrictions.search_only")
    elif not used_sources and web_attempts:
        summary += config_text("speech/thinker.toml", "restrictions.no_sources")
    # Produce the concise spoken form in the same finish call. The
    # shared guard only removes display syntax; legacy reports without
    # a generated spoken summary retain every statement and condition,
    # with no sentence/character cutoff or second rewrite model call.
    # Original reports and raw model/tool output remain in result/trace.
    spoken_summary = speech_text(args.get("spoken_summary") or args["summary"])
    if not spoken_summary:
        spoken_summary = speech_text(args["summary"])
    # Limits come from the evidence ledger, not from reading the prose;
    # a repeated caveat is cheaper than a dropped one.
    forecast = _forecast_sentence(list(adopted_forecasts), args.get("official_source_status"))
    if forecast:
        summary = forecast + "\n" + summary
        spoken_summary = forecast + spoken_summary
    if evidence == "search_only":
        spoken_summary += config_text("speech/thinker.toml", "spoken.search_only")
    elif not used_sources and web_attempts:
        spoken_summary += config_text("speech/thinker.toml", "spoken.no_sources")
    if suggestions:
        spoken_summary += config_text("speech/thinker.toml", "spoken.suggestions")
    queried = [source.get("queried_at") for source in used_sources if source.get("queried_at")]
    return {"ok": True, "result": {"summary": summary, "spoken_summary": spoken_summary,
            "sources": used_sources,
            "suggested_actions": deepcopy(suggestions), "query_times": sorted(set(queried)),
            "web_evidence": evidence,
            "forecast_records": [deepcopy(item["record"]) for item in observations
                if item["tool"] == "get_weather_forecast" and item.get("ok") and item["record"]["source_url"] in urls],
            "tool_observations": deepcopy(observations)}}


validate_config("speech/thinker.toml", {
    "cancelled": set(),
    "task_moved_background": set(),
    "quick_budget_exhausted": set(),
    "lookup_no_sources": set(),
    "processing": set(),
    "suggestion_saved": set(),
    "source_heading": set(),
    "source_item": {"title", "url"},
    "forecast.record": {"date", "location", "maximum", "minimum"},
    "forecast.record_separator": set(),
    "forecast.source_suffix": set(),
    "forecast.official_not_verified": set(),
    "forecast.source_title": set(),
    "forecast.geocoding_title": set(),
    "restrictions.search_only": set(),
    "restrictions.no_sources": set(),
    "spoken.search_only": set(),
    "spoken.no_sources": set(),
    "spoken.suggestions": set(),
})
