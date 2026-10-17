import asyncio
from datetime import datetime, timedelta, timezone
import json

from test_worker import RegisteredDatabase as Database
from backend.worker import ThinkerWorker
from test_worker import FakeArk, FakeTavily, message


def test_waiting_is_not_execution_budget(tmp_path):
    db = Database(tmp_path / 'budget.sqlite')
    task = db.create_task('s', '查资料')
    task = db.claim_next()
    db.save_task(task['id'], 1, status='waiting_clarification', question='哪个城市？')
    with db.connection() as con:
        con.execute('UPDATE tasks SET created_at=?', ((datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),))
    assert db.expire_tasks(300) == 0
    resumed = db.mutate_task(task['id'], 'reply', '深圳')
    assert resumed['status'] == 'queued'


async def test_resume_keeps_messages_and_sources(tmp_path):
    db = Database(tmp_path / 'resume.sqlite')
    task = db.create_task('s', '查找展馆信息')
    ark = FakeArk(message('search_web', {'query':'展馆信息'}),
                  message('extract_web', {'urls':['https://example.org/official']}),
                  message('ask_clarification', {'question':'需要哪个日期？'}),
                  message('finish_task', {'summary':'根据已检索的来源整理。', 'source_urls':['https://example.org/official']}))
    worker = ThinkerWorker(db, ark, FakeTavily())
    await worker.run_task(db.claim_next())
    assert db.get_task(task['id'])['status'] == 'waiting_clarification'
    db.mutate_task(task['id'], 'reply', '明天')
    await worker.run_task(db.claim_next())
    restored = ark.calls[-1]
    assert any(m.get('role')=='tool' and 'https://example.org/official' in m.get('content','') for m in restored)
    assert db.get_task(task['id'])['status'] == 'completed'

import pytest
from copy import deepcopy
from backend.catalog import TOOLS
from backend.demo.vehicle_simulator import initial_vehicle_state
from backend.session import SessionActor, Turn
from backend.demo.device import VehicleSimulator
from backend.harness.vehicle_planner import validate_vehicle_plan
from test_session import FakeArk as TalkerArk, call, wait_for
from test_audio_session import ControlledArk, StreamingTTS, until
from test_vehicle_planner import plan, intent, action
from vehicle_fakes import FakeVehiclePlanner, plan_response


def test_clarification_has_separate_expiry_and_running_accumulates(tmp_path):
    db = Database(tmp_path / 'clock.sqlite')
    task = db.create_task('s', '查资料'); db.claim_next()
    old = (datetime.now(timezone.utc) - timedelta(seconds=110)).isoformat()
    with db.connection() as con:
        con.execute('UPDATE tasks SET running_since=?', (old,))
    db.save_task(task['id'], 1, status='waiting_clarification', question='日期？')
    waiting = db.get_task(task['id'])
    assert 110 <= waiting['running_seconds'] < 112
    assert waiting['running_since'] is None and waiting['clarification_expires_at']
    db.mutate_task(task['id'], 'reply', '明天'); db.claim_next()
    assert db.running_elapsed(db.get_task(task['id'])) >= 110
    with db.connection() as con:
        con.execute('UPDATE tasks SET running_seconds=301')
    assert db.expire_tasks() == 1
    assert db.get_task(task['id'])['status'] == 'failed'
    another = db.create_task('s','等待'); db.claim_next()
    db.save_task(another['id'],1,status='waiting_clarification',question='哪里？')
    with db.connection() as con:
        con.execute('UPDATE tasks SET clarification_expires_at=? WHERE id=?', (old,another['id']))
    assert db.expire_tasks() == 1
    assert '澄清等待' in db.get_task(another['id'])['error']


def test_polite_words_are_warning_not_retry():
    query='麻烦帮我把后视镜加热打开'
    source='把后视镜加热打开'
    value=plan([intent('i',query,source,actions=['a'])], [action('a','i','set_mirror_heating',{'enabled':True})])
    result=validate_vehicle_plan(value,query,initial_vehicle_state(),allowed_tools=['set_mirror_heating'])
    assert result['ok'] and result['validation']['warnings'][0]['code']=='unaccounted_request_text'


