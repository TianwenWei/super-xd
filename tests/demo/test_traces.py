"""Local trace API and actor races, using explicitly injected model responses."""
import asyncio
import copy
import json
import threading

import httpx
import pytest

from super_xd.demo.api.app import actors, app
from super_xd.demo.tasks.database import Database
from super_xd.demo.session.actor import SessionActor
from tests.support.vehicle import FakeVehiclePlanner, plan_response, expected_talker_tool_names


def tool_call(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class ScriptedArk:
    """This is a test double; no model or external service is called."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        item = copy.deepcopy(self.responses.pop(0))
        if item["message"].get("content"):
            yield {"type": "delta", "text": item["message"]["content"]}
        yield {"type": "message", **item}


def observed(content=None, calls=None, **meta):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"message": message, "meta": {"usage": None, **meta}}


@pytest.fixture
async def trace_context(tmp_path):
    db = Database(tmp_path / "traces.sqlite3")
    created = []

    async def make(ark=None, session_id=None, vehicle_planner=None):
        actor = SessionActor(session_id or f"trace-session-{len(created)}", db, ark or ScriptedArk(),
                             vehicle_planner=vehicle_planner or FakeVehiclePlanner())
        actor.simulator.execution_ms = actor.simulator.network_rtt_ms = 0
        created.append(actor)
        actors[actor.id] = actor
        outgoing = await actor.attach()
        assert (await outgoing.get())["type"] == "session.snapshot"
        return actor, outgoing

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield make, client, db
    for actor in created:
        actors.pop(actor.id, None)
    await asyncio.gather(*(actor.close() for actor in created))


async def receive(queue, kind, actor=None):
    seen = []
    async with asyncio.timeout(3):
        while True:
            event = await queue.get()
            seen.append(event)
            if event["type"] == "vehicle.command" and actor:
                receipt = await actor.simulator.execute(event["command_id"])
                await actor.post({"type": "vehicle.receipt", "command_id": event["command_id"], "receipt_id": receipt["receipt_id"]})
            if event["type"] == kind:
                return event, seen


async def recorded(actor, **filters):
    await actor.queue.join()
    await actor.trace_queue.join()
    return actor.db.traces_after(actor.id, **filters)


async def test_vehicle_delegation_separates_talker_plan_and_actual_receipt_traces(trace_context):
    make, client, _ = trace_context
    ark = ScriptedArk([
        observed(calls=[tool_call("delegate_vehicle_control", {"skill_ids": ["mirrors"]}, "delegate-1")], provider_id="provider-first"),
    ])
    planner = FakeVehiclePlanner(plan_response(("set_mirror_heating", {"enabled": True})))
    actor, queue = await make(ark, vehicle_planner=planner)
    await actor.post({"type": "input", "text": "打开后视镜加热"})
    done, _ = await receive(queue, "stream.done", actor)
    events = await recorded(actor)
    started = [event for event in events if event["kind"] == "model.started" and event["data"].get("agent") == "talker"]
    completed = [event for event in events if event["kind"] == "model.completed" and event["data"].get("agent") == "talker"]
    assert len(started) == len(completed) == 1
    assert len({event["call_id"] for event in started}) == 1
    assert [event["data"]["model_call_index"] for event in completed] == [1]
    assert [event["call_id"] for event in completed] == [event["call_id"] for event in started]
    assert all(event["turn_id"] == done["turn_id"] for event in started + completed)
    assert all(event["data"]["message"]["content"] is None for event in completed)
    assert all(event["data"]["meta"]["usage"] is None for event in completed)
    assert all("reasoning_content" not in event["data"]["message"] for event in completed)
    tools = [event for event in events if event["kind"] == "tool.started"]
    assert [event["call_id"] for event in tools] == ["delegate-1"]
    assert [event["data"]["model_call_id"] for event in tools] == [event["call_id"] for event in started]
    assert [tool["function"]["name"] for tool in ark.requests[0]["tools"]] == expected_talker_tool_names()
    planner_return = next(event for event in events if event['kind'] == 'model.completed'
                          and event['data'].get('agent') == 'vehicle_planner')
    actual = next(event for event in events if event['kind'] == 'vehicle.receipt')
    outer = next(event for event in events if event['kind'] == 'tool.completed' and event['call_id'] == 'delegate-1')
    assert actual['data']['result']['ok'] is True
    assert planner_return['trace_id'] < actual['trace_id'] < outer['trace_id']
    assert outer['data']['result']['receipts'][0]['result']['executed'] is True
    assert len(planner.requests) == 1
    final = next(event for event in events if event["kind"] == "turn.completed")
    assert final["data"]["output_source"] == "verified_receipts"
    assert final["data"]["output"] == done["text"] and "后视镜加热开了" in done["text"]
    assert actor.vehicle["mirrors"]["heating"] is True
    response = await client.get(f"/api/sessions/{actor.id}/traces", params={"turn_id": done["turn_id"]})
    assert response.json()["events"] == events
    assert response.json()["recording_error"] is None


async def test_actual_long_content_and_returned_reasoning_are_not_truncated(trace_context):
    make, _, _ = trace_context
    content, reasoning = "完整返回内容。" * 900, "供应商实际返回的解释。" * 700
    item = observed(content, provider_id="observed-id", finish_reason="stop")
    item["message"]["reasoning_content"] = reasoning
    actor, queue = await make(ScriptedArk([item]))
    await actor.post({"type": "input", "text": "展示注入响应"})
    done, _ = await receive(queue, "stream.done")
    events = await recorded(actor)
    result = next(event for event in events if event["kind"] == "model.completed"
                  and event["data"]["message"].get("content") == content)  # skips the routing call
    assert result["data"]["message"] == item["message"]
    assert result["data"]["meta"]["usage"] is None
    assert done["text"] == content
    assert next(event for event in events if event["kind"] == "turn.completed")["data"]["output_source"] == "model_content"


class InterruptedArk:
    def __init__(self, late=None):
        self.calls = 0
        self.late = late
        self.release = asyncio.Event()
        # The routing call withholds deltas from the UI, so tests wait on this.
        self.streaming = asyncio.Event()

    async def stream(self, request):
        self.calls += 1
        if self.calls != 1:
            yield {"type": "delta", "text": "新轮次正常回答。"}
            yield {"type": "message", **observed("新轮次正常回答。", provider_id="new-provider")}
            return
        yield {"type": "delta", "text": "旧轮次已返回片段"}
        self.streaming.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError as exc:
            if self.late is None:
                exc.trace_partial = {"message": {"role": "assistant", "content": "旧轮次已返回片段"},
                                     "meta": {"usage": None, "provider_id": "old-provider"}}
                raise
            # Deliberately non-cooperative test transport: expose a callback that
            # finishes after cancellation without claiming this is live Ark.
            await self.release.wait()
        if self.late == "error":
            raise RuntimeError("old-call failed trace-test-key")
        yield {"type": "message", **observed("旧轮次迟到完整内容", calls=[
            tool_call("set_volume", {"volume": 88}, "must-not-execute")], provider_id="old-provider")}


async def test_cancelled_model_keeps_received_partial_under_old_turn(trace_context):
    make, _, _ = trace_context
    ark = InterruptedArk()
    actor, queue = await make(ark)
    await actor.post({"type": "input", "text": "旧输入"})
    await asyncio.wait_for(ark.streaming.wait(), 3)
    old = actor.current
    await actor.post({"type": "input", "text": "新输入"})
    done, _ = await receive(queue, "stream.done")
    await old.model_task
    events = await recorded(actor)
    cancelled = next(event for event in events if event["kind"] == "run.cancelled")
    started = next(event for event in events if event["kind"] == "model.started" and event["turn_id"] == old.id)
    assert cancelled["turn_id"] == old.id != done["turn_id"]
    assert cancelled["status"] == "stale" and cancelled["call_id"] == started["call_id"]
    assert cancelled["data"]["partial"]["message"]["content"] == "旧轮次已返回片段"
    assert cancelled["data"]["partial"]["meta"]["usage"] is None
    assert done["text"] == "新轮次正常回答。"
    assert any(event["kind"] == "turn.interrupted" and event["turn_id"] == old.id for event in events)


@pytest.mark.parametrize("late", ["message", "error"])
async def test_late_old_model_callback_is_retained_without_new_turn_pollution(trace_context, monkeypatch, late):
    make, _, _ = trace_context
    monkeypatch.setenv("ARK_API_KEY", "trace-test-key")
    ark = InterruptedArk(late)
    actor, queue = await make(ark)
    await actor.post({"type": "input", "text": "旧输入"})
    await asyncio.wait_for(ark.streaming.wait(), 3)
    old = actor.current
    await actor.post({"type": "input", "text": "新输入"})
    done, _ = await receive(queue, "stream.done")
    ark.release.set()
    await old.model_task
    events = await recorded(actor)
    kind = "run.cancelled"
    stale = next(event for event in events if event["kind"] == kind and event["turn_id"] == old.id)
    assert stale["status"] == "stale"
    assert stale["call_id"] != next(event["call_id"] for event in events
                                   if event["kind"] == "model.completed" and event["turn_id"] == done["turn_id"])
    if late == "message":
        assert stale["data"]["partial"]["late_return"]["message"]["content"] == "旧轮次迟到完整内容"
        assert stale["data"]["partial"]["late_return"]["message"]["tool_calls"][0]["id"] == "must-not-execute"
    else:
        assert "old-call failed" in stale["data"]["partial"]["late_error"]["message"]
        assert "trace-test-key" not in json.dumps(stale)
    assert done["text"] == actor.current.output == "新轮次正常回答。"
    assert not any(event["kind"] in {"tool.started", "vehicle.dispatched"} for event in events)
    assert actor.vehicle["media"]["volume"] != 88
    assert not any(event["type"] in {"stream.delta", "stream.done", "vehicle.command"} for event in list(queue._queue))


@pytest.mark.parametrize("late", ["message", "error"])
async def test_model_trace_status_follows_actor_queue_order(trace_context, late):
    make, _, _ = trace_context
    ark = InterruptedArk(late)
    actor, queue = await make(ark)
    await actor.post({"type": "input", "text": "旧输入"})
    await asyncio.wait_for(ark.streaming.wait(), 3)
    old = actor.current
    # The old network task resumes first, but its callback is enqueued after
    # this new input. Status must follow actor consumption, not network timing.
    ark.release.set()
    actor.queue.put_nowait({"type": "input", "text": "新输入"})
    done, _ = await receive(queue, "stream.done")
    await old.model_task
    events = await recorded(actor)
    kind = "run.cancelled"
    result = next(event for event in events if event["kind"] == kind and event["turn_id"] == old.id)
    assert old.interrupted and result["status"] == "stale"
    assert result["call_id"] == next(event["call_id"] for event in events
                                    if event["kind"] == "model.started" and event["turn_id"] == old.id)
    if late == "message":
        assert result["data"]["partial"]["late_return"]["message"]["content"] == "旧轮次迟到完整内容"
        assert result["data"]["partial"]["late_return"]["meta"]["usage"] is None
    else:
        assert "old-call failed" in result["data"]["partial"]["late_error"]["message"]
    assert done["text"] == actor.current.output == "新轮次正常回答。"
    assert not any(event["kind"] in {"tool.started", "vehicle.dispatched"} for event in events)


async def test_late_receipt_is_old_turn_stale_and_duplicate_does_not_add_trace(trace_context):
    make, _, _ = trace_context
    actor, queue = await make(ScriptedArk([
        observed(calls=[tool_call("set_volume", {"volume": 55}, "old-volume-call")]),
        observed("新轮次正常回答。"),
    ]))
    await actor.post({"type": "input", "text": "音量55"})
    command, _ = await receive(queue, "vehicle.command")
    old = actor.current
    await actor.post({"type": "input", "text": "新输入"})
    done, _ = await receive(queue, "stream.done")
    receipt = await actor.simulator.execute(command["command_id"])
    await actor.post({"type": "vehicle.receipt", "command_id": command["command_id"], "receipt_id": receipt["receipt_id"]})
    events = await recorded(actor)
    receipt = next(event for event in events if event["kind"] == "vehicle.receipt")
    assert receipt["turn_id"] == old.id != done["turn_id"]
    assert receipt["call_id"] == "old-volume-call"
    assert receipt["status"] == "stale" and receipt["data"]["late"]
    assert actor.vehicle["media"]["volume"] != 55
    assert actor.current.output == "新轮次正常回答。"
    assert not any(item.get("tool_call_id") == "old-volume-call" for item in old.messages)
    assert not any(e['type'] == 'vehicle.result' for e in queue._queue)
    receipt = await actor.simulator.execute(command["command_id"])
    await actor.post({"type": "vehicle.receipt", "command_id": command["command_id"], "receipt_id": receipt["receipt_id"]})
    assert await recorded(actor) == events


async def test_old_turn_command_timeout_receipt_is_stale(trace_context):
    make, _, _ = trace_context
    actor, queue = await make(ScriptedArk([
        observed(calls=[tool_call("set_volume", {"volume": 55}, "timed-out-call")]),
        observed("新轮次正常回答。"),
    ]))
    await actor.post({"type": "input", "text": "音量55"})
    command, _ = await receive(queue, "vehicle.command")
    old = actor.current
    await actor.post({"type": "input", "text": "新输入"})
    await receive(queue, "stream.done")
    await actor.post({"type": "_command_timeout", "command_id": command["command_id"]})
    events = await recorded(actor)
    receipt = next(event for event in events if event["kind"] == "vehicle.receipt")
    assert receipt["turn_id"] == old.id and receipt["call_id"] == "timed-out-call"
    assert receipt["status"] == "stale" and receipt["data"]["late"] is True
    assert receipt["data"]["timeout"] is True
    assert not any(e["type"] == "vehicle.result" for e in queue._queue)
    assert actor.vehicle["media"]["volume"] != 55


async def test_trace_api_paginates_more_than_200_without_loss_or_duplicates(trace_context):
    make, client, db = trace_context
    actor, _ = await make()
    expected = []
    for index in range(451):
        expected.append(db.append_trace(actor.id, "model.completed", {"index": index}, turn_id="ours")["trace_id"])
        if index % 17 == 0:
            db.append_trace("unrelated-session", "model.completed", {"private": True}, turn_id="ours")
    after, pages, actual = 0, [], []
    for _ in range(4):
        response = await client.get(f"/api/sessions/{actor.id}/traces", params={"after_id": after})
        assert response.status_code == 200
        page = response.json()
        pages.append(page)
        actual.extend(event["trace_id"] for event in page["events"])
        assert all(event["session_id"] == actor.id for event in page["events"])
        assert page["next_after_id"] == page["events"][-1]["trace_id"]
        after = page["next_after_id"]
        if not page["has_more"]:
            break
    assert [len(page["events"]) for page in pages] == [200, 200, 51]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert actual == expected and len(set(actual)) == 451
    empty = (await client.get(f"/api/sessions/{actor.id}/traces", params={"after_id": after})).json()
    assert empty["events"] == [] and empty["next_after_id"] == after and not empty["has_more"]


async def test_trace_api_composes_turn_task_and_session_filters(trace_context):
    make, client, db = trace_context
    actor, _ = await make()
    other, _ = await make()
    ids = {}
    for owner, turn, task in ((actor.id, "t1", "task1"), (actor.id, "t1", "task2"),
                              (actor.id, "t2", "task1"), (other.id, "t1", "task1")):
        ids[(owner, turn, task)] = db.append_trace(owner, "model.completed", {"source": owner},
                                                  turn_id=turn, task_id=task)["trace_id"]
    url = f"/api/sessions/{actor.id}/traces"
    for params, wanted in (
        ({"turn_id": "t1"}, [ids[(actor.id, "t1", "task1")], ids[(actor.id, "t1", "task2")]]),
        ({"task_id": "task1"}, [ids[(actor.id, "t1", "task1")], ids[(actor.id, "t2", "task1")]]),
        ({"turn_id": "t1", "task_id": "task1"}, [ids[(actor.id, "t1", "task1")]]),
        ({"task_id": "not-ours"}, []),
    ):
        response = await client.get(url, params=params)
        assert response.status_code == 200
        assert [event["trace_id"] for event in response.json()["events"]] == wanted
    filtered = (await client.get(url, params={"turn_id": "t1", "limit": 1})).json()
    assert filtered["has_more"] and len(filtered["events"]) == 1
    rest = (await client.get(url, params={"turn_id": "t1", "after_id": filtered["next_after_id"]})).json()
    assert [event["trace_id"] for event in rest["events"]] == [ids[(actor.id, "t1", "task2")]]
    assert (await client.get("/api/sessions/unknown-session/traces")).status_code == 404


async def test_manual_task_does_not_inherit_previous_turn_association(trace_context):
    make, _, db = trace_context
    actor, queue = await make(ScriptedArk([observed("上一轮回答结束。")]))
    await actor.post({"type": "input", "text": "上一轮闲聊"})
    previous, _ = await receive(queue, "stream.done")
    await actor.post({"type": "task.create", "goal": "手工登记的独立任务"})
    # Registration runs off the actor thread; wait for its receipt, not just
    # the event that launched the write.
    await receive(queue, "ui.card")
    events = await recorded(actor)
    task = next(task for task in db.list_tasks(actor.id) if task["goal"] == "手工登记的独立任务")
    assert task["context"]["origin_turn_id"] is None
    lifecycle = next(event for event in events if event["kind"] == "task.created")
    assert lifecycle["task_id"] == task["id"] and lifecycle["turn_id"] is None
    assert not any(event["task_id"] == task["id"] for event in db.traces_after(actor.id, turn_id=previous["turn_id"]))
    assert task["context"]["recent_history"]  # Context is retained without false ownership.


async def test_model_delegated_task_associates_with_its_own_turn(trace_context):
    make, _, db = trace_context
    actor, queue = await make(ScriptedArk([
        observed("上一轮回答结束。"),
        observed(calls=[tool_call("delegate_task", {"goal": "真实委托这一轮的任务"}, "delegate-call")]),
    ]))
    await actor.post({"type": "input", "text": "上一轮闲聊"})
    previous, _ = await receive(queue, "stream.done")
    await actor.post({"type": "input", "text": "请研究这一轮的问题"})
    current, _ = await receive(queue, "stream.done")
    events = await recorded(actor)
    task = db.list_tasks(actor.id)[0]
    assert task["context"]["origin_turn_id"] == current["turn_id"] != previous["turn_id"]
    delegated = next(event for event in events if event["kind"] == "tool.completed")
    assert delegated["task_id"] == task["id"] and delegated["turn_id"] == current["turn_id"]
    assert delegated["call_id"] == "delegate-call"
    assert delegated["data"]["model_call_id"] == next(event["call_id"] for event in events
        if event["kind"] == "model.started" and event["turn_id"] == current["turn_id"])


@pytest.mark.parametrize("params", [{"after_id": -1}, {"after_id": "bad"}, {"limit": 0}, {"limit": 201}])
async def test_trace_api_rejects_invalid_cursors_and_page_sizes(trace_context, params):
    make, client, _ = trace_context
    actor, _ = await make()
    assert (await client.get(f"/api/sessions/{actor.id}/traces", params=params)).status_code == 422


async def test_trace_recording_failure_is_visible_without_breaking_actor(trace_context, monkeypatch):
    make, client, db = trace_context
    monkeypatch.setenv("ARK_API_KEY", "trace-storage-secret")
    original = db.append_trace

    def fail(*args, **kwargs):
        raise RuntimeError("trace-storage-secret database unavailable")

    monkeypatch.setattr(db, "append_trace", fail)
    actor, queue = await make(ScriptedArk([observed("本轮回答正常。")]))
    await actor.post({"type": "input", "text": "本轮输入"})
    done, _ = await receive(queue, "stream.done")
    await actor.trace_queue.join()
    response = await client.get(f"/api/sessions/{actor.id}/traces")
    assert done["metrics"]["error"] == "diagnostics_unavailable"
    assert done["text"] != "本轮回答正常。"
    assert "database unavailable" in response.json()["recording_error"]
    assert "trace-storage-secret" not in response.text
    monkeypatch.setattr(db, "append_trace", original)


async def test_blocked_trace_writer_keeps_actor_responsive_but_gates_model_until_commit(trace_context, monkeypatch):
    make, _, db = trace_context
    entered, release = threading.Event(), threading.Event()
    original = db.append_trace

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("test writer was not released")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "append_trace", blocked)
    actor, queue = await make(ScriptedArk([observed("日志等待时仍能回答。")]))
    try:
        actor.trace("test.writer_blocked", {"injected": True})
        assert await asyncio.to_thread(entered.wait, 1)
        await actor.post({"type": "input", "text": "本轮输入"})
        await actor.queue.join()
        assert not actor.ark.requests and not actor.current.finished
        await actor.post({'type': 'persona.set', 'preset': 'professional'})
        await actor.queue.join()
        assert not release.is_set() and actor.trace_queue.qsize() > 0
        release.set()
        done, _ = await receive(queue, 'stream.done')
        assert done['text'] == '日志等待时仍能回答。'
    finally:
        release.set()
        await actor.trace_queue.join()
        monkeypatch.setattr(db, "append_trace", original)
    assert actor.trace_error is None
    assert any(event["kind"] == "turn.completed" for event in await recorded(actor))
