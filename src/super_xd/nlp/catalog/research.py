"""Read-only research tool contracts; wording is compiled separately."""

from typing import Any
from super_xd.nlp.catalog.vehicle import SKILLS

DATE_PATTERN = "^\\d{4}-\\d{2}-\\d{2}$"


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _tool(name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": parameters}}


THINKER_TOOLS = [
    _tool(
        "search_web",
        _object(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 1500},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            ["query"],
        ),
    ),
    _tool(
        "extract_web",
        _object(
            {
                "urls": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 8},
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                }
            },
            ["urls"],
        ),
    ),
    _tool(
        "load_skill",
        _object({"skill_id": {"type": "string", "enum": sorted(SKILLS)}}, ["skill_id"]),
    ),
    _tool(
        "ask_clarification",
        _object(
            {"question": {"type": "string", "minLength": 1, "maxLength": 1500}},
            ["question"],
        ),
    ),
    _tool(
        "suggest_vehicle_action",
        _object(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "arguments": {"type": "object"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1500},
            },
            ["name", "arguments", "reason"],
        ),
    ),
    _tool(
        "finish_task",
        _object(
            {
                "summary": {"type": "string", "minLength": 1, "maxLength": 12000},
                "spoken_summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                "reuse_prior_evidence": {"type": "boolean"},
                "forecast_claims": {
                    "type": "array",
                    "maxItems": 7,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "date",
                            "source_url",
                            "temperature_min",
                            "temperature_max",
                        ],
                        "properties": {
                            "date": {"type": "string", "pattern": DATE_PATTERN},
                            "source_url": {"type": "string", "minLength": 8},
                            "temperature_min": {"type": "number"},
                            "temperature_max": {"type": "number"},
                        },
                    },
                },
                "official_source_status": {
                    "type": "string",
                    "enum": ["verified", "not_verified"],
                },
                "source_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "uniqueItems": True,
                },
            },
            ["summary"],
        ),
    ),
]
FORECAST_TOOL = _tool(
    "get_weather_forecast",
    _object(
        {
            "city": {"type": "string", "minLength": 1, "maxLength": 120},
            "date": {"type": "string", "pattern": DATE_PATTERN},
            "country_code": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
        },
        ["city", "date"],
    ),
)
THINKER_TOOLS.append(FORECAST_TOOL)
TOOL_SCHEMAS = {
    tool["function"]["name"]: tool["function"]["parameters"] for tool in THINKER_TOOLS
}
LOOKUP_TOOL = _tool(
    "lookup_web",
    _object(
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 1500},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 3},
        },
        ["query"],
    ),
)
QUICK_TOOLS = [FORECAST_TOOL, LOOKUP_TOOL] + [
    tool
    for tool in THINKER_TOOLS
    if tool["function"]["name"] in {"ask_clarification", "finish_task"}
]
TOOL_SCHEMAS["lookup_web"] = LOOKUP_TOOL["function"]["parameters"]