async def test_plan_actions_advance_serially_after_actual_receipts(tmp_path):
    ark=TalkerArk([{'tool_calls':[call('delegate_vehicle_control',{'skill_ids':['mirrors','lights']},'plan')]}])
    planner=FakeVehiclePlanner(plan_response(('set_mirror_heating',{'enabled':True}),('set_interior_light',{'zone':'all','enabled':True})))
    actor=SessionActor('parallel',Database(tmp_path/'parallel.sqlite'),ark,vehicle_planner=planner)
    try:
        q=await actor.attach(); await q.get()
        await actor.post({'type':'input','text':'打开后视镜加热并打开车内灯'})
        first,_=await wait_for(q,'vehicle.command')
        await actor.queue.join()
        assert actor.completed_commands == {}
        assert not any(event['type'] == 'vehicle.command' for event in q._queue)
        receipt = await actor.simulator.execute(first['command_id'])
        await actor.post({'type': 'vehicle.receipt', 'command_id': first['command_id'], 'receipt_id': receipt['receipt_id']})
        second,_=await wait_for(q,'vehicle.command')
        assert first['command_id'] != second['command_id']
        assert actor.completed_commands[first['command_id']]['executed'] is True
        group = actor.current.checkpoint.orchestration.groups[-1]
        assert len(group.external_pending) == 1 and len(group.plan_receipts) == 1
        receipt = await actor.simulator.execute(second['command_id'])
        await actor.post({'type': 'vehicle.receipt', 'command_id': second['command_id'], 'receipt_id': receipt['receipt_id']})
        await wait_for(q, 'stream.done')
        assert actor.current.tool_results['plan']['ok'] is True
    finally: await actor.close()


@pytest.mark.parametrize('name', ['set_mirror_fold','set_window_position','set_sunroof_position',
    'set_seat_position','recall_seat_memory','set_steering_position','set_window_anti_pinch',
    'set_door_lock','set_child_lock','set_tailgate_open','set_tailgate_height'])
async def test_driving_restrictions_from_device(name):
    # Uses catalog examples, with unsafe driving targets explicitly configured.
    state=initial_vehicle_state(); state['_meta'].update(speed_kph=100,gear='D')
    args=deepcopy(TOOLS[name].example)
    if name=='set_window_position': args.update(position=100)
    if name=='set_sunroof_position': args.update(position=100)
    if name=='set_door_lock': args.update(locked=False)
    if name=='set_tailgate_open': args.update(open=True)
    sim=VehicleSimulator(state,execution_ms=0,network_rtt_ms=0)
    sim.register({'command_id':'cmd','turn_id':'t','name':name,'arguments':args})
    result=await sim.execute('cmd')
    assert not result['ok'], (name,args,result)
    assert result['reason_code'] in ('vehicle_moving','speed_limit')
    assert result['resume_condition']
    assert sim.state == state
    assert await sim.execute('cmd') == result
    await sim.close()


async def test_simulator_parallel_latency_and_receipt_binding():
    sim=VehicleSimulator(initial_vehicle_state())
    for i,name in enumerate(['set_mirror_heating','set_interior_light']):
        sim.register({'command_id':str(i),'turn_id':'t','name':name,'arguments':deepcopy(TOOLS[name].example)})
    before=asyncio.get_running_loop().time()
    results=await asyncio.gather(sim.execute('0'),sim.execute('1'))
    assert asyncio.get_running_loop().time()-before < 1.2
    assert all(650 <= r['timings']['total_ms'] < 1200 for r in results)
    with pytest.raises(ValueError): sim.receipt('0','invented')
    assert sim.receipt('0',results[0]['receipt_id'])['ok']
    await sim.close()


