"""Live Speech-only V3 streaming probe; never a query-to-browser benchmark."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from backend.tts import VolcanoTTS, XIAOHE_VOICE, TTS_RESOURCE_ID
from backend.speech_stream import STREAM_TTS_URL


async def sample(tts, index):
    received = asyncio.Event()
    input_done = False
    first_audio = None
    nonzero_audio = None
    total_bytes = 0
    chunks = 0
    metadata = []
    began = time.perf_counter()
    at = datetime.now(timezone.utc).isoformat()

    async def texts():
        nonlocal input_done
        yield '晚上好，今天想聊些什么？'
        # The second text is withheld until real PCM arrives, proving that
        # neither input EOF nor a whole reply is necessary for synthesis.
        await asyncio.wait_for(received.wait(), 10)
        yield '我们可以慢慢聊。'
        input_done = True

    try:
        async for item in tts.stream_synthesize(texts()):
            if item['type'] == 'audio':
                chunks += 1
                total_bytes += len(item['audio'])
                if first_audio is None:
                    first_audio = {'elapsed_ms': (time.perf_counter() - began) * 1000,
                                   'input_still_open': not input_done,
                                   **{key: value for key, value in item.items() if key != 'audio'}}
                    received.set()
                if nonzero_audio is None and any(item['audio']):
                    nonzero_audio = (time.perf_counter() - began) * 1000
            else:
                metadata.append(item)
        return {'index': index, 'started_at': at, 'ok': True, 'first_audio': first_audio,
                'first_nonzero_audio_ms': nonzero_audio, 'chunks': chunks, 'audio_bytes': total_bytes,
                'pcm_duration_seconds': total_bytes / 48000,
                'total_ms': (time.perf_counter() - began) * 1000, 'events': metadata}
    except Exception as exc:
        return {'index': index, 'started_at': at, 'ok': False, 'error': str(exc),
                'first_audio': first_audio, 'chunks': chunks, 'audio_bytes': total_bytes, 'events': metadata}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=2)
    parser.add_argument('--output', default='docs/tts-ws-live.json')
    args = parser.parse_args()
    load_dotenv('.env')
    tts = VolcanoTTS(timeout_seconds=15)
    report = {'provider': 'volcengine-speech-tts', 'endpoint': STREAM_TTS_URL,
              'speaker': XIAOHE_VOICE, 'resource': TTS_RESOURCE_ID, 'source': tts.info()['source'],
              'real_api': True, 'metric_scope': 'Speech adapter -> server PCM; excludes LLM/browser/playback',
              'audio_format': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1,
              'official_protocol': 'https://docs.volcengine.com/docs/DoubaoVoice/WebSocketBidirectionalStreaming-V3?lang=zh',
              'prewarm': None, 'samples': []}
    try:
        report['prewarm'] = await tts.prewarm_stream()
        for index in range(1, args.samples + 1):
            result = await sample(tts, index)
            report['samples'].append(result)
            print(json.dumps({key: result[key] for key in ('index', 'ok', 'first_audio', 'chunks', 'audio_bytes')},
                             ensure_ascii=False), flush=True)
    finally:
        await tts.aclose()
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(destination), 'prewarm': report['prewarm'],
                      'all_ok': all(item['ok'] for item in report['samples'])}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
