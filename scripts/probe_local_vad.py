"""Measure real Silero inference; --live also exercises cloud ASR and a TTS interruption.

Uses the existing synthetic Xiaohe fixture, never the physical microphone.
"""
if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from backend.vad import FRAME_BYTES, SileroVAD, SpeechGate, vad_info


def measure(pcm):
    started = time.perf_counter()
    vad = SileroVAD()
    load_ms = (time.perf_counter() - started) * 1000
    gate, timings, events = SpeechGate(), [], []
    padded = pcm + bytes((-len(pcm)) % FRAME_BYTES) + bytes(FRAME_BYTES * 32)
    for offset in range(0, len(padded), FRAME_BYTES):
        chunk = padded[offset:offset + FRAME_BYTES]
        started = time.perf_counter()
        probability = vad.score(chunk)
        timings.append((time.perf_counter() - started) * 1000)
        decision = gate.feed(chunk, probability)
        if decision['event']:
            events.append({'event': decision['event'], 'audio_ms': offset / 32,
                           'probability': probability})
    warm = sorted(timings[1:])
    return {'load_ms': round(load_ms, 3), 'frames': len(timings),
            'frame_ms': 32, 'p50_ms': round(warm[len(warm) // 2], 3),
            'p90_ms': round(warm[int(len(warm) * .9)], 3),
            'p99_ms': round(warm[int(len(warm) * .99)], 3),
            'events': events, 'ok': [e['event'] for e in events] == ['start', 'end']}


async def live(pcm, output):
    from contextlib import AsyncExitStack
    from backend.services.audio import build_asr, build_tts
    from backend.services.config import load_services_config
    from backend.services.factory import create_llm, resolved_profile

    config = load_services_config()
    if any(config.profile(domain)['provider'] != 'volcengine' for domain in ('asr', 'tts')):
        raise ValueError('This live VAD probe requires selected Volcengine ASR and TTS profiles')
    async with AsyncExitStack() as resources:
        ark = create_llm(config)
        resources.push_async_callback(ark.aclose)
        asr = build_asr(resolved_profile(config.profile('asr')))
        resources.push_async_callback(asr.aclose)
        tts = build_tts(config.profile('tts'))
        resources.push_async_callback(tts.aclose)
        if not asr.info().get('configured') or not tts.info().get('configured'):
            raise RuntimeError('真实验证需要已配置的火山 ASR 和 TTS。')
        return await probe_live(pcm, output, ark, asr, tts, config)


async def probe_live(pcm, output, ark, asr, tts, config):
    from backend.db import Database
    from backend.handsfree_endpoint import listen_socket
    from backend.session import SessionActor

    report = {'physical_microphone_tested': False, 'browser_aec_tested': False,
              'events': [], 'first_reply_pcm': False, 'reply_interrupted': False,
              'scope': 'configured LLM roles and selected Volcengine ASR/TTS; synthetic audio only',
              'services_config': config.source, 'llm': ark.info(), 'asr': asr.info(), 'tts': tts.info()}
    inbox, events, first_pcm = asyncio.Queue(), asyncio.Queue(), asyncio.Event()

    class ProbeSocket:
        headers = {'origin': 'http://localhost:5173'}

        async def accept(self):
            pass

        async def close(self, **kwargs):
            pass

        async def receive(self):
            return await inbox.get()

        async def send_json(self, event):
            report['events'].append({**event, 'observed_perf_ms': time.perf_counter() * 1000})
            if event['type'] == 'voice.final' and not first_pcm.is_set():
                if not event['text'].strip():
                    raise RuntimeError('真实 ASR 返回空文本。')
                await actor.post({'type': 'input', 'text': event['text'], 'client_request_id': 'vad-live-probe'})
            await events.put(event)

    async def until(kind):
        async with asyncio.timeout(45):
            while True:
                event = await events.get()
                if event['type'] == 'voice.error':
                    raise RuntimeError(event['message'])
                if event['type'] == kind:
                    return event

    async def feed():
        padded = pcm + bytes((-len(pcm)) % FRAME_BYTES) + bytes(FRAME_BYTES * 32)
        deadline = time.perf_counter()
        for offset in range(0, len(padded), FRAME_BYTES):
            await inbox.put({'type': 'websocket.receive', 'bytes': padded[offset:offset + FRAME_BYTES]})
            deadline += .032
            await asyncio.sleep(max(0, deadline - time.perf_counter()))

    with tempfile.TemporaryDirectory() as directory:
        actor = SessionActor('vad-live-probe', Database(Path(directory) / 'probe.sqlite3'), ark, tts)
        await actor.attach()
        audio = await actor.attach_audio()
        await actor.post({'type': 'audio.configure', 'enabled': True})

        async def consume_audio():
            while True:
                packet = await audio.get()
                if isinstance(packet, bytes) and len(packet) > 32 and any(packet[32:]):
                    report['first_reply_pcm'] = True
                    first_pcm.set()

        consumer = asyncio.create_task(consume_audio())
        listener = asyncio.create_task(listen_socket(ProbeSocket(), actor, asr))
        try:
            await inbox.put({'type': 'websocket.receive', 'text': json.dumps({
                'type': 'voice.start', 'listen_id': 'vad-live-listener', 'sample_rate': 16000,
                'format': 'pcm_s16le', 'channels': 1})})
            await until('voice.ready')
            await feed()
            first_final = await until('voice.final')
            await asyncio.wait_for(first_pcm.wait(), 45)
            first_turn = actor.current
            feeder = asyncio.create_task(feed())
            second_start = await until('voice.speech_start')
            await actor.queue.join()
            report['reply_interrupted'] = first_turn.audio_cancelled.is_set()
            report['capture_changed'] = second_start['capture_id'] != first_final['capture_id']
            await feeder
            second_final = await until('voice.final')
            report['recognized_texts'] = [first_final['text'], second_final['text']]
            report['ok'] = all(report.get(key) for key in ('first_reply_pcm', 'reply_interrupted', 'capture_changed'))
            await actor.trace_queue.join()
            traces = await asyncio.to_thread(actor.db.traces_after, actor.id, limit=200)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.with_suffix('.trace.json').write_text(json.dumps(traces, ensure_ascii=False, indent=2) + '\n')
        finally:
            await inbox.put({'type': 'websocket.disconnect'})
            await asyncio.wait_for(listener, 10)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await actor.close()
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', default='data/asr-chinese-probe.pcm')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--out', default='data/local-vad-probe.json')
    args = parser.parse_args()
    pcm, output = Path(args.fixture).read_bytes(), Path(args.out)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'vad': vad_info(),
              'audio_source': 'synthetic_xiaohe_fixture', 'physical_microphone_tested': False,
              'inference': await asyncio.to_thread(measure, pcm)}
    if args.live:
        report['live'] = await live(pcm, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['inference']['ok'] or (args.live and not report['live']['ok']):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