async def test_timezone_comes_from_vehicle_settings(tmp_path):
    actor=SessionActor('tz',Database(tmp_path/'tz.sqlite'),TalkerArk([]))
    try:
        await actor.post({'type':'vehicle.configure','settings':{'timezone':'Asia/Shanghai'}})
        await actor.queue.join()
        turn = Turn('fixture', '读取时区', actor.persona, actor.persona_version)
        assert actor.nlp_start_request(turn).context.environment.timezone == 'Asia/Shanghai'
        before=deepcopy(actor.vehicle)
        await actor.post({'type':'vehicle.configure','settings':{'timezone':'Invalid/Zone'}})
        await actor.queue.join()
        assert actor.vehicle==before
    finally: await actor.close()


async def test_confirmed_chat_streams_sentence_before_model_end(tmp_path):
    gate=asyncio.Event()
    ark=ControlledArk([{'content':'第一句。第二句。','deltas':['第一句。','第二句。'],'gate':gate}])
    tts=StreamingTTS()
    actor=SessionActor('chat',Database(tmp_path/'chat.sqlite'),ark,tts)
    try:
        q=await actor.attach(); await q.get(); audio=await actor.attach_audio()
        await actor.post({'type':'audio.configure','enabled':True})
        await actor.post({'type':'input','text':'聊聊今天'})
        await until(audio,'pcm')
        assert not actor.current.finished and not gate.is_set()
        assert tts.calls[0][0]=='第一句。'
        gate.set(); await wait_for(q,'stream.done')
        assert ''.join(tts.calls[0])=='第一句。第二句。'
    finally: gate.set(); await actor.close()


async def test_played_history_preserves_generation_and_marks_only_completed_heard_sentences(tmp_path):
    ark=TalkerArk([{'content':'第一句。第二句。'},{'content':'收到。'}])
    actor=SessionActor('heard',Database(tmp_path/'heard.sqlite'),ark)
    try:
        q=await actor.attach(); await q.get()
        await actor.post({'type':'input','text':'说两句'})
        await wait_for(q,'stream.done')
        old=actor.current
        # Explicit provider/worklet fixtures; never inferred from elapsed wall time.
        old.playback_id='a'*32; old.audio_bytes=96000
        old.sentences=[{'text':'第一句。','start_ms':0,'end_ms':500}, {'text':'第二句。','start_ms':500,'end_ms':2000}]
        await actor.post({'type':'audio.played','turn_id':old.id,'playback_id':old.playback_id,'played_ms':750})
        await actor.post({'type':'audio.cancel','turn_id':old.id,'playback_id':old.playback_id})
        await actor.post({'type':'input','text':'停一下'})
        await wait_for(q,'stream.done')
        heard=[m['content'] for m in ark.requests[-1]['messages'] if m['role']=='assistant' and m.get('content')]
        assert any('第一句。' in text for text in heard)
        assert any('第一句。第二句。' in text for text in heard)
        playback = next(message['content'] for message in ark.requests[-1]['messages'] if 'playback_history' in message.get('content', ''))
        assert '"heard_text":"第一句。"' in playback.replace(' ', '')
        assert '"status":"partial"' in playback.replace(' ', '')
        assert old.messages[-1]['content']=='第一句。第二句。'
    finally: await actor.close()


async def test_tool_only_speech_follows_verified_receipt_without_demo_preface(tmp_path):
    ark=ControlledArk([{'tool_calls':[call('set_window_position',{'window':'driver','position':100},'window')]}])
    tts=StreamingTTS(); actor=SessionActor('dispatch',Database(tmp_path/'dispatch.sqlite'),ark,tts)
    try:
        q=await actor.attach(); await q.get(); audio=await actor.attach_audio()
        await actor.post({'type':'audio.configure','enabled':True})
        await actor.post({'type':'input','text':'打开主驾车窗'})
        command,_=await wait_for(q,'vehicle.command')
        await actor.queue.join()
        assert not any(tts.calls) and not actor.completed_commands
        receipt=await actor.simulator.execute(command['command_id'])
        await actor.post({'type':'vehicle.receipt','command_id':command['command_id'],'receipt_id':receipt['receipt_id']})
        await wait_for(q,'stream.done'); await actor.queue.join()
        await until(audio,'audio.ended')
        assert len(tts.calls[0])==1 and actor.vehicle['windows']['positions']['driver']==100
        assert ''.join(tts.calls[0]) == actor.current.checkpoint.presentation.speech == actor.current.output
        await actor.trace_queue.join()
        traces=actor.db.traces_after(actor.id)
        kinds=[r['kind'] for r in traces]
        assert kinds.index('vehicle.dispatched') < kinds.index('vehicle.receipt') < kinds.index('audio.text')
    finally: await actor.close()


