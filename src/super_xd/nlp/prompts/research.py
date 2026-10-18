"""Compile research wording and fixed role messages at startup."""

from copy import deepcopy
from datetime import datetime, timezone
import json

from super_xd.nlp.catalog import research
from super_xd.nlp.contracts.task_state import TaskState
from super_xd.nlp.prompts.catalog import SKILLS
from super_xd.nlp.prompts.config import config_text, validate_config
from super_xd.nlp.prompts.templates import render_prompt
from super_xd.nlp.policy.speaker import speaker_context

validate_config(
    "thinker/tools.toml",
    {
        "search_web.description": set(),
        "extract_web.description": set(),
        "load_skill.description": set(),
        "ask_clarification.description": set(),
        "suggest_vehicle_action.description": set(),
        "finish_task.description": set(),
        "finish_task.parameters.spoken_summary": set(),
        "finish_task.parameters.reuse_prior_evidence": set(),
        "finish_task.parameters.forecast_claims": set(),
        "finish_task.parameters.official_source_status": set(),
        "finish_task.parameters.source_urls": set(),
        "get_weather_forecast.description": set(),
        "get_weather_forecast.parameters.date": set(),
        "get_weather_forecast.parameters.country_code": set(),
        "lookup_web.description": set(),
    },
)


def _compile(tools):
    compiled = deepcopy(tools)
    fields = {
        "finish_task": (
            "spoken_summary",
            "reuse_prior_evidence",
            "forecast_claims",
            "official_source_status",
            "source_urls",
        ),
        "get_weather_forecast": ("date", "country_code"),
    }
    for tool in compiled:
        spec = tool["function"]
        name = spec["name"]
        spec["description"] = config_text("thinker/tools.toml", name + ".description")
        for field in fields.get(name, ()):
            spec["parameters"]["properties"][field]["description"] = config_text(
                "thinker/tools.toml", f"{name}.parameters.{field}"
            )
    return compiled


THINKER_TOOLS = _compile(research.THINKER_TOOLS)
QUICK_TOOLS = _compile(research.QUICK_TOOLS)
SKILL_GUIDE = "；".join(
    f"{identity}：{SKILLS[identity].name}" for identity in sorted(SKILLS)
)
QUICK_PROMPT = render_prompt("thinker_quick")
SYSTEM_PROMPT = render_prompt("thinker_research", skill_guide=SKILL_GUIDE)
DELIVERY_PROMPTS = {
    mode: render_prompt("reasoner_" + mode) for mode in ("foreground", "background")
}


def apply_delivery_mode(messages, task):
    """Only the delivery instruction changes; evidence and task identity stay put."""
    prompt = DELIVERY_PROMPTS[task.get("delivery_mode", "background")]
    for message in messages:
        if (
            message.get("role") == "system"
            and message.get("content") in DELIVERY_PROMPTS.values()
        ):
            message["content"] = prompt
            return messages
    messages.insert(1, {"role": "system", "content": prompt})
    return messages


def query_mode(task: TaskState) -> str:
    return (
        "quick"
        if (task.get("context") or {}).get("query_mode") == "quick"
        else "research"
    )


def research_messages(task: TaskState, *, current_utc: str | None = None):
    context = {
        "goal": task["goal"],
        "constraints": task.get("constraints", []),
        "revision": task["revision"],
        "query_mode": query_mode(task),
        "current_utc": current_utc or datetime.now(timezone.utc).isoformat(),
    }
    if task.get("context"):
        context["context"] = deepcopy(task["context"])
        if isinstance(context["context"].get("speaker"), dict):
            context["context"]["speaker"].pop("control_defaults", None)
    prompt = QUICK_PROMPT if query_mode(task) == "quick" else SYSTEM_PROMPT
    messages = [{"role": "system", "content": prompt}]
    speaker = (task.get("context") or {}).get("speaker")
    if speaker:
        defaults = speaker_context(speaker["seat"])
        defaults.pop("control_defaults")
        messages.append(
            {
                "role": "system",
                "content": render_prompt(
                    "speaker_context",
                    speaker_json=json.dumps(defaults, ensure_ascii=False),
                ),
            }
        )
    messages.append(
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
    )
    return apply_delivery_mode(messages, task)
