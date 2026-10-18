"""Shared explicit fixtures for worker tests."""
from tests.support.models import scripted_result

import asyncio

from copy import deepcopy

import json


import pytest

from super_xd.demo.tasks.database import Database


from super_xd.demo.tasks.worker import ThinkerWorker

from super_xd.nlp.prompts.research import THINKER_TOOLS, QUICK_TOOLS


from super_xd.demo.resources.runtime import local_model_policy_id, local_task_budget

from super_xd.nlp.release import get_release_id


def message(name, arguments, call_id="call_1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class FakeArk:
    def info(self):
        return {"fixture_policy": "offline-worker-v1"}

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.thinker_modes = []
        self.options = []

    async def complete(self, request):
        messages = request.messages
        tools = request.tools
        thinker = request.role == 'thinker'
        options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
        self.calls.append(deepcopy(messages))
        self.thinker_modes.append(thinker)
        self.options.append({"tools": deepcopy(tools), **options})
        allowed = THINKER_TOOLS if thinker else QUICK_TOOLS
        assert all(
            tool["function"]["name"] in {item["function"]["name"] for item in allowed}
            for tool in tools
        )
        if not self.responses:
            raise AssertionError("Fake script exhausted")
        response = self.responses.pop(0)
        if callable(response):
            return scripted_result(await response(messages))
        return scripted_result(response)


class FakeTavily:
    def __init__(self, results=None):
        self.results = (
            results
            if results is not None
            else [
                {
                    "url": "https://example.org/official",
                    "title": "Official",
                    "content": "A fact",
                }
            ]
        )
        self.calls = []

    async def search(self, query, max_results=5):
        self.calls.append(("search", query))
        return {"results": self.results, "queried_at": "2026-10-11T10:00:00+00:00"}

    async def extract(self, urls):
        self.calls.append(("extract", urls))
        return {
            "results": [
                {"url": url, "raw_content": "Actual page content"} for url in urls
            ],
            "queried_at": "2026-10-11T10:00:01+00:00",
        }


@pytest.fixture
def db(tmp_path):
    return RegisteredDatabase(tmp_path / "worker.sqlite3")


def registration_context(context=None, *, model=None, worker_options=None):
    return {
        "release_id": get_release_id(),
        "model_policy_id": local_model_policy_id(
            model or FakeArk(), local_task_budget(**(worker_options or {}))
        ),
        **(context or {}),
    }


class RegisteredDatabase(Database):
    """Fixture registrations pass the same identity gate as the local service."""

    def create_task(self, session_id, goal, context=None, **options):
        return super().create_task(
            session_id, goal, registration_context(context), **options
        )


def task(db, goal="查找当前公开信息", context=None, *, model=None, worker_options=None):
    created = db.create_task(
        "test-session",
        goal,
        context=registration_context(
            context, model=model, worker_options=worker_options
        ),
    )
    claimed = db.claim_next()
    assert claimed["id"] == created["id"]
    return claimed


async def wait_until(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


class FakeWeather:
    def __init__(self, response=None):
        self.calls = []
        self.closed = False
        self.response = response or {
            "ok": True,
            "data_type": "forecast",
            "queried_at": "2026-10-13T05:00:00+00:00",
            "date": "2026-10-14",
            "location": {"name": "Paris", "country_code": "FR"},
            "timezone": "Europe/Paris",
            "temperature_min": 12.5,
            "temperature_max": 18.1,
            "unit": "°C",
            "source_url": "https://api.open-meteo.com/v1/forecast?start_date=2026-10-14",
            "geocoding_source_url": "https://geocoding-api.open-meteo.com/v1/search?name=Paris&countryCode=FR",
            "raw": {
                "geocoding": {"results": ["actual fake fixture"]},
                "forecast": {
                    "timezone": "Europe/Paris",
                    "daily_units": {
                        "temperature_2m_min": "°C",
                        "temperature_2m_max": "°C",
                    },
                    "daily": {
                        "time": ["2026-10-14"],
                        "temperature_2m_min": [12.5],
                        "temperature_2m_max": [18.1],
                    },
                },
            },
        }

    async def forecast(self, city, date, country_code=None):
        self.calls.append((city, date, country_code))
        if isinstance(self.response, Exception):
            raise self.response
        return deepcopy(self.response)

    async def aclose(self):
        self.closed = True


async def weather_guard_evidence(db):
    sources, observations = {}, []
    worker = ThinkerWorker(db, FakeArk(), FakeTavily(), weather=FakeWeather())
    await worker._execute_tool(
        "get_weather_forecast",
        {"city": "Paris", "date": "2026-10-14"},
        sources,
        [],
        observations,
    )
    return sources, observations


async def weather_guard_evidence(db):
    sources, observations = {}, []
    worker = ThinkerWorker(db, FakeArk(), FakeTavily(), weather=FakeWeather())
    await worker._execute_tool(
        "get_weather_forecast",
        {"city": "Paris", "date": "2026-10-14"},
        sources,
        [],
        observations,
    )
    return sources, observations


def claim(**overrides):
    value = {
        "date": "2026-10-14",
        "source_url": FakeWeather().response["source_url"],
        "temperature_min": 12.5,
        "temperature_max": 18.1,
    }
    return {**value, **overrides}
