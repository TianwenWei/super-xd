"""Shared explicit fixtures for session tests."""

from super_xd.demo.audio.providers import PCM24K_TTS
import asyncio

import json

import copy

import pytest

from super_xd.demo.tasks.database import Database

from super_xd.demo.session.actor import SessionActor
from super_xd.demo.session.state import Turn


from tests.support.vehicle import FakeVehiclePlanner


def call(name, arguments=None, id="call_1"):
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


class FakeArk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        self.requests.append(
            {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            delay, response = response
            await asyncio.sleep(delay)
        if response.get("content"):
            yield {"type": "delta", "text": response["content"]}
        yield {"type": "message", "message": {"role": "assistant", **response}}


def configure_task_fixtures(actor):
    """Seed tasks as registrations under the same frozen NLP deployment."""
    create = actor.db.create_task

    def registered(session_id, goal, context=None, **options):
        context = {
            "release_id": actor.nlp.release_id,
            "model_policy_id": actor.nlp.model_policy_id,
            **(context or {}),
        }
        return create(session_id, goal, context, **options)

    actor.db.create_task = registered


async def manage_task(actor, arguments):
    from uuid import uuid4
    from super_xd.nlp.contracts.run import ToolRequest
    from super_xd.nlp.policy.product import normalize_task_arguments, ProductPolicyError

    try:
        normalized = normalize_task_arguments(arguments, actor.task_list())
    except ProductPolicyError as error:
        return error.result
    turn = Turn("task-read", "查询任务进度", actor.persona, actor.persona_version)
    request = ToolRequest(
        invocation_id=uuid4().hex,
        tool_call_id="task-read",
        name="manage_task",
        arguments=arguments,
        idempotency_key=uuid4().hex,
    )
    return await actor.nlp.task_service.handle(
        actor.nlp_start_request(turn), request, normalized
    )


@pytest.fixture
async def make_actor(tmp_path):
    actors = []

    async def make(responses, vehicle_planner=None):
        ark = FakeArk(responses)
        db = Database(tmp_path / f"{len(actors)}.sqlite3")
        actor = SessionActor(
            f"session{len(actors)}",
            db,
            ark,
            vehicle_planner=vehicle_planner or FakeVehiclePlanner(),
        )
        configure_task_fixtures(actor)
        actor.simulator.execution_ms = actor.simulator.network_rtt_ms = 0
        actors.append(actor)
        outgoing = await actor.attach()
        await outgoing.get()  # initial snapshot
        return actor, outgoing, ark

    yield make
    await asyncio.gather(*(a.close() for a in actors))


def sync_tasks(actor):
    """Mirror rows written straight to SQLite, as the actor's event poller would."""
    for task in actor.db.list_tasks(actor.id):
        actor.remember_task(task)


async def wait_for(q, type_, ack_actor=None, timeout=2):
    events = []
    async with asyncio.timeout(timeout):
        while True:
            event = await q.get()
            events.append(event)
            if event["type"] == "vehicle.command" and ack_actor:
                receipt = await ack_actor.simulator.execute(event["command_id"])
                await ack_actor.post(
                    {
                        "type": "vehicle.receipt",
                        "command_id": event["command_id"],
                        "receipt_id": receipt["receipt_id"],
                    }
                )
            if event["type"] == type_:
                return event, events


def weather_tasks(actor):
    ordinary = actor.db.create_task(actor.id, "查询法国巴黎明天温度")
    actor.db.save_task(
        ordinary["id"],
        1,
        status="completed",
        summary="Open-Meteo预报已核实",
        result={
            "summary": "完整普通天气报告。",
            "spoken_summary": "巴黎明天11.2到19.3度，来自Open-Meteo。",
        },
    )
    official = actor.db.create_task(
        actor.id, "查询法国气象局官网，核对法国巴黎明天温度"
    )
    actor.db.save_task(
        official["id"],
        1,
        status="waiting_clarification",
        summary="等待官网条件",
        question="请提供法国气象局官网网址。",
    )
    sync_tasks(actor)
    return actor.db.get_task(ordinary["id"]), actor.db.get_task(official["id"])


class HeldDeltaArk(FakeArk):
    def __init__(self, responses):
        super().__init__(responses)
        self.delta_sent = asyncio.Event()

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        self.requests.append(
            {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)}
        )
        response = copy.copy(self.responses.pop(0))
        gate = response.pop("gate", None)
        if response.get("content"):
            yield {"type": "delta", "text": response["content"]}
            self.delta_sent.set()
        if gate:
            await gate.wait()
        yield {
            "type": "message",
            "message": {"role": "assistant", **response},
            "meta": {"test_provider": "actual_fake_return"},
        }


class SpeechRecorder:
    output_format = PCM24K_TTS

    def __init__(self):
        self.texts = []

    async def stream_synthesize(self, texts, voice=None):
        async for text in texts:
            self.texts.append(text)
            yield {"type": "audio", "audio": b"\x01\x00"}
