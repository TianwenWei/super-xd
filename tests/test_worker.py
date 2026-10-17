"""Offline harness regression tests. Injected fake clients never power the app."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.db import Database
from backend.catalog import SKILLS
from backend.worker import ThinkerWorker
from backend.harness.task_runner import (THINKER_TOOLS, QUICK_TOOLS, SYSTEM_PROMPT,
                                         TOOL_SCHEMAS, _weather_finish_guard)
from backend.services.reads import verify_forecast_record
from backend.demo.resources import local_model_policy_id, local_task_budget
from backend.harness.release import get_release_id


def message(name, arguments, call_id="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class FakeArk:
    def info(self):
        return {'fixture_policy': 'offline-worker-v1'}

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.thinker_modes = []
        self.options = []

    async def complete(self, messages, tools=None, thinker=False, **options):
        self.calls.append(deepcopy(messages))
        self.thinker_modes.append(thinker)
        self.options.append({'tools': deepcopy(tools), **options})
        allowed = THINKER_TOOLS if thinker else QUICK_TOOLS
        assert all(tool['function']['name'] in {item['function']['name'] for item in allowed} for tool in tools)
        if not self.responses:
            raise AssertionError("Fake script exhausted")
        response = self.responses.pop(0)
        if callable(response):
            return await response(messages)
        return response


class FakeTavily:
    def __init__(self, results=None):
        self.results = results if results is not None else [{"url": "https://example.org/official", "title": "Official", "content": "A fact"}]
        self.calls = []

    async def search(self, query, max_results=5):
        self.calls.append(("search", query))
        return {"results": self.results, "queried_at": "2026-10-11T10:00:00+00:00"}

    async def extract(self, urls):
        self.calls.append(("extract", urls))
        return {"results": [{"url": url, "raw_content": "Actual page content"} for url in urls],
                "queried_at": "2026-10-11T10:00:01+00:00"}


@pytest.fixture
def db(tmp_path):
    return RegisteredDatabase(tmp_path / "worker.sqlite3")


def registration_context(context=None, *, model=None, worker_options=None):
    return {'release_id': get_release_id(),
            'model_policy_id': local_model_policy_id(model or FakeArk(), local_task_budget(**(worker_options or {}))),
            **(context or {})}


class RegisteredDatabase(Database):
    """Fixture registrations pass the same identity gate as the local service."""
    def create_task(self, session_id, goal, context=None, **options):
        return super().create_task(session_id, goal, registration_context(context), **options)


def task(db, goal="查找当前公开信息", context=None, *, model=None, worker_options=None):
    created = db.create_task("test-session", goal, context=registration_context(
        context, model=model, worker_options=worker_options))
    claimed = db.claim_next()
    assert claimed["id"] == created["id"]
    return claimed


@pytest.mark.parametrize('change', ['missing', 'old_release', 'old_model', 'budget'])
@pytest.mark.parametrize('checkpoint', [{}, {'checkpoint_version': 1, 'release_id': 'old-release', 'messages': []}])
async def test_worker_rejects_unregistered_or_changed_deployment_before_model(db, change, checkpoint):
    context = registration_context()
    if change == 'missing': context = {}
    elif change == 'old_release': context['release_id'] = 'old-release'
    elif change == 'old_model': context['model_policy_id'] = 'old-model'
    # Explicitly construct the historical registration, without fixture stamping.
    created = Database.create_task(db, 'test-session', '旧注册不能续跑', context=context)
    db.save_task(created['id'], 1, checkpoint=checkpoint)
    current = db.claim_next()
    model = FakeArk()
    worker = ThinkerWorker(db, model, FakeTavily(), **({'max_steps': 1} if change == 'budget' else {}))
    await worker.run_task(current)
    saved = db.get_task(created['id'])
    assert model.calls == [] and saved['status'] == 'failed'
    assert saved['error_code'] == 'task_deployment_mismatch'
    assert saved['context'] == context and saved['checkpoint'] == checkpoint
    row = next(item for item in db.traces_after('test-session') if item['kind'] == 'task.deployment.rejected')
    assert row['data']['model_called'] is False and row['data']['code'] == saved['error_code']


async def test_scheduler_rejects_old_registration_then_executes_current_task(db):
    old = Database.create_task(db, 'test-session', '旧排队任务', context={
        'release_id': 'old-release', 'model_policy_id': 'old-policy'})
    current = db.create_task('test-session', '当前登记任务')
    model = FakeArk(message('finish_task', {'summary': '当前任务完成。'}))
    worker = ThinkerWorker(db, model, FakeTavily(), concurrency=1)
    loop = asyncio.create_task(worker.run_forever(poll_interval=.001))
    try:
        await wait_until(lambda: db.get_task(current['id'])['status'] == 'completed')
    finally:
        worker.stop()
        await loop
    assert len(model.calls) == 1
    rejected = db.get_task(old['id'])
    assert rejected['status'] == 'failed' and rejected['error_code'] == 'task_deployment_mismatch'
    assert rejected['context'] == {'release_id': 'old-release', 'model_policy_id': 'old-policy'}


def test_vehicle_skill_guide_and_schema_are_generated_from_catalog():
    assert len(SKILLS) == 10
    for skill_id, skill in SKILLS.items():
        assert f"{skill_id}：{skill.name}" in SYSTEM_PROMPT
    assert TOOL_SCHEMAS["load_skill"]["properties"]["skill_id"]["enum"] == sorted(SKILLS)
    assert set(TOOL_SCHEMAS) == {"search_web", "extract_web", "load_skill", "ask_clarification",
                                 "suggest_vehicle_action", "finish_task", "lookup_web", "get_weather_forecast"}


@pytest.mark.asyncio
async def test_direct_skill_load_supplies_schema_for_vehicle_suggestion(db):
    async def propose(messages):
        loaded = json.loads(messages[-1]["content"])
        assert loaded["ok"] is True
        assert loaded["skill"]["id"] == "mirrors"
        names = {tool["name"] for tool in loaded["tools"]}
        assert "set_mirror_fold" in names
        return message("suggest_vehicle_action", {"name": "set_mirror_fold", "arguments": {"folded": True},
                                                  "reason": "已按后视镜分类查阅参数。"})

    ark = FakeArk(message("load_skill", {"skill_id": "mirrors"}), propose,
                  message("finish_task", {"summary": "建议折叠后视镜，尚未执行。"}))
    tavily = FakeTavily()
    current = task(db, "给出后视镜收纳建议")
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert tavily.calls == []
    assert saved["result"]["suggested_actions"] == [{"name": "set_mirror_fold", "arguments": {"folded": True},
                                                     "reason": "已按后视镜分类查阅参数。"}]


@pytest.mark.asyncio
async def test_unknown_skill_id_rejected_by_server_schema(db):
    ark = FakeArk(message("load_skill", {"skill_id": "missing_skill"}),
                  message("finish_task", {"summary": "分类无效，未提交车辆动作。"}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    saved = db.get_task(current["id"])
    results = [json.loads(item["content"]) for item in saved["transcript"] if item["role"] == "tool"]
    assert results[0]["ok"] is False
    assert "参数校验失败" in results[0]["error"]
    assert saved["result"]["suggested_actions"] == []


@pytest.mark.asyncio
async def test_research_uses_search_extract_and_persists_actual_sources(db):
    ark = FakeArk(message("search_web", {"query": "公开信息"}),
                  message("extract_web", {"urls": ["https://example.org/official"]}),
                  message("finish_task", {"summary": "工具读取到页面中的事实。"}))
    tavily = FakeTavily()
    current = task(db)
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert saved["steps"] == 3
    assert tavily.calls == [("search", "公开信息"), ("extract", ["https://example.org/official"])]
    source = saved["result"]["sources"][0]
    assert source == {"url": "https://example.org/official", "title": "Official",
                      "queried_at": "2026-10-11T10:00:01+00:00", "kind": "extract", "extracted": True}
    assert saved["result"]["web_evidence"] == "extracted"
    tools = [item for item in saved["transcript"] if item["role"] == "tool"]
    assert len(tools) == 3
    assert "Actual page content" in tools[1]["content"]
    assert [event["event_id"] for event in db.events_after(0)] == sorted(event["event_id"] for event in db.events_after(0))


@pytest.mark.asyncio
async def test_finish_supplies_short_spoken_result_without_an_extra_model_call(db):
    report = ("## Search 与 Extract\n"
              "Search 查找网页，Extract 读取选定页面。\n"
              "| 操作 | 用途 |\n| --- | --- |\n| Search | 查找来源 |\n| Extract | 读取正文 |\n"
              "来源：[官方说明](https://example.org/official)\n"
              "查询时间以实际工具结果为准。")
    spoken = "Search用来找网页，Extract用来读正文。先搜索，再读取需要核实的页面。"
    ark = FakeArk(message("search_web", {"query": "Search与Extract区别"}),
                  message("extract_web", {"urls": ["https://example.org/official"]}),
                  message("finish_task", {"summary": report, "spoken_summary": spoken}))
    current = task(db, "研究Search与Extract区别，给出完整报告")
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert len(ark.calls) == saved["steps"] == 3
    assert saved["result"]["summary"] == saved["summary"] == report
    assert saved["result"]["spoken_summary"] == spoken
    assert saved["result"]["query_times"] == ["2026-10-11T10:00:01+00:00"]
    assert saved["result"]["sources"][0]["url"] == "https://example.org/official"
    traces = db.traces_after("test-session", task_id=current["id"])
    model = [row for row in traces if row["kind"] == "model.completed"][-1]
    original = json.loads(model["data"]["message"]["tool_calls"][0]["function"]["arguments"])
    assert original == {"summary": report, "spoken_summary": spoken}
    completion = [row for row in traces if row["kind"] == "tool.completed"][-1]
    assert completion["data"]["result"]["result"]["summary"] == report
    assert completion["data"]["result"]["result"]["spoken_summary"] == spoken


@pytest.mark.asyncio
async def test_legacy_finish_preserves_the_complete_report_for_speech(db):
    report = "建议先确认营业时间。这样可以避免白跑。\n" + "补充说明供详细阅读。" * 30
    ark = FakeArk(message("finish_task", {"summary": report}))
    current = task(db, "分析出行准备")
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    result = db.get_task(current["id"])["result"]
    assert result["summary"] == report
    assert result["spoken_summary"] == "建议先确认营业时间。这样可以避免白跑。" + "补充说明供详细阅读。" * 30
    assert len(ark.calls) == 1


@pytest.mark.asyncio
async def test_spoken_result_cleans_display_syntax_and_keeps_full_original(db):
    raw_spoken = "**先确认营业时间。** [1]\n- 建议出发前再核对一次。"
    ark = FakeArk(message("finish_task", {"summary": "完整报告仍含**阅读格式**。",
                                          "spoken_summary": raw_spoken}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    result = db.get_task(current["id"])["result"]
    assert result["spoken_summary"] == "先确认营业时间。建议出发前再核对一次。"
    assert result["summary"] == "完整报告仍含**阅读格式**。"
    traces = db.traces_after("test-session", task_id=current["id"])
    started = next(row for row in traces if row["kind"] == "tool.started")
    assert json.loads(started["data"]["arguments"])["spoken_summary"] == raw_spoken


@pytest.mark.asyncio
async def test_spoken_result_retains_unverified_evidence_without_polish_call(db):
    class FailedExtract(FakeTavily):
        async def extract(self, urls):
            return {"results": [], "failed_results": [{"url": urls[0], "error": "unavailable"}],
                    "queried_at": "2026-10-11T10:00:01+00:00"}

    ark = FakeArk(message("search_web", {"query": "营业时间"}),
                  message("extract_web", {"urls": ["https://example.org/official"]}),
                  message("finish_task", {"summary": "搜索摘要列出了营业时间，但页面读取失败。",
                                          "spoken_summary": "搜索摘要列出了营业时间。"}))
    current = task(db)
    await ThinkerWorker(db, ark, FailedExtract()).run_task(current)
    result = db.get_task(current["id"])["result"]
    assert result["web_evidence"] == "search_only"
    assert result["spoken_summary"] == "搜索摘要列出了营业时间。部分内容仅有搜索摘要，尚未核实。"
    assert "页面未成功提取" in result["summary"]
    assert len(ark.calls) == 3


@pytest.mark.asyncio
async def test_spoken_result_keeps_unexecuted_vehicle_suggestion_status(db):
    ark = FakeArk(message("suggest_vehicle_action", {"name": "set_temperature", "arguments": {"value": 22},
                                                      "reason": "舒适温度"}),
                  message("finish_task", {"summary": "建议设置为22度，等待会话层校验执行。",
                                          "spoken_summary": "建议把空调设为22度。"}))
    current = task(db, "给出舒适温度建议")
    catalog = {"set_temperature": {"parameters": {"type": "object", "properties": {
        "value": {"type": "integer", "minimum": 16, "maximum": 30}}, "required": ["value"]}}}
    await ThinkerWorker(db, ark, FakeTavily(), catalog=catalog).run_task(current)
    result = db.get_task(current["id"])["result"]
    assert result["spoken_summary"] == "建议把空调设为22度。这些是动作建议，尚未执行。"
    assert len(result["suggested_actions"]) == 1
    assert len(ark.calls) == 2


@pytest.mark.asyncio
async def test_spoken_result_adds_missing_no_source_limit(db):
    ark = FakeArk(message("search_web", {"query": "营业时间"}),
                  message("finish_task", {"summary": "没有查到营业时间，需要进一步确认。",
                                          "spoken_summary": "建议出发前确认营业时间。"}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily(results=[])).run_task(current)
    result = db.get_task(current["id"])["result"]
    assert result["sources"] == []
    assert result["spoken_summary"] == "建议出发前确认营业时间。没有查到可核实来源，无法确认实时信息。"
    assert len(ark.calls) == 2


@pytest.mark.asyncio
async def test_spoken_legacy_preserves_all_late_conditions_without_keyword_selection(db):
    report = ("初步路线更省时。" + "沿途风景可供参考。" * 15 +
              "预约以入口当天核验通过为准。到达晚于十八点会失去入场资格。"
              "实时路况尚未核实，不能确认这次一定更快。")
    ark = FakeArk(message("finish_task", {"summary": report}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    result = db.get_task(current["id"])["result"]
    assert result["summary"] == report
    assert result["spoken_summary"] == report
    assert len(ark.calls) == 1


@pytest.mark.asyncio
async def test_revision_discards_late_search_and_rebuilds_with_new_condition(db):
    started, release = asyncio.Event(), asyncio.Event()

    class GatedTavily(FakeTavily):
        async def search(self, query, max_results=5):
            started.set()
            await release.wait()
            return await super().search(query, max_results)

    async def new_result(messages):
        assert "只考虑电动车" in messages[-1]["content"]
        assert "old-site" not in json.dumps(messages)
        return message("finish_task", {"summary": "已采用电动车条件，尚无外部事实。"})

    ark = FakeArk(message("search_web", {"query": "旧条件"}), new_result)
    tavily = GatedTavily([{"url": "https://old-site.example/page"}])
    current = task(db)
    running = asyncio.create_task(ThinkerWorker(db, ark, tavily).run_task(current))
    await started.wait()
    changed = db.mutate_task(current["id"], "update", "只考虑电动车", expected_revision=1)
    release.set()
    await running
    saved = db.get_task(current["id"])
    assert saved["revision"] == changed["revision"] == 2
    assert saved["status"] == "completed"
    assert saved["sources"] == []
    assert "电动车" in saved["summary"]
    assert db.inbox(current["id"]) == []
    traces = db.traces_after('test-session', task_id=current['id'])
    old_result = next(item for item in traces if item['kind'] == 'tool.completed' and item['revision'] == 1)
    old_model = next(item for item in traces if item['kind'] == 'model.started' and item['revision'] == 1)
    old_tool = next(item for item in traces if item['kind'] == 'tool.started' and item['revision'] == 1)
    assert old_result['data']['result']['results'][0]['url'] == 'https://old-site.example/page'
    assert old_result['call_id'] == 'call_1' and old_result['step'] == 1
    assert old_result['status'] == 'stale'
    assert old_result['data']['model_call_id'] == old_tool['data']['model_call_id'] == old_model['call_id']
    discarded = next(item for item in traces if item['kind'] == 'revision.discarded')
    assert discarded['status'] == 'stale' and discarded['data']['new_revision'] == 2
    assert discarded['data']['phase'] == 'tool'
    assert discarded['call_id'] == old_model['call_id']


@pytest.mark.asyncio
async def test_old_model_error_cannot_fail_new_revision(db):
    started, release = asyncio.Event(), asyncio.Event()

    async def old_failure(messages):
        started.set()
        await release.wait()
        raise RuntimeError("old-revision network failure")

    async def new_result(messages):
        assert "只考虑上海" in messages[-1]["content"]
        return message("finish_task", {"summary": "已按上海的新条件完成推理。"})

    current = task(db)
    worker = ThinkerWorker(db, FakeArk(old_failure, new_result), FakeTavily())
    pending = asyncio.create_task(worker.run_task(current))
    await started.wait()
    db.mutate_task(current["id"], "update", "只考虑上海", expected_revision=1)
    release.set()
    await pending
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert saved["revision"] == 2
    assert saved["error"] is None
    assert saved["steps"] == 2
    assert not any(event["kind"] == "failed" for event in db.events_after(0))
    traces = db.traces_after('test-session', task_id=current['id'])
    error = next(item for item in traces if item['kind'] == 'model.error')
    assert error['revision'] == 1 and error['step'] == 1
    assert 'old-revision network failure' in error['data']['error']
    stale = next(item for item in traces if item['kind'] == 'revision.discarded')
    assert stale['revision'] == 1 and stale['data']['new_revision'] == 2
    assert stale['data']['reason'] == 'error_from_old_revision'
    assert any(item['kind'] == 'task.completed' and item['revision'] == 2 for item in traces)


@pytest.mark.asyncio
async def test_current_model_error_fails_only_its_own_revision(db):
    async def failure(messages):
        raise RuntimeError("current revision model failure")

    current = task(db)
    await ThinkerWorker(db, FakeArk(failure), FakeTavily()).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "failed"
    assert saved["revision"] == 1
    assert "current revision model failure" in saved["error"]


@pytest.mark.asyncio
async def test_cancel_in_flight_model_at_boundary_preserves_no_stale_result(db):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(messages):
        started.set()
        await release.wait()
        return message("finish_task", {"summary": "迟到结果不能出现"})

    current = task(db)
    running = asyncio.create_task(ThinkerWorker(db, FakeArk(slow), FakeTavily()).run_task(current))
    await started.wait()
    db.mutate_task(current["id"], "cancel")
    release.set()
    await running
    saved = db.get_task(current["id"])
    assert saved["status"] == "cancelled"
    assert saved["result"] is None
    assert "迟到" not in saved["summary"]
    traces = db.traces_after('test-session', task_id=current['id'])
    late = next(item for item in traces if item['kind'] == 'model.completed')
    late_args = json.loads(late['data']['message']['tool_calls'][0]['function']['arguments'])
    assert late['revision'] == 1 and '迟到结果' in late_args['summary']
    assert late['status'] == 'stale'
    assert not any(item['kind'] == 'tool.started' for item in traces)
    assert any(item['kind'] == 'task.cancelled' and item['revision'] == 2 for item in traces)


async def wait_until(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_clarification_releases_slot_and_reply_resumes_same_task(db):
    first = db.create_task("test-session", "需要哪个城市？")
    second = db.create_task("test-session", "另一个任务")

    async def resumed(messages):
        assert "上海" in messages[-1]["content"]
        return message("finish_task", {"summary": "已按上海继续。"})

    ark = FakeArk(message("ask_clarification", {"question": "请提供城市。"}),
                  message("finish_task", {"summary": "第二个任务完成。"}), resumed)
    worker = ThinkerWorker(db, ark, FakeTavily(), concurrency=1)
    loop = asyncio.create_task(worker.run_forever(poll_interval=0.01))
    try:
        await wait_until(lambda: db.get_task(second["id"])["status"] == "completed")
        waiting = db.get_task(first["id"])
        assert waiting["status"] == "waiting_clarification"
        assert waiting["question"] == "请提供城市。"
        db.mutate_task(first["id"], "reply", "上海", expected_revision=1)
        await wait_until(lambda: db.get_task(first["id"])["status"] == "completed")
        finished = db.get_task(first["id"])
        assert finished["id"] == first["id"]
        assert finished["revision"] == 2
        assert finished["steps"] == 2
        traces = db.traces_after('test-session', task_id=first['id'])
        kinds = [item['kind'] for item in traces]
        assert 'task.waiting_clarification' in kinds and 'task.reply' in kinds and 'task.completed' in kinds
        assert {item['revision'] for item in traces if item['kind'] == 'model.started'} == {1, 2}
    finally:
        worker.stop()
        await loop


@pytest.mark.asyncio
async def test_queued_cancellation_does_not_spin_claim_loop(db):
    cancelled = db.create_task("test-session", "取消的任务")
    db.mutate_task(cancelled["id"], "cancel")
    next_task = db.create_task("test-session", "继续任务")
    worker = ThinkerWorker(db, FakeArk(message("finish_task", {"summary": "已完成"})), FakeTavily(), concurrency=2)
    loop = asyncio.create_task(worker.run_forever(poll_interval=0.01))
    try:
        await wait_until(lambda: db.get_task(next_task["id"])["status"] == "completed")
        assert db.get_task(cancelled["id"])["status"] == "cancelled"
    finally:
        worker.stop()
        await loop


@pytest.mark.asyncio
async def test_vehicle_suggestion_validates_schema_and_never_executes(db):
    catalog = {"set_temperature": SimpleNamespace(parameters={"type": "object", "properties": {
        "value": {"type": "number", "minimum": 16, "maximum": 30}}, "required": ["value"], "additionalProperties": False})}
    ark = FakeArk(message("suggest_vehicle_action", {"name": "set_temperature", "arguments": {"value": 99}, "reason": "无效建议"}),
                  message("suggest_vehicle_action", {"name": "set_temperature", "arguments": {"value": 22}, "reason": "舒适温度"}),
                  message("finish_task", {"summary": "建议设置温度，尚未执行。"}))
    current = task(db, "给出舒适方案")
    await ThinkerWorker(db, ark, FakeTavily(), catalog=catalog).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert saved["result"]["suggested_actions"] == [{"name": "set_temperature", "arguments": {"value": 22}, "reason": "舒适温度"}]
    results = [json.loads(item["content"]) for item in saved["transcript"] if item["role"] == "tool"]
    assert results[0]["ok"] is False
    assert results[1]["executed"] is False


@pytest.mark.asyncio
async def test_invalid_args_and_fabricated_sources_are_rejected(db):
    ark = FakeArk(message("search_web", {"query": "信息", "max_results": 99}),
                  message("extract_web", {"urls": ["https://never-searched.example/page"]}),
                  message("finish_task", {"summary": "伪造引用", "source_urls": ["https://invented.example"]}),
                  message("finish_task", {"summary": "没有来源，不能确认。"}))
    tavily = FakeTavily()
    current = task(db)
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert tavily.calls == []
    results = [json.loads(item["content"]) for item in saved["transcript"] if item["role"] == "tool"]
    assert [item["ok"] for item in results] == [False, False, False, True]
    assert saved["result"]["sources"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_no_result_or_credentials_error_is_not_claimed_as_research(db, fail, monkeypatch):
    class BrokenTavily(FakeTavily):
        async def search(self, query, max_results=5):
            if fail:
                raise RuntimeError("credential invalid: tvly-secretvalue012345")
            return {"results": [], "queried_at": "2026-10-11T10:00:00Z"}

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secretvalue012345")
    ark = FakeArk(message("search_web", {"query": "无结果查询"}), message("finish_task", {"summary": "查询没有取得信息。"}))
    current = task(db)
    await ThinkerWorker(db, ark, BrokenTavily()).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert saved["result"]["web_evidence"] == "none"
    assert "不能据此确认实时事实" in saved["summary"]
    assert "tvly-secretvalue012345" not in json.dumps(saved)
    assert saved["result"]["tool_observations"][0]["ok"] is (not fail)


@pytest.mark.asyncio
async def test_model_timeout_is_bounded_and_task_failed(db):
    async def slow(messages):
        await asyncio.sleep(1)
        return message("finish_task", {"summary": "不应到达"})

    current = task(db, worker_options={'timeout_seconds': 0.04})
    await ThinkerWorker(db, FakeArk(slow, slow), FakeTavily(), timeout_seconds=0.04).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "failed"
    assert "时间上限" in saved["error"]
    assert saved["result"] is None
    traces = db.traces_after('test-session', task_id=current['id'])
    assert any(item['kind'] == 'model.error' and item['status'] == 'cancelled' for item in traces)
    assert any(item['kind'] == 'task.timeout' for item in traces)


@pytest.mark.asyncio
async def test_tool_timeout_is_returned_to_model_without_fake_results(db):
    class SlowTavily(FakeTavily):
        async def search(self, query, max_results=5):
            await asyncio.sleep(1)
            return await super().search(query, max_results)

    ark = FakeArk(message("search_web", {"query": "慢查询"}),
                  message("finish_task", {"summary": "工具超时，无法确认信息。"}))
    current = task(db, worker_options={'tool_timeout_seconds': 0.02})
    await ThinkerWorker(db, ark, SlowTavily(), tool_timeout_seconds=0.02).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "completed"
    assert saved["result"]["sources"] == []
    assert "TimeoutError" in saved["result"]["tool_observations"][0]["error"]
    assert "不能据此确认实时事实" in saved["summary"]


@pytest.mark.asyncio
async def test_step_budget_and_plain_text_do_not_fake_success(db):
    current = task(db, worker_options={'max_steps': 2})
    ark = FakeArk({"role": "assistant", "content": "一个未经提交的结论"},
                  {"role": "assistant", "content": "另一个结论"})
    await ThinkerWorker(db, ark, FakeTavily(), max_steps=2).run_task(current)
    saved = db.get_task(current["id"])
    assert saved["status"] == "failed"
    assert saved["steps"] == 2
    assert "推理步骤上限" in saved["error"]
    assert saved["result"] is None


@pytest.mark.asyncio
async def test_search_requires_extract_before_finishing(db):
    ark = FakeArk(message("search_web", {"query": "事实"}),
                  message("finish_task", {"summary": "尚未读取页面不能成功。"}),
                  message("extract_web", {"urls": ["https://example.org/official"]}),
                  message("finish_task", {"summary": "已读取页面。"}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    saved = db.get_task(current["id"])
    results = [json.loads(item["content"]) for item in saved["transcript"] if item["role"] == "tool"]
    assert results[1]["ok"] is False
    assert "extract_web" in results[1]["error"]
    assert saved["status"] == "completed"


@pytest.mark.asyncio
async def test_fabricated_url_in_summary_cannot_bypass_source_ledger(db):
    ark = FakeArk(message("finish_task", {"summary": "结论见[来源](https://fabricated.example/page)", "source_urls": []}),
                  message("finish_task", {"summary": "没有工具来源，无法确认。"}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    saved = db.get_task(current["id"])
    results = [json.loads(item["content"]) for item in saved["transcript"] if item["role"] == "tool"]
    assert results[0]["ok"] is False
    assert saved["status"] == "completed"
    assert "fabricated" not in saved["summary"]


@pytest.mark.asyncio
async def test_explicit_parallelism_keeps_third_task_queued(db):
    entered = 0
    maximum = 0
    release = asyncio.Event()

    async def gated(messages):
        nonlocal entered, maximum
        entered += 1
        maximum = max(maximum, entered)
        await release.wait()
        entered -= 1
        return message("finish_task", {"summary": "完成"})

    jobs = [db.create_task("test-session", f"任务{i}") for i in range(3)]
    worker = ThinkerWorker(db, FakeArk(gated, gated, gated), FakeTavily(), concurrency=2)
    loop = asyncio.create_task(worker.run_forever(poll_interval=0.01))
    try:
        await wait_until(lambda: entered == 2)
        assert db.get_task(jobs[2]["id"])["status"] == "queued"
        release.set()
        await wait_until(lambda: all(db.get_task(job["id"])["status"] == "completed" for job in jobs))
        assert maximum == 2
        traces = db.traces_after('test-session')
        started = [item for item in traces if item['kind'] == 'model.started']
        completed = [item for item in traces if item['kind'] == 'model.completed']
        assert len(started) == len(completed) == len(jobs)
        assert len({item['call_id'] for item in started}) == len(jobs)
        assert {item['call_id'] for item in started} == {item['call_id'] for item in completed}
        goals = {job['id']: job['goal'] for job in jobs}
        for item in started:
            request = item['data']['request']
            assert json.loads(request['messages'][-1]['content'])['goal'] == goals[item['task_id']]
        for item in traces:
            if item['kind'] == 'tool.completed':
                model = next(entry for entry in started if entry['call_id'] == item['data']['model_call_id'])
                assert model['task_id'] == item['task_id']
    finally:
        worker.stop()
        await loop


@pytest.mark.asyncio
async def test_cancelling_active_task_does_not_block_free_slot(db):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(messages):
        started.set()
        await release.wait()
        return message("finish_task", {"summary": "此迟到结果会被取消"})

    first = db.create_task("test-session", "第一个任务")
    worker = ThinkerWorker(db, FakeArk(slow, message("finish_task", {"summary": "第二个任务已完成"})),
                           FakeTavily(), concurrency=2)
    loop = asyncio.create_task(worker.run_forever(poll_interval=0.01))
    try:
        await started.wait()
        db.mutate_task(first["id"], "cancel")
        second = db.create_task("test-session", "第二个任务")
        await wait_until(lambda: db.get_task(second["id"])["status"] == "completed")
        assert db.get_task(first["id"])["status"] == "cancelling"
        release.set()
        await wait_until(lambda: db.get_task(first["id"])["status"] == "cancelled")
    finally:
        release.set()
        worker.stop()
        await loop


@pytest.mark.asyncio
async def test_model_trace_keeps_full_returned_reasoning_metadata_but_not_in_next_prompt(db):
    long_content = '完整正文。' * 4000
    returned_reasoning = '仅记录供应商实际返回的推理内容。' * 1000
    first = {'role': 'assistant', 'content': long_content, 'reasoning_content': returned_reasoning,
             '_meta': {'provider_id': 'provider-message-1', 'model': 'real-model-id',
                       'finish_reason': 'stop', 'usage': {'total_tokens': 12345},
                       'raw_response': {'choices': [{'message': {'content': long_content,
                                          'reasoning_content': returned_reasoning}}]}}}
    ark = FakeArk(first, message('finish_task', {'summary': '按结构提交结论。'}))
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    traces = db.traces_after('test-session', task_id=current['id'])
    completed = [item for item in traces if item['kind'] == 'model.completed']
    assert len(completed) == 2
    event = completed[0]
    assert event['data']['message'] == {key: value for key, value in first.items() if key != '_meta'}
    assert event['data']['meta'] == first['_meta']
    assert event['step'] == event['data']['model_call_index'] == 1
    assert event['data']['agent'] == 'thinker'
    assert '_meta' not in json.dumps(ark.calls[1])
    assert 'raw_response' not in json.dumps(ark.calls[1])
    assert ark.calls[1][2]['content'] == long_content
    started = next(item for item in traces if item['kind'] == 'model.started')
    assert started['data']['request']['messages'] == ark.calls[0]
    assert started['data']['request']['tools'] == THINKER_TOOLS


@pytest.mark.asyncio
async def test_tool_trace_keeps_complete_long_web_result_and_call_association(db):
    long_page = '真实页面长内容\n' * 16000

    class LongTavily(FakeTavily):
        async def extract(self, urls):
            return {'results': [{'url': urls[0], 'raw_content': long_page}],
                    'queried_at': '2026-10-11T10:00:01Z', 'provider_extra': {'request_id': 'extract-provider-id'}}

    ark = FakeArk(message('search_web', {'query': '信息'}, 'search-call'),
                  message('extract_web', {'urls': ['https://example.org/official']}, 'extract-call'),
                  message('finish_task', {'summary': '有实际来源。'}, 'finish-call'))
    current = task(db)
    await ThinkerWorker(db, ark, LongTavily()).run_task(current)
    traces = db.traces_after('test-session', task_id=current['id'])
    tool_events = [item for item in traces if item['call_id'] == 'extract-call']
    assert [item['kind'] for item in tool_events] == ['tool.started', 'tool.completed']
    result = tool_events[1]
    assert result['step'] == result['data']['model_call_index'] == 2 and result['revision'] == 1
    assert result['data']['result']['results'][0]['raw_content'] == long_page
    assert result['data']['result']['provider_extra']['request_id'] == 'extract-provider-id'
    assert db.get_task(current['id'])['status'] == 'completed'


@pytest.mark.asyncio
async def test_stale_model_response_is_traceable_without_executing_its_tool(db):
    started, release = asyncio.Event(), asyncio.Event()

    async def old_response(messages):
        started.set()
        await release.wait()
        response = message('search_web', {'query': '不再适用的旧查询'}, 'old-call')
        response['_meta'] = {'provider_id': 'old-provider-id', 'usage': {'total_tokens': 77}}
        return response

    current = task(db)
    tavily = FakeTavily()
    worker = ThinkerWorker(db, FakeArk(old_response, message('finish_task', {'summary': '已采用新条件。'})), tavily)
    pending = asyncio.create_task(worker.run_task(current))
    await started.wait()
    db.mutate_task(current['id'], 'update', '新的地点条件')
    release.set()
    await pending
    traces = db.traces_after('test-session', task_id=current['id'])
    old = next(item for item in traces if item['kind'] == 'model.completed' and item['revision'] == 1)
    assert old['data']['meta']['provider_id'] == 'old-provider-id'
    assert old['data']['message']['tool_calls'][0]['id'] == 'old-call'
    assert not any(item['kind'] == 'tool.started' and item['call_id'] == 'old-call' for item in traces)
    assert tavily.calls == []
    assert '不再适用' not in json.dumps(db.get_task(current['id'])['transcript'], ensure_ascii=False)
    # Independent persistence survives the task's latest transcript replacement.
    assert Database(db.path).traces_after('test-session', task_id=current['id']) == traces


@pytest.mark.asyncio
async def test_tool_error_trace_preserves_full_diagnostic_and_redacts_secret(db, monkeypatch):
    diagnostic = 'network failure details:' + 'detail-' * 1500
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-trace-secret-0123456789')

    class BrokenTavily(FakeTavily):
        async def search(self, query, max_results=5):
            raise RuntimeError(diagnostic + ' credential=tvly-trace-secret-0123456789')

    current = task(db)
    ark = FakeArk(message('search_web', {'query': '测试错误'}, 'broken-call'),
                  message('finish_task', {'summary': '查询失败，无法确认。'}))
    await ThinkerWorker(db, ark, BrokenTavily()).run_task(current)
    traces = db.traces_after('test-session', task_id=current['id'])
    error = next(item for item in traces if item['kind'] == 'tool.error')
    started = next(item for item in traces if item['kind'] == 'tool.started' and item['call_id'] == 'broken-call')
    assert diagnostic in error['data']['error']
    assert error['call_id'] == 'broken-call' and error['revision'] == 1 and error['step'] == 1
    assert error['data']['model_call_id'] == started['data']['model_call_id']
    assert error['data']['model_call_id'] is not None
    assert error['data']['result']['ok'] is False
    assert 'tvly-trace-secret-0123456789' not in json.dumps(traces)


@pytest.mark.asyncio
async def test_tool_schema_rejection_is_traced_before_any_network_work(db):
    current = task(db)
    tavily = FakeTavily()
    ark = FakeArk(message('search_web', {'query': '信息', 'max_results': 99}, 'invalid-call'),
                  message('finish_task', {'summary': '无有效查询。'}))
    await ThinkerWorker(db, ark, tavily).run_task(current)
    traces = [item for item in db.traces_after('test-session', task_id=current['id']) if item['call_id'] == 'invalid-call']
    assert [item['kind'] for item in traces] == ['tool.started', 'tool.error']
    assert '参数校验失败' in traces[-1]['data']['error']
    assert tavily.calls == []


@pytest.mark.asyncio
async def test_worker_trace_links_delegating_turn_for_every_step(db):
    created = db.create_task('test-session', '来源回合', context={'origin_turn_id': 'source-turn'})
    current = db.claim_next()
    await ThinkerWorker(db, FakeArk(message('finish_task', {'summary': '完成。'})), FakeTavily()).run_task(current)
    traces = db.traces_after('test-session', task_id=created['id'])
    assert traces and all(item['turn_id'] == 'source-turn' for item in traces)
    assert any(item['kind'] == 'model.completed' for item in traces)


@pytest.mark.asyncio
async def test_model_failure_trace_keeps_provider_supplied_partial_response(db):
    async def failure(messages):
        exc = RuntimeError('provider transport ended early')
        exc.trace_partial = {'message': {'content': '已经实际收到的片段'},
                             'raw_chunks': [{'choices': [{'delta': {'content': '已经实际收到的片段'}}]}]}
        raise exc

    current = task(db)
    await ThinkerWorker(db, FakeArk(failure), FakeTavily()).run_task(current)
    traces = db.traces_after('test-session', task_id=current['id'])
    error = next(item for item in traces if item['kind'] == 'model.error')
    assert error['data']['partial']['message']['content'] == '已经实际收到的片段'
    assert error['data']['partial']['raw_chunks'][0]['choices'][0]['delta']['content'] == '已经实际收到的片段'
    assert db.get_task(current['id'])['status'] == 'failed'


@pytest.mark.asyncio
async def test_quick_lookup_finishes_in_two_light_calls_with_full_real_material_and_sources(db):
    full_page = '必要条件与实时数值。' * 2000

    class FullTavily(FakeTavily):
        async def extract(self, urls):
            data = await super().extract(urls)
            data['results'][0]['raw_content'] = full_page
            return data

    async def finish(messages):
        material = json.loads(messages[-1]['content'])
        assert material['extract']['results'][0]['raw_content'] == full_page
        assert len(material['search']['results']) == 3
        return message('finish_task', {'summary': '当前数值为20，条件以实际页面为准。',
                                      'spoken_summary': '当前大约20，页面说明的条件仍适用。'})

    ark = FakeArk(message('lookup_web', {'query': '明确地点日期与条件'}), finish)
    tavily = FullTavily([{'url': f'https://example.org/{i}', 'content': f'完整搜索材料{i}'} for i in range(3)])
    current = task(db, '一个简短实时问题', {'query_mode': 'quick'})
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 2
    assert ark.thinker_modes == [False, False]
    assert tavily.calls == [('search', '明确地点日期与条件'), ('extract', ['https://example.org/0', 'https://example.org/1'])]
    assert saved['result']['query_mode'] == 'quick'
    assert len(saved['sources']) == 3  # Complete search ledger is retained.
    assert saved['result']['web_evidence'] == 'search_only'  # Unread third source isn't silently verified.
    assert saved['result']['query_times'] == ['2026-10-11T10:00:00+00:00', '2026-10-11T10:00:01+00:00']
    traces = db.traces_after('test-session', task_id=current['id'])
    extracted = next(row for row in traces if row['kind'] == 'tool.completed' and row['data'].get('name') == 'extract_web')
    assert extracted['data']['result']['results'][0]['raw_content'] == full_page
    assert extracted['data']['parent_call_id'] == 'call_1'


@pytest.mark.asyncio
async def test_quick_can_check_one_missing_field_but_cannot_search_indefinitely(db):
    ark = FakeArk(message('lookup_web', {'query': '第一字段'}),
                  message('lookup_web', {'query': '缺失字段'}),
                  message('lookup_web', {'query': '无必要第三次'}),
                  message('finish_task', {'summary': '已核对可查字段，剩余限制未核实。'}))
    tavily = FakeTavily()
    current = task(db, context={'query_mode': 'quick'})
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 4
    assert [call[1] for call in tavily.calls if call[0] == 'search'] == ['第一字段', '缺失字段']
    results = [json.loads(row['content']) for row in saved['transcript'] if row['role'] == 'tool']
    assert results[2]['ok'] is False and 'lookup_web' in results[2]['error']
    assert ark.thinker_modes == [False] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['empty', 'credentials', 'extract', 'extract_timeout'])
async def test_quick_failed_verification_keeps_actual_limitations_without_fabricated_results(db, failure, monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-offline-secret-value')

    class FailingTavily(FakeTavily):
        async def search(self, query, max_results=5):
            if failure == 'credentials':
                raise RuntimeError('tvly-offline-secret-value not authorized')
            return await super().search(query, max_results)

        async def extract(self, urls):
            if failure == 'extract':
                raise RuntimeError('cannot read page')
            if failure == 'extract_timeout':
                await asyncio.sleep(1)
            return await super().extract(urls)

    ark = FakeArk(message('lookup_web', {'query': '核实当前字段'}),
                  message('finish_task', {'summary': '未取得足够材料，无法确认。', 'spoken_summary': '目前无法确认。'}))
    current = task(db, context={'query_mode': 'quick'}, worker_options={'tool_timeout_seconds': 0.02})
    tavily = FailingTavily([] if failure == 'empty' else None)
    await ThinkerWorker(db, ark, tavily, tool_timeout_seconds=.02).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed'
    assert saved['result']['web_evidence'] == ('none' if failure in {'empty', 'credentials'} else 'search_only')
    assert 'tvly-offline-secret-value' not in json.dumps(saved)
    assert any(row['tool'] == 'search_web' for row in saved['result']['tool_observations'])
    if failure.startswith('extract'):
        assert any(row['tool'] == 'extract_web' and row['ok'] is False for row in saved['result']['tool_observations'])
        assert '无法确认' in saved['result']['spoken_summary']


@pytest.mark.asyncio
async def test_quick_revision_between_search_and_extract_preserves_stale_trace_and_only_extracts_new_sources(db):
    started, release = asyncio.Event(), asyncio.Event()

    class GatedTavily(FakeTavily):
        async def search(self, query, max_results=5):
            self.calls.append(('search', query))
            if query == '旧条件':
                started.set()
                await release.wait()
            return {'results': [{'url': f'https://example.org/{query}', 'content': query}],
                    'queried_at': '2026-10-13T00:00:00+00:00'}

    async def new_query(messages):
        assert '新条件' in messages[1]['content']
        assert 'https://example.org/旧条件' not in json.dumps(messages, ensure_ascii=False)
        return message('lookup_web', {'query': '新条件'})

    ark = FakeArk(message('lookup_web', {'query': '旧条件'}), new_query,
                  message('finish_task', {'summary': '采用新条件实际页面。'}))
    current = task(db, context={'query_mode': 'quick'})
    tavily = GatedTavily()
    pending = asyncio.create_task(ThinkerWorker(db, ark, tavily).run_task(current))
    await started.wait()
    db.mutate_task(current['id'], 'update', '只使用新条件')
    release.set()
    await pending
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['revision'] == 2
    assert [call for call in tavily.calls if call[0] == 'extract'] == [('extract', ['https://example.org/新条件'])]
    assert [source['url'] for source in saved['sources']] == ['https://example.org/新条件']
    traces = db.traces_after('test-session', task_id=current['id'])
    old = next(row for row in traces if row['kind'] == 'tool.completed' and row['revision'] == 1 and row['data'].get('name') == 'search_web')
    assert old['status'] == 'stale' and old['data']['result']['results'][0]['content'] == '旧条件'


@pytest.mark.asyncio
async def test_quick_cancel_after_search_prevents_extract_and_completion(db):
    current = task(db, context={'query_mode': 'quick'})

    class CancelTavily(FakeTavily):
        async def search(self, query, max_results=5):
            result = await super().search(query, max_results)
            db.mutate_task(current['id'], 'cancel')
            return result

    tavily = CancelTavily()
    ark = FakeArk(message('lookup_web', {'query': '将取消'}))
    await ThinkerWorker(db, ark, tavily).run_task(current)
    assert db.get_task(current['id'])['status'] == 'cancelled'
    assert tavily.calls == [('search', '将取消')] and len(ark.calls) == 1


@pytest.mark.asyncio
async def test_quick_clarification_wait_does_not_spend_execution_budget_but_reply_does_not_reset_it(db):
    from datetime import datetime, timedelta, timezone

    async def clarify(messages):
        await asyncio.sleep(.03)
        return message('ask_clarification', {'question': '在哪个城市？'})

    async def slow_lookup(messages):
        assert '巴黎' in messages[-1]['content']
        await asyncio.sleep(2)
        return message('lookup_web', {'query': '巴黎'})

    current = task(db, context={'query_mode': 'quick'}, worker_options={'quick_timeout_seconds': 0.5})
    worker = ThinkerWorker(db, FakeArk(clarify, slow_lookup, slow_lookup), FakeTavily(), quick_timeout_seconds=.5)
    await worker.run_task(current)
    assert db.get_task(current['id'])['status'] == 'waiting_clarification'
    # Model a minute spent waiting for the user, still within global five minutes.
    created = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    with db.connection() as con:
        con.execute('UPDATE tasks SET created_at=? WHERE id=?', (created, current['id']))
    db.mutate_task(current['id'], 'reply', '巴黎')
    resumed = db.claim_next()
    await worker.run_task(resumed)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'failed' and saved['revision'] == 2
    assert '累计执行' in saved['error'] and saved['steps'] == 2
    traces = db.traces_after('test-session', task_id=current['id'])
    usages = [row['data']['elapsed_seconds'] for row in traces if row['kind'] == 'task.execution.finished']
    # The budget leaves room for SQLite overhead on slow disks; waiting time is never counted.
    assert len(usages) == 2 and sum(usages) < .9
    timeout = next(row for row in traces if row['kind'] == 'task.timeout')
    assert timeout['data']['reason'] == 'quick_execution_budget'


@pytest.mark.asyncio
async def test_quick_keeps_accumulated_running_deadline_and_unknown_mode_stays_full_research(db):
    from datetime import datetime, timedelta, timezone
    current = task(db, context={'query_mode': 'quick'})
    with db.connection() as con:
        con.execute('UPDATE tasks SET running_seconds=301 WHERE id=?', (current['id'],))
    ark = FakeArk()
    await ThinkerWorker(db, ark, FakeTavily()).run_task(db.get_task(current['id']))
    assert not ark.calls and db.get_task(current['id'])['status'] == 'failed'
    research = task(db, context={'query_mode': 'unrecognized'})
    pro = FakeArk(message('search_web', {'query': '复杂研究'}),
                  message('extract_web', {'urls': ['https://example.org/official']}),
                  message('finish_task', {'summary': '保留完整研究材料。'}))
    await ThinkerWorker(db, pro, FakeTavily()).run_task(research)
    assert pro.thinker_modes == [True] * 3
    assert db.get_task(research['id'])['result']['query_mode'] == 'research'


@pytest.mark.asyncio
async def test_waiting_quick_lookup_does_not_block_another_research_task(db):
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedTavily(FakeTavily):
        async def search(self, query, max_results=5):
            entered.set()
            await release.wait()
            return await super().search(query, max_results)

    class RoutingArk(FakeArk):
        async def complete(self, messages, tools=None, thinker=False, **options):
            if thinker:
                return message('finish_task', {'summary': '独立研究任务完成。'})
            if messages[-1]['role'] != 'tool':
                return message('lookup_web', {'query': '实时问题'})
            return message('finish_task', {'summary': '读取了实际来源。'})

    first = db.create_task('parallel', '简单问题', context={'query_mode': 'quick'})
    second = db.create_task('parallel', '复杂研究', context={'query_mode': 'research'})
    worker = ThinkerWorker(db, RoutingArk(), GatedTavily(), concurrency=2)
    loop = asyncio.create_task(worker.run_forever(poll_interval=.01))
    try:
        await entered.wait()
        await wait_until(lambda: db.get_task(second['id'])['status'] == 'completed')
        assert db.get_task(first['id'])['status'] == 'running'
        release.set()
        await wait_until(lambda: db.get_task(first['id'])['status'] == 'completed')
    finally:
        release.set()
        worker.stop()
        await loop


@pytest.mark.asyncio
async def test_quick_plain_text_cannot_bypass_finish_or_refresh_four_step_budget(db):
    current = task(db, context={'query_mode': 'quick'})
    ark = FakeArk(*[{'role': 'assistant', 'content': '未通过工具核实的结论。'} for _ in range(5)])
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'failed' and saved['result'] is None
    assert saved['steps'] == len(ark.calls) == 4
    assert '步骤上限' in saved['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('citation', [
    '来源官网（https://example.org/official）的说明。',
    '见【https://example.org/official】提供的实际材料。',
    '来源：https://example.org/official，必要条件仍适用。',
])
async def test_real_inline_citation_with_chinese_delimiters_is_not_falsely_rejected(db, citation):
    ark = FakeArk(message('lookup_web', {'query': '真实官网'}),
                  message('finish_task', {'summary': citation, 'source_urls': ['https://example.org/official']}))
    current = task(db, context={'query_mode': 'quick'})
    await ThinkerWorker(db, ark, FakeTavily()).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 2
    assert saved['result']['summary'] == citation
    assert not any(row['kind'] == 'tool.error' for row in db.traces_after('test-session', task_id=current['id']))


@pytest.mark.asyncio
async def test_quick_reserves_final_call_for_finish_after_two_lookups_and_repairs_real_validation_error(db):
    ark = FakeArk(message('lookup_web', {'query': '初次核实'}),
                  message('lookup_web', {'query': '必要补充'}),
                  message('finish_task', {'summary': '错误引用https://fabricated.example/'}),
                  message('finish_task', {'summary': '实际读取官网（https://example.org/official）的材料。',
                                          'source_urls': ['https://example.org/official']}))
    current = task(db, context={'query_mode': 'quick'})
    tavily = FakeTavily()
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 4
    assert len([call for call in tavily.calls if call[0] == 'search']) == 2
    assert [option['tool_choice'] for option in ark.options[:2]] == ['required', 'required']
    assert ark.options[2]['tool_choice'] == 'required'
    assert {tool['function']['name'] for tool in ark.options[2]['tools']} == {'get_weather_forecast', 'finish_task'}
    assert ark.options[3]['tool_choice'] == {'type': 'function', 'function': {'name': 'finish_task'}}
    assert [tool['function']['name'] for tool in ark.options[3]['tools']] == ['finish_task']
    traces = db.traces_after('test-session', task_id=current['id'])
    assert any(row['kind'] == 'tool.error' and '未经' in row['data'].get('error', '') for row in traces)


class FakeWeather:
    def __init__(self, response=None):
        self.calls = []
        self.closed = False
        self.response = response or {'ok': True, 'data_type': 'forecast',
            'queried_at': '2026-10-13T05:00:00+00:00', 'date': '2026-10-14',
            'location': {'name': 'Paris', 'country_code': 'FR'}, 'timezone': 'Europe/Paris',
            'temperature_min': 12.5, 'temperature_max': 18.1, 'unit': '°C',
            'source_url': 'https://api.open-meteo.com/v1/forecast?start_date=2026-10-14',
            'geocoding_source_url': 'https://geocoding-api.open-meteo.com/v1/search?name=Paris&countryCode=FR',
            'raw': {'geocoding': {'results': ['actual fake fixture']}, 'forecast': {
                'timezone': 'Europe/Paris', 'daily_units': {'temperature_2m_min': '°C', 'temperature_2m_max': '°C'},
                'daily': {'time': ['2026-10-14'], 'temperature_2m_min': [12.5], 'temperature_2m_max': [18.1]}}}}

    async def forecast(self, city, date, country_code=None):
        self.calls.append((city, date, country_code))
        if isinstance(self.response, Exception):
            raise self.response
        return deepcopy(self.response)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_structured_forecast_finishes_without_tavily_and_keeps_actual_date_timezone_units_and_raw(db):
    weather, tavily = FakeWeather(), FakeTavily()
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14', 'country_code': 'FR'}),
                  message('finish_task', {'summary': 'Open-Meteo预报巴黎2026-10-14为12.5到18.1摄氏度，巴黎时区。',
                                          'spoken_summary': '巴黎10月14日预计12.5到18.1摄氏度。'}))
    current = task(db, '简短核实巴黎明天温度', {'query_mode': 'quick'})
    worker = ThinkerWorker(db, ark, tavily, weather=weather)
    await worker.run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 2
    assert not tavily.calls and weather.calls == [('Paris', '2026-10-14', 'FR')]
    assert saved['result']['web_evidence'] == 'forecast'
    source = saved['result']['sources'][0]
    assert source['kind'] == 'forecast' and source['verified'] is True and source['extracted'] is False
    record = saved['result']['forecast_records'][0]
    assert record['date'] == '2026-10-14' and record['timezone'] == 'Europe/Paris' and record['unit'] == '°C'
    assert record['temperature_min'] == 12.5 and record['temperature_max'] == 18.1
    response = next(json.loads(row['content']) for row in saved['transcript'] if row['role'] == 'tool')
    assert response['raw'] == weather.response['raw']
    await worker.aclose()
    assert weather.closed


@pytest.mark.asyncio
async def test_ambiguous_forecast_pauses_for_clarification_without_invented_forecast_source(db):
    weather = FakeWeather({'ok': False, 'needs_clarification': True, 'candidates': [{'name': 'Paris', 'country_code': 'FR'}, {'name': 'Paris', 'country_code': 'US'}]})
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message('ask_clarification', {'question': '你指的是法国巴黎还是美国同名城市？'}))
    current = task(db, context={'query_mode': 'quick'})
    await ThinkerWorker(db, ark, FakeTavily(), weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'waiting_clarification' and saved['sources'] == []
    assert saved['result'] is None and '法国' in saved['question']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    {'date': '2026-10-13'}, {'data_type': 'historical'}, {'unit': '°F'},
    {'temperature_min': None}, {'temperature_max': -20}, {'source_url': ''},
])
async def test_forecast_type_date_units_and_source_must_match_real_structured_contract(db, invalid):
    weather = FakeWeather()
    weather.response.update(invalid)
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message('finish_task', {'summary': '未取得目标日的可靠预报，无法确认温度。'}))
    current = task(db, context={'query_mode': 'quick'})
    await ThinkerWorker(db, ark, FakeTavily(), weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['sources'] == []
    assert saved['result']['web_evidence'] == 'none' and saved['result']['forecast_records'] == []
    assert '不能据此确认实时事实' in saved['result']['summary']


@pytest.mark.asyncio
async def test_explicit_official_source_forecast_crosscheck_keeps_both_actual_providers(db):
    weather, tavily = FakeWeather(), FakeTavily()
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14', 'country_code': 'FR'}),
                  message('lookup_web', {'query': '实际指定官网目标日期'}),
                  message('finish_task', {'summary': '指定官网页面未证明目标日温度；Open-Meteo的2026-10-14预报为12.5到18.1摄氏度。',
                                          'spoken_summary': '官网的目标日温度未核实，Open-Meteo预报10月14日为12.5到18.1摄氏度。'}))
    current = task(db, '读取指定官网并交叉核实目标日温度', {'query_mode': 'quick'})
    await ThinkerWorker(db, ark, tavily, weather=weather).run_task(current)
    result = db.get_task(current['id'])['result']
    assert result['web_evidence'] == 'verified'
    assert {source['kind'] for source in result['sources']} == {'forecast', 'geocoding', 'extract'}
    assert '官网的目标日温度未核实' in result['spoken_summary']
    assert len(tavily.calls) == 2 and len(weather.calls) == 1


@pytest.mark.asyncio
async def test_quick_old_two_lookups_cannot_force_unsupported_completion_for_new_revision(db):
    current = task(db, context={'query_mode': 'quick'})

    class UpdateAfterSecondQuery(FakeTavily):
        async def search(self, query, max_results=5):
            result = await super().search(query, max_results)
            if query == '旧版本第二次':
                db.mutate_task(current['id'], 'update', '新条件必须重新核实')
            return result

    ark = FakeArk(message('lookup_web', {'query': '旧版本第一次'}),
                  message('lookup_web', {'query': '旧版本第二次'}),
                  message('finish_task', {'summary': '不允许无本版材料却确认新条件。'}))
    tavily = UpdateAfterSecondQuery()
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'failed' and saved['revision'] == 2
    assert saved['result']['partial'] is True and len(saved['sources']) == 1
    assert [call[1] for call in tavily.calls if call[0] == 'search'] == ['旧版本第一次', '旧版本第二次']
    traces = db.traces_after('test-session', task_id=current['id'])
    assert any(row['kind'] == 'tool.completed' and row['status'] == 'stale' and row['revision'] == 1 for row in traces)
    # Restored sources may be reused only when the model explicitly verifies
    # they support the new conditions. This response omitted that assertion.
    assert any(row['kind'] == 'tool.error' and row['revision'] == 2
               and row['data'].get('name') == 'finish_task' for row in traces)
    assert not any(row['kind'] == 'task.completed' for row in traces)


@pytest.mark.asyncio
async def test_quick_finish_needs_current_revision_attempt_even_with_old_lookup_budget_count(db):
    current = task(db, context={'query_mode': 'quick'})

    class OneUpdate(FakeTavily):
        async def search(self, query, max_results=5):
            result = await super().search(query, max_results)
            if query == '旧条件':
                db.mutate_task(current['id'], 'update', '新条件')
            return result

    ark = FakeArk(message('lookup_web', {'query': '旧条件'}),
                  message('finish_task', {'summary': '不应采用未核实的新条件结论。'}),
                  message('lookup_web', {'query': '新条件'}),
                  message('finish_task', {'summary': '已经按本版实际材料核实。'}))
    tavily = OneUpdate()
    await ThinkerWorker(db, ark, tavily).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['revision'] == 2 and saved['steps'] == 4
    assert saved['result']['summary'] == '已经按本版实际材料核实。'
    rejected = [json.loads(row['content']) for row in saved['transcript'] if row['role'] == 'tool'][0]
    assert rejected['ok'] is False and '当前版本' in rejected['error']
    assert len([call for call in tavily.calls if call[0] == 'search']) == 2
    assert len([call for call in tavily.calls if call[0] == 'extract']) == 1


@pytest.mark.asyncio
async def test_quick_invalid_tool_parameters_do_not_count_as_a_real_attempt(db):
    weather = FakeWeather()
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': 'invalid-date'}),
                  message('finish_task', {'summary': '参数校验失败不能成为已真实查询的依据。'}),
                  message('ask_clarification', {'question': '请确认目标日期。'}))
    current = task(db, context={'query_mode': 'quick'})
    await ThinkerWorker(db, ark, FakeTavily(), weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    assert not weather.calls and saved['status'] == 'waiting_clarification' and saved['result'] is None
    replies = [json.loads(row['content']) for row in saved['transcript'] if row['role'] == 'tool']
    assert replies[1]['ok'] is False and '当前版本' in replies[1]['error']


@pytest.mark.asyncio
async def test_actual_forecast_and_geocoding_urls_are_both_citable_without_extract(db):
    weather = FakeWeather()
    forecast = weather.response['source_url']
    geo = weather.response['geocoding_source_url']
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14', 'country_code': 'FR'}),
                  message('finish_task', {'summary': f'城市出处（{geo}），预报出处（{forecast}）。',
                                          'spoken_summary': '巴黎10月14日预报为12.5到18.1摄氏度。',
                                          'source_urls': [forecast, geo]}))
    current = task(db, context={'query_mode': 'quick'})
    tavily = FakeTavily()
    await ThinkerWorker(db, ark, tavily, weather=weather).run_task(current)
    result = db.get_task(current['id'])['result']
    assert result and result['web_evidence'] == 'forecast' and not tavily.calls
    assert {source['kind'] for source in result['sources']} == {'forecast', 'geocoding'}
    location_source = next(source for source in result['sources'] if source['kind'] == 'geocoding')
    assert location_source['supports'] == 'location' and location_source['extracted'] is False
    assert len(result['forecast_records']) == 1 and result['forecast_records'][0]['source_url'] == forecast


@pytest.mark.asyncio
async def test_geocoding_alone_cannot_support_forecast_and_unknown_urls_still_fail(db):
    weather = FakeWeather()
    forecast, geo = weather.response['source_url'], weather.response['geocoding_source_url']
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14', 'country_code': 'FR'}),
                  message('finish_task', {'summary': '地理编码不能当气温预报。', 'source_urls': [geo]}),
                  message('finish_task', {'summary': '不存在的来源不能引用。', 'source_urls': [forecast, 'https://unknown.example/']}),
                  message('finish_task', {'summary': '使用实际日期的结构化预报与真实城市出处。', 'source_urls': [forecast, geo]}))
    current = task(db, context={'query_mode': 'quick'})
    await ThinkerWorker(db, ark, FakeTavily(), weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 4
    replies = [json.loads(row['content']) for row in saved['transcript'] if row['role'] == 'tool']
    assert replies[1]['ok'] is False and '地理编码' in replies[1]['error']
    assert replies[2]['ok'] is False and '未经' in replies[2]['error']
    assert set(replies[1]['citable_source_urls']) == set(replies[2]['citable_source_urls']) == {forecast, geo}
    assert saved['result']['web_evidence'] == 'forecast'


async def weather_guard_evidence(db):
    sources, observations = {}, []
    worker = ThinkerWorker(db, FakeArk(), FakeTavily(), weather=FakeWeather())
    await worker._execute_tool('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'},
                               sources, [], observations)
    return sources, observations


@pytest.mark.asyncio
async def test_official_weather_requirement_scopes_real_search_and_extract_selection(db):
    official = 'https://meteofrance.com/previsions-meteo-france/paris/75000'
    unrelated = 'https://example.org/weather'
    class FilterTavily(FakeTavily):
        async def search(self, query, max_results=5, *, include_domains=None):
            self.calls.append(('search', query, include_domains))
            # Retain an out-of-scope provider row as diagnostic material;
            # the reader must still select only the required source domain.
            return {'results': [{'url': unrelated, 'content': 'Unrelated'},
                                {'url': official, 'content': 'Official page'}],
                    'queried_at': '2026-10-13T05:00:00+00:00',
                    'request_filters': {'include_domains': include_domains, 'include_domains_mode': 'restrict'}}
    tavily = FilterTavily()
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message('lookup_web', {'query': '巴黎明天天气'}),
                  message('finish_task', {'summary': '官网未提供可核实的目标日气温，无法确认温度。',
                                          'spoken_summary': '官网目标日气温尚未核实，无法确认温度。',
                                          'official_source_status': 'not_verified',
                                          'source_urls': [official]}))
    current = task(db, '查询法国巴黎明天天气，读取法国气象局官网',
                   context={'query_mode': 'quick', 'query_domain': 'weather', 'required_source_domains': ['meteofrance.com']})
    await ThinkerWorker(db, ark, tavily, weather=FakeWeather()).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed'
    assert tavily.calls == [('search', '巴黎明天天气', ['meteofrance.com']), ('extract', [official])]
    assert saved['result']['sources'][0]['url'] == official
    assert saved['result']['sources'][0]['extracted'] is True
    lookup = next(json.loads(row['content']) for row in saved['transcript']
                  if row['role'] == 'tool' and 'search' in json.loads(row['content']))
    assert lookup['search']['results'][0]['url'] == unrelated
    assert lookup['search']['request_filters']['include_domains'] == ['meteofrance.com']
    assert {tool['function']['name'] for tool in ark.options[0]['tools']} == {'get_weather_forecast', 'ask_clarification'}
    assert {tool['function']['name'] for tool in ark.options[1]['tools']} == {'lookup_web', 'ask_clarification'}
    assert [option['tool_choice'] for option in ark.options] == ['required'] * 3


@pytest.mark.parametrize('url,allowed', [
    ('https://meteofrance.com/paris', True), ('https://x.meteofrance.com/paris', True),
    ('https://meteofrance.com.example.org/paris', False), ('https://evil-meteofrance.com/paris', False),
])
def test_required_source_domain_uses_host_boundary(url, allowed):
    from backend.harness.task_runner import _source_in_domains
    assert _source_in_domains(url, ['meteofrance.com']) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('question', [
    '你指的是法国巴黎还是美国同名城市？',
    '请确认要查哪一天。',
    '请提供官网链接，并确认目标城市和日期。',
])
async def test_known_weather_source_does_not_block_real_or_mixed_condition_clarification(db, question):
    weather, tavily = FakeWeather(), FakeTavily()
    ark = FakeArk(message('ask_clarification', {'question': question}))
    current = task(db, '读取法国气象局官网核实天气',
                   {'query_mode': 'quick', 'query_domain': 'weather'})
    await ThinkerWorker(db, ark, tavily, weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'waiting_clarification' and saved['question'] == question
    assert saved['steps'] == 1 and len(ark.calls) == 1 and not weather.calls and not tavily.calls
    assert not any(row['kind'] == 'tool.error' for row in db.traces_after('test-session', task_id=current['id']))


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing_raw', 'missing_day', 'wrong_day', 'wrong_unit',
                                    'wrong_timezone', 'wrong_value', 'missing_source'])
async def test_forecast_rejects_missing_or_inconsistent_raw_without_creating_verified_evidence(db, invalid):
    weather = FakeWeather()
    raw = weather.response['raw']['forecast']
    if invalid == 'missing_raw':
        weather.response.pop('raw')
    elif invalid == 'missing_day':
        raw['daily'].pop('time')
    elif invalid == 'wrong_day':
        raw['daily']['time'] = ['2026-10-13']
    elif invalid == 'wrong_unit':
        raw['daily_units']['temperature_2m_max'] = '°F'
    elif invalid == 'wrong_timezone':
        raw['timezone'] = 'UTC'
    elif invalid == 'wrong_value':
        raw['daily']['temperature_2m_min'] = [17]
    else:
        weather.response.pop('source_url')
    sources, observations = {}, []
    worker = ThinkerWorker(db, FakeArk(), FakeTavily(), weather=weather)
    with pytest.raises(ValueError, match='raw|来源'):
        await worker._execute_tool('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'},
                                   sources, [], observations)
    assert sources == {} and observations == []


@pytest.mark.asyncio
async def test_quick_keeps_forecast_available_after_two_lookups_without_exceeding_budget(db):
    weather, tavily = FakeWeather(), FakeTavily()
    ark = FakeArk(message('lookup_web', {'query': '官网目标日'}),
                  message('lookup_web', {'query': '必要补充核实'}),
                  message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message('finish_task', {'summary': 'Open-Meteo预报12.5到18.1度。',
                                          'source_urls': [weather.response['source_url']]}))
    current = task(db, '巴黎天气', {'query_mode': 'quick'})
    await ThinkerWorker(db, ark, tavily, weather=weather).run_task(current)
    assert db.get_task(current['id'])['status'] == 'completed' and len(ark.calls) == 4
    assert len([call for call in tavily.calls if call[0] == 'search']) == 2
    assert {tool['function']['name'] for tool in ark.options[2]['tools']} == {'get_weather_forecast', 'finish_task'}
    assert ark.options[3]['tool_choice'] == {'type': 'function', 'function': {'name': 'finish_task'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('response,terminal', [
    ({'ok': False, 'needs_clarification': True, 'candidates': [{'name': 'Paris', 'country_code': 'FR'}, {'name': 'Paris', 'country_code': 'US'}]}, 'ask_clarification'),
    (RuntimeError('Weather service temporarily unavailable'), 'finish_task'),
])
async def test_declared_quick_weather_failed_or_ambiguous_forecast_does_not_loop_or_force_numbers(db, response, terminal):
    weather, tavily = FakeWeather(response), FakeTavily()
    arguments = {'question': '你指的是法国巴黎吗？'} if terminal == 'ask_clarification' else {
        'summary': '天气服务暂不可用，无法核实温度。'}
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message(terminal, arguments))
    current = task(db, '巴黎天气', {'query_mode': 'quick', 'query_domain': 'weather'})
    await ThinkerWorker(db, ark, tavily, weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == ('waiting_clarification' if terminal == 'ask_clarification' else 'completed')
    assert len(ark.calls) == 2 and len(weather.calls) == 1 and not tavily.calls
    assert {tool['function']['name'] for tool in ark.options[1]['tools']} == {'finish_task', 'ask_clarification'}
    assert ark.thinker_modes == [False, False]


@pytest.mark.asyncio
async def test_declared_quick_weather_missing_city_can_clarify_before_any_provider_call(db):
    weather, tavily = FakeWeather(), FakeTavily()
    ark = FakeArk(message('ask_clarification', {'question': '你要查哪个城市？'}))
    current = task(db, '查明天天气', {'query_mode': 'quick', 'query_domain': 'weather'})
    await ThinkerWorker(db, ark, tavily, weather=weather).run_task(current)
    assert db.get_task(current['id'])['status'] == 'waiting_clarification'
    assert len(ark.calls) == 1 and not weather.calls and not tavily.calls
    assert {tool['function']['name'] for tool in ark.options[0]['tools']} == {'get_weather_forecast', 'ask_clarification'}


@pytest.mark.asyncio
async def test_declared_weather_source_gate_stops_at_existing_lookup_and_model_budget(db):
    class EmptyFilteredTavily(FakeTavily):
        async def search(self, query, max_results=5, *, include_domains=None):
            self.calls.append(('search', query, include_domains))
            return {'results': [], 'queried_at': '2026-10-13T05:00:00+00:00'}
    weather, tavily = FakeWeather(), EmptyFilteredTavily()
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message('lookup_web', {'query': '巴黎官网预报'}),
                  message('lookup_web', {'query': '补充官网目标日'}),
                  message('finish_task', {'summary': '官网目标日温度未核实。', 'official_source_status': 'not_verified'}))
    current = task(db, '巴黎天气，读取法国气象局官网', {'query_mode': 'quick', 'query_domain': 'weather',
                                                     'required_source_domains': ['meteofrance.com']})
    worker = ThinkerWorker(db, ark, tavily, weather=weather)
    await worker.run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['steps'] == 4
    assert len(weather.calls) == 1 and len(tavily.calls) == 2 and len(ark.calls) == 4
    assert ark.thinker_modes == [False] * 4
    assert all({t['function']['name'] for t in option['tools']} == {'lookup_web', 'ask_clarification'}
               for option in ark.options[1:3])
    assert ark.options[3]['tool_choice'] == {'type': 'function', 'function': {'name': 'finish_task'}}
    assert worker.quick_max_steps == 4 and worker.quick_timeout_seconds == 60 and worker.timeout_seconds == 60


async def weather_guard_evidence(db):
    sources, observations = {}, []
    worker = ThinkerWorker(db, FakeArk(), FakeTavily(), weather=FakeWeather())
    await worker._execute_tool('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'},
                               sources, [], observations)
    return sources, observations


def claim(**overrides):
    value = {'date': '2026-10-14', 'source_url': FakeWeather().response['source_url'],
             'temperature_min': 12.5, 'temperature_max': 18.1}
    return {**value, **overrides}


@pytest.mark.asyncio
async def test_forecast_claims_must_equal_current_revision_observations(db):
    sources, observations = await weather_guard_evidence(db)
    current = {'goal': '巴黎明天气温', 'context': {'query_domain': 'weather'}}
    adopted = _weather_finish_guard({'summary': '预报不含降水信息。', 'forecast_claims': [claim()]},
                                    current, sources, observations, verifier=verify_forecast_record)
    assert adopted[0]['temperature_min'] == 12.5
    for bad in (claim(temperature_min=17), claim(date='2026-10-13'), claim(source_url='https://example.org/x')):
        with pytest.raises(ValueError, match='forecast_claims'):
            _weather_finish_guard({'summary': 'x', 'forecast_claims': [bad]}, current, sources, observations, verifier=verify_forecast_record)
    with pytest.raises(ValueError, match='forecast_claims'):
        _weather_finish_guard({'summary': 'x', 'forecast_claims': [claim()]}, current, sources, [], verifier=verify_forecast_record)
    # Without a declared weather domain no claim contract applies.
    assert _weather_finish_guard({'summary': 'x'}, {'goal': '车内温度', 'context': {}}, sources, observations, verifier=verify_forecast_record) == []


@pytest.mark.asyncio
async def test_official_status_is_structured_and_verified_needs_a_real_read(db):
    sources, observations = await weather_guard_evidence(db)
    current = {'goal': '读取法国气象局官网核实巴黎明天天气',
               'context': {'query_domain': 'weather', 'required_source_domains': ['meteofrance.com']}}
    with pytest.raises(ValueError, match='official_source_status'):
        _weather_finish_guard({'summary': 'x'}, current, sources, observations, verifier=verify_forecast_record)
    _weather_finish_guard({'summary': 'x', 'official_source_status': 'not_verified'}, current, sources, observations, verifier=verify_forecast_record)
    url = 'https://meteofrance.com/previsions-meteo-france/paris/75000'
    sources[url] = {'url': url, 'extracted': False}
    with pytest.raises(ValueError, match='verified'):
        _weather_finish_guard({'summary': 'x', 'official_source_status': 'verified'}, current, sources, observations, verifier=verify_forecast_record)
    sources[url]['extracted'] = True
    _weather_finish_guard({'summary': 'x', 'official_source_status': 'verified'}, current, sources, observations, verifier=verify_forecast_record)


@pytest.mark.asyncio
async def test_spoken_temperatures_come_from_verified_claims_not_model_prose(db):
    weather = FakeWeather()
    ark = FakeArk(message('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-14'}),
                  message('finish_task', {'summary': '巴黎17到22度。', 'forecast_claims': [claim(temperature_min=17)]}),
                  message('finish_task', {'summary': '预报不含降水信息。', 'spoken_summary': '降水情况预报里没有。',
                                          'forecast_claims': [claim()]}))
    current = task(db, '巴黎天气', {'query_mode': 'quick', 'query_domain': 'weather'})
    await ThinkerWorker(db, ark, FakeTavily(), weather=weather).run_task(current)
    saved = db.get_task(current['id'])
    replies = [json.loads(row['content']) for row in saved['transcript'] if row['role'] == 'tool']
    assert replies[1]['ok'] is False and replies[1]['forecast_records'][0]['temperature_min'] == 12.5
    spoken = saved['result']['spoken_summary']
    assert spoken.startswith('Paris2026-10-14最低十二点五度，最高十八点一度，数据来自Open-Meteo预报。')
    assert spoken.endswith('降水情况预报里没有。')
    assert saved['result']['summary'].startswith('Paris2026-10-14最低十二点五度')


def test_forecast_sentence_names_provider_and_unverified_official_source():
    from backend.harness.task_runner import _forecast_sentence
    record = {'location': {'name': '巴黎'}, 'date': '2026-10-14', 'temperature_min': -1.5, 'temperature_max': 3}
    assert _forecast_sentence([record], 'not_verified') == (
        '巴黎2026-10-14最低负一点五度，最高三度，数据来自Open-Meteo预报。指定的官方来源没能核实。')
    assert _forecast_sentence([], None) == ''
