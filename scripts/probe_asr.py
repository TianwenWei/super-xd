"""One real Chinese ASR smoke request, using synthetic Xiaohe audio, not a mic.

Run from the project root with ``.venv/bin/python -m scripts.probe_asr``.
The local PCM fixture can be reused; credentials and request headers are never
written to the report. This does not measure physical microphone capture.
"""
import argparse
import asyncio
import audioop
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from backend import settings  # Loads server-only .env before creating adapters.
from backend.asr import VolcanoASR
from backend.tts import VolcanoTTS

TEXT = '你好，请把空调温度设为二十三度。'


async def fixture(path):
    if path.exists():
        return path.read_bytes(), 'reused_synthetic_xiaohe_fixture'
    tts = VolcanoTTS()
    async def texts():
        yield TEXT
    try:
        chunks = [item['audio'] async for item in tts.stream_synthesize(texts())
                  if item.get('type') == 'audio']
        if not chunks:
            raise RuntimeError('小何没有返回测试音频。')
        pcm, _ = audioop.ratecv(b''.join(chunks), 2, 1, 24000, 16000, None)
        pcm = bytes(9600) + pcm + bytes(9600)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pcm)
        return pcm, 'new_synthetic_xiaohe_fixture'
    finally:
        await tts.aclose()


async def run(args):
    asr = VolcanoASR()
    report = {'kind': 'real_volcengine_asr_smoke', 'physical_microphone_tested': False,
              'started_at': datetime.now(timezone.utc).isoformat(),
              'expected_spoken_text': TEXT, 'asr': asr.info(), 'ok': False}
    connection = None
    receiver = None
    started = time.perf_counter()
    try:
        pcm, source = await fixture(Path(args.fixture))
        report.update(audio_source=source, audio_duration_ms=len(pcm) / 32)
        connection = await asr.open(uuid4().hex)
        events = []
        async def read():
            async for item in connection.results():
                events.append(item)
        receiver = asyncio.create_task(read())
        for offset in range(0, len(pcm), 6400):
            chunk = pcm[offset:offset + 6400]
            await connection.send_audio(chunk)
            await asyncio.sleep(len(chunk) / 32000)
        stopped = time.perf_counter()
        await connection.finish()
        await receiver
        report.update(events=events, metrics=connection.metrics,
                      stop_to_final_ms=round((time.perf_counter() - stopped) * 1000, 3))
        final = next((item['text'] for item in reversed(events) if item['type'] == 'final'), '')
        normalized = final.replace('23', '二十三').replace('，', '').replace('。', '')
        report.update(final_text=final, ok=normalized == TEXT.replace('，', '').replace('。', ''))
    except Exception as exc:
        report['error'] = asr.safe_message(exc)
    finally:
        if receiver is not None and not receiver.done():
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if connection is not None:
            await connection.aclose()
        await asr.aclose()
    report['total_probe_ms'] = round((time.perf_counter() - started) * 1000, 3)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', default='data/asr-chinese-probe.pcm')
    parser.add_argument('--output', default='docs/asr-live.json')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