async def test_failed_device_receipt_speaks_followup_and_preserves_state(tmp_path):
    ark=ControlledArk([{'tool_calls':[call('set_window_position',{'window':'driver','position':100},'window')]},
                       {'content': '车辆正在行驶，车窗没有打开；请先降低车速。'}])
    tts=StreamingTTS(); actor=SessionActor('failure',Database(tmp_path/'failure.sqlite'),ark,tts)
    actor.simulator.execution_ms=0; actor.simulator.network_rtt_ms=0
    try:
        q=await actor.attach(); await q.get(); audio=await actor.attach_audio()
        await actor.post({'type':'audio.configure','enabled':True})
        await actor.post({'type':'input','text':'打开主驾车窗'})
        command,_=await wait_for(q,'vehicle.command')
        await actor.queue.join()
        assert not any(tts.calls)
        # Driving begins after dispatch, so only the device can reject this race.
        await actor.post({'type':'vehicle.configure','settings':{'speed_kph':100,'gear':'D'}})
        await actor.queue.join()
        receipt=await actor.simulator.execute(command['command_id'])
        assert receipt['reason_code']=='speed_limit' and receipt['resume_condition']
        await actor.post({'type':'vehicle.receipt','command_id':command['command_id'],'receipt_id':receipt['receipt_id']})
        await wait_for(q,'stream.done'); await until(audio,'audio.ended')
        assert '车窗没有打开' in ''.join(tts.calls[0]) and actor.vehicle['windows']['positions']['driver']==0
        assert actor.current.tool_results['window']['executed'] is False
        assert actor.current.model_calls == 2
    finally: await actor.close()


async def test_expired_command_cannot_execute_when_browser_reconnects():
    sim=VehicleSimulator(initial_vehicle_state(),execution_ms=0,network_rtt_ms=0)
    sim.register({'command_id':'expired','turn_id':'t','name':'set_volume','arguments':{'volume':90}})
    sim.expire('expired')
    receipt=await sim.execute('expired')
    assert receipt['reason_code']=='stale_command' and not receipt['ok']
    assert sim.state['media']['volume']!=90
    await sim.close()


async def test_quick_resume_can_reuse_verified_sources_without_searching_again(tmp_path):
    db=Database(tmp_path/'reuse.sqlite')
    task=db.create_task('s','核实展馆信息',context={'query_mode':'quick'})
    tavily=FakeTavily()
    ark=FakeArk(message('lookup_web',{'query':'展馆信息'}),
                message('ask_clarification',{'question':'按什么格式整理？'}),
                message('finish_task',{'summary':'来源已核实，按列表整理。','reuse_prior_evidence':True}))
    worker=ThinkerWorker(db,ark,tavily)
    await worker.run_task(db.claim_next())
    db.mutate_task(task['id'],'reply','列表')
    calls=deepcopy(tavily.calls)
    await worker.run_task(db.claim_next())
    assert db.get_task(task['id'])['status']=='completed' and tavily.calls==calls
    assert any(m.get('role')=='tool' for m in ark.calls[-1])


async def test_tts_waits_for_text_before_starting_provider_session():
    from test_speech_stream import FakeWS, adapter
    ws=FakeWS(); tts,_=adapter(ws); gate=asyncio.Event()
    async def texts():
        await gate.wait()
        yield '规划完成后才有的播报。'
    stream=tts.stream_synthesize(texts())
    try:
        assert (await anext(stream))['event']=='connection.started'
        pending=asyncio.create_task(anext(stream))
        await asyncio.sleep(.01)
        assert not any(frame['event']==100 for frame in ws.sent)
        gate.set(); assert (await pending)['event']=='session.started'
        assert any([item.get('audio') async for item in stream])
    finally:
        gate.set(); await stream.aclose(); await tts.aclose()
