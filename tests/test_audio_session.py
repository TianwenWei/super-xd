"""Local ordering checks for streaming audio; these bytes are explicit test doubles."""
import asyncio
import copy
import json

import pytest

from backend.db import Database
from backend.session import SessionActor


def tool(name, args, call_id='audio_call'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


class ControlledArk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.delta_sent = asyncio.Event()

    async def stream(self, messages, tools):
        response = self.responses.pop(0)
        for text in response.get('deltas', [response.get('content', '')]):
            if text:
                yield {'type': 'delta', 'text': text}
        self.delta_sent.set()
        if response.get('gate'):
            await response['gate'].wait()
        message = {k: v for k, v in response.items() if k not in {'gate', 'deltas'}}
        yield {'type': 'message', 'message': {'role': 'assistant', **message}}


class StreamingTTS:
    def __init__(self, *, finish_gate=None, packets=True, error=None):
        self.calls = []
        self.connected = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finish_gate = finish_gate
        self.packets = packets
        self.error = error

    async def stream_synthesize(self, texts):
        call = []
        self.calls.append(call)
        self.connected.set()
        try:
            async for text in texts:
                call.append(text)
                if self.error:
                    raise self.error
                # Metadata is diagnostic and must never count as first audio.
                yield {'type': 'metrics', 'provider_first_packet_ms': 1.5}
                if self.packets:
                    yield {'type': 'audio', 'audio': b'\x01\x00\x02\x00'}
            if self.finish_gate:
                await self.finish_gate.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.fixture
async def audio_actor(tmp_path):
    actors = []

    async def make(responses, tts=None):
        ark, tts = ControlledArk(responses), tts or StreamingTTS()
        actor = SessionActor(f'audio{len(actors)}', Database(tmp_path / f'{len(actors)}.sqlite3'), ark, tts)
        actors.append(actor)
        control = await actor.attach()
        await control.get()
        audio = await actor.attach_audio()
        await actor.post({'type': 'audio.configure', 'enabled': True})
        await actor.queue.join()
        return actor, control, audio, ark, tts

    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


async def until(queue, wanted, timeout=1):
    events = []
    async with asyncio.timeout(timeout):
        while True:
            event = await queue.get()
            events.append(event)
            if (wanted == 'pcm' and isinstance(event, bytes)) or (
                    isinstance(event, dict) and event.get('type') == wanted):
                return event, events


async def submit(actor, text='陪我聊一句', request='request-a'):
    await actor.post({'type': 'input', 'text': text, 'client_request_id': request})


async def test_explicit_full_report_read_cleans_markup_before_persona_and_keeps_report(audio_actor):
    actor, control, audio, ark, tts = await audio_actor([])
    report = '# 结论\n推荐室内场馆。\n| 城市 | 天气 |\n|---|---|\n| 巴黎 | 雨天 |'
    task = actor.db.create_task(actor.id, '详细朗读核查结果', context={'read_details': True})
    result = {'summary': report, 'spoken_summary': '推荐室内场馆。',
              'sources': [{'url': 'https://example.com/official'}]}
    actor.db.save_task(task['id'], 1, status='completed', result=result, summary='完成')
    actor.announcements[task['id']] = actor.db.get_task(task['id'])
    await actor.post({'type': '_announce'})
    await until(control, 'stream.done')
    await until(audio, 'audio.ended')
    spoken = ''.join(tts.calls[0])
    assert '推荐室内场馆' in spoken and '巴黎' in spoken and '雨天' in spoken
    assert not any(marker in spoken for marker in ('#', '|', '---'))
    assert actor.db.get_task(task['id'])['result'] == result
    assert ark.responses == []  # Report presentation makes no extra model call.


async def test_tts_connects_early_but_chat_text_waits_for_routing_completion(audio_actor):
    model_gate, finish_gate = asyncio.Event(), asyncio.Event()
    actor, control, audio, ark, tts = await audio_actor(
        [{'content': '很高兴陪你出发。', 'deltas': ['很高兴', '陪你出发。'], 'gate': model_gate}],
        StreamingTTS(finish_gate=finish_gate))
    await submit(actor)
    started, _ = await until(control, 'turn.started')
    audio_started, _ = await until(audio, 'audio.started')
    await asyncio.wait_for(tts.connected.wait(), 1)
    await asyncio.wait_for(ark.delta_sent.wait(), 1)
    await actor.queue.join()
    assert audio_started['client_request_id'] == started['client_request_id'] == 'request-a'
    assert audio_started['turn_id'] == started['turn_id']
    assert len(audio_started['playback_id']) == 32
    assert audio_started['sample_rate'] == 24000 and audio_started['format'] == 'pcm_s16le'
    assert tts.calls == [[]]
    assert actor.current.output == ''  # Routing payload stays private until its mode is known.
    model_gate.set()
    done, _ = await until(control, 'stream.done')
    packet, events = await until(audio, 'pcm')
    assert packet[:32].decode() == audio_started['playback_id']
    assert packet[32:] == b'\x01\x00\x02\x00'
    assert tts.calls == [['很高兴陪你出发。']]
    assert done['turn_id'] == started['turn_id']
    assert any(e.get('type') == 'audio.metrics' for e in events if isinstance(e, dict))
    assert not actor.current.audio_task.done()  # First PCM precedes provider completion.
    finish_gate.set()
    await until(audio, 'audio.ended')
    assert actor.speaking  # Input finished, but browser has not drained its Worklet.
    await actor.post({'type': 'playback', 'speaking': False,
                      'turn_id': started['turn_id'], 'playback_id': audio_started['playback_id']})
    await actor.queue.join()
    assert not actor.speaking


async def test_model_preface_is_spoken_after_local_checks_and_success_stays_silent(audio_actor):
    gate = asyncio.Event()
    actor, control, audio, ark, tts = await audio_actor([
        {'content': '这就把主驾调到二十三度。', 'gate': gate,
         'tool_calls': [tool('set_temperature', {'zone': 'driver', 'temperature': 23})]}])
    old = copy.deepcopy(actor.vehicle)
    await submit(actor, '把空调温度设为23度')
    await asyncio.wait_for(ark.delta_sent.wait(), 1)
    await actor.queue.join()
    assert tts.calls == [[]]  # Nothing is spoken before the whole message passed checks.
    gate.set()
    command, _ = await until(control, 'vehicle.command')
    await actor.queue.join()
    await until(audio, 'pcm')
    assert actor.vehicle == old and tts.calls[0] == ['这就把主驾调到二十三度。']
    await actor.post({'type': 'vehicle.execute', 'command_id': command['command_id']})
    receipt, _ = await until(control, 'vehicle.result')
    done, _ = await until(control, 'stream.done')
    assert receipt['result']['ok'] and tts.calls[0] == ['这就把主驾调到二十三度。']
    assert done['metrics']['route'] == 'tool' and done['metrics']['preface'] == '这就把主驾调到二十三度。'


async def test_without_a_preface_the_dispatch_template_still_speaks(audio_actor):
    actor, control, audio, _, tts = await audio_actor([
        {'tool_calls': [tool('set_temperature', {'zone': 'driver', 'temperature': 23})]}])
    await submit(actor, '把空调温度设为23度')
    await until(control, 'vehicle.command')
    await until(audio, 'pcm')
    assert '正在' in ''.join(tts.calls[0])


async def test_tool_timeout_after_preface_reports_failure_and_preserves_state(audio_actor, monkeypatch):
    monkeypatch.setattr('backend.session.TOOL_TIMEOUT', .02)
    actor, control, audio, _, tts = await audio_actor([
        {'content': '这就把音量调到三十五。', 'tool_calls': [tool('set_volume', {'volume': 35})]}])
    old = copy.deepcopy(actor.vehicle)
    await submit(actor, '音量设为35%')
    await until(control, 'vehicle.command')
    await until(audio, 'pcm')
    receipt, _ = await until(control, 'vehicle.result')
    done, _ = await until(control, 'stream.done')
    await until(audio, 'pcm')
    assert not receipt['result']['ok'] and actor.vehicle == old
    spoken = ''.join(tts.calls[0])
    assert spoken.startswith('这就把音量调到三十五。') and '状态尚未确认' in spoken
    assert '状态尚未确认' in done['text']


async def test_delegate_preface_replaces_the_template_ack_once_registered(audio_actor):
    goal = '查询巴黎天气，并比较室内与室外活动方案。'
    actor, control, audio, _, tts = await audio_actor([
        {'content': '我去查一下，有结果告诉你。', 'tool_calls': [tool('delegate_task', {'goal': goal})]}])
    await submit(actor, '查一下巴黎天气，顺便看看室内室外活动')
    done, _ = await until(control, 'stream.done')
    await until(audio, 'pcm')
    tasks = actor.db.list_tasks(actor.id)
    assert len(tasks) == 1 and tasks[0]['goal'] == goal and done['metrics']['route'] == 'delegate'
    assert tts.calls[0] == ['我去查一下，有结果告诉你。']


async def test_rejected_group_never_speaks_its_preface(audio_actor):
    actor, control, audio, ark, tts = await audio_actor([
        {'content': '这就导航过去。', 'tool_calls': [tool('set_navigation_destination', {'destination': '虹桥机场'})]},
        {'content': '你要去哪里？'}])
    await submit(actor, '导航去机场')
    done, _ = await until(control, 'stream.done')
    assert '这就导航过去' not in ''.join(sum(tts.calls, [])) and '这就导航过去' not in done['text']
    assert done['metrics']['preface'] is None


async def test_new_turn_cancels_old_stream_and_rejects_late_audio_and_old_playback(audio_actor):
    finish_gate = asyncio.Event()
    actor, control, audio, _, tts = await audio_actor(
        [{'content': '第一轮真实测试文本'}, {'content': '第二轮真实测试文本'}],
        StreamingTTS(finish_gate=finish_gate))
    await submit(actor)
    await until(control, 'stream.done')
    await until(audio, 'pcm')
    old = actor.current
    await submit(actor, '换个话题', 'request-b')
    await until(control, 'stream.done')
    await until(audio, 'pcm')
    current = actor.current
    await asyncio.wait_for(tts.cancelled.wait(), 1)
    assert old.audio_cancelled.is_set() and current is not old
    assert current.playback_id != old.playback_id and actor.speaking
    while not audio.empty(): audio.get_nowait()
    await actor.post({'type': '_audio_item', 'turn': old, 'item': {'audio': b'\x07\x00'}})
    await actor.post({'type': '_audio_finished', 'turn': old})
    await actor.post({'type': 'playback', 'speaking': False, 'turn_id': old.id, 'playback_id': old.playback_id})
    await actor.post({'type': 'audio.cancel', 'turn_id': current.id, 'playback_id': old.playback_id})
    await actor.queue.join()
    assert audio.empty() and actor.speaking and not current.audio_cancelled.is_set()


@pytest.mark.parametrize('reason', ['disable', 'audio_disconnect', 'control_disconnect', 'cancel'])
async def test_audio_stop_paths_close_provider_and_release_speaking(audio_actor, reason):
    actor, control, audio, _, tts = await audio_actor(
        [{'content': '取消测试文本。'}], StreamingTTS(finish_gate=asyncio.Event()))
    await submit(actor)
    await until(control, 'stream.done')
    await until(audio, 'pcm')
    turn = actor.current
    event = {'disable': {'type': 'audio.configure', 'enabled': False},
             'audio_disconnect': {'type': '_audio_detach', 'outgoing': audio},
             'control_disconnect': {'type': '_detach', 'outgoing': control},
             'cancel': {'type': 'audio.cancel', 'turn_id': turn.id, 'playback_id': turn.playback_id}}[reason]
    await actor.post(event)
    await actor.queue.join()
    await asyncio.wait_for(tts.cancelled.wait(), 1)
    assert turn.audio_cancelled.is_set() and not actor.speaking


async def test_zero_audio_does_not_claim_first_packet_or_keep_speaking(audio_actor):
    actor, control, audio, _, _ = await audio_actor([{'content': '无音频测试。'}], StreamingTTS(packets=False))
    await submit(actor)
    await until(control, 'stream.done')
    error, events = await until(audio, 'audio.error')
    assert not actor.speaking and not any(isinstance(e, bytes) for e in events)
    assert '未返回有效音频' in error['message'] and actor.current.audio_cancelled.is_set()
    assert 'server_first_audio_ms' not in actor.current.metrics
    assert 'server_first_nonzero_audio_ms' not in actor.current.metrics
    await actor.trace_queue.join()
    traces = actor.db.traces_after(actor.id, 0)
    assert next(e for e in traces if e['kind'] == 'audio.error')['status'] == 'failed'


async def test_provider_error_is_explicit_and_releases_speaking(audio_actor):
    actor, control, audio, _, _ = await audio_actor(
        [{'content': '错误测试。'}], StreamingTTS(error=RuntimeError('provider disconnected')))
    await submit(actor)
    await until(control, 'stream.done')
    error, _ = await until(audio, 'audio.error')
    assert error['turn_id'] == actor.current.id and error['playback_id'] == actor.current.playback_id
    assert not actor.speaking and actor.current.audio_chunks == 0
    assert actor.current.audio_cancelled.is_set()
    await actor.post({'type': 'playback', 'speaking': True,
                      'turn_id': actor.current.id, 'playback_id': actor.current.playback_id})
    await actor.queue.join()
    assert not actor.speaking


async def test_invalid_pcm_cancels_provider_and_late_frames_cannot_restart_audio(audio_actor):
    actor, control, audio, _, tts = await audio_actor(
        [{'content': '错误帧测试。'}], StreamingTTS(finish_gate=asyncio.Event(), packets=False))
    await submit(actor)
    await until(control, 'stream.done')
    turn = actor.current
    await actor.post({'type': '_audio_item', 'turn': turn, 'item': {'audio': b'\x01'}})
    error, _ = await until(audio, 'audio.error')
    await asyncio.wait_for(tts.cancelled.wait(), 1)
    assert '无效PCM' in error['message'] and turn.audio_cancelled.is_set() and not actor.speaking
    while not audio.empty(): audio.get_nowait()
    await actor.post({'type': '_audio_item', 'turn': turn, 'item': {'audio': b'\x01\x00'}})
    await actor.post({'type': '_audio_finished', 'turn': turn})
    await actor.post({'type': 'playback', 'speaking': True, 'turn_id': turn.id, 'playback_id': turn.playback_id})
    await actor.queue.join()
    assert audio.empty() and not actor.speaking and turn.audio_chunks == 0


async def test_audio_client_metrics_are_bound_to_exact_turn_and_playback(audio_actor):
    actor, control, audio, _, _ = await audio_actor([{'content': '指标测试。'}])
    await submit(actor)
    await until(control, 'stream.done')
    await until(audio, 'audio.ended')
    turn = actor.current
    await actor.post({'type': 'audio.client_metrics', 'turn_id': turn.id, 'playback_id': 'wrong',
                      'metrics': {'first_nonzero_pcm_ms': 12}})
    await actor.queue.join()
    assert 'audio_client' not in turn.metrics
    await actor.post({'type': 'audio.client_metrics', 'turn_id': turn.id, 'playback_id': turn.playback_id,
                      'metrics': {'first_nonzero_pcm_ms': 18, 'nested': {'ignored': True}}})
    await actor.queue.join()
    assert turn.metrics['audio_client'] == {'first_nonzero_pcm_ms': 18}
