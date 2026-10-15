"""Real Ark/Speech acceptance with authoritative, delayed simulated receipts."""
import asyncio
import json
from pathlib import Path
import tempfile
import time

from backend.db import Database
from backend.models import ArkClient, utcnow
from backend.session import SessionActor
from backend.tts import VolcanoTTS


async def run_case(root, name, text, *, driving_race=False):
    ark, tts = ArkClient(), VolcanoTTS()
    actor = SessionActor(name, Database(root / f'{name}.sqlite'), ark, tts)
    control = await actor.attach(); await control.get()
    audio = await actor.attach_audio()
    events, audio_events, jobs = [], [], []
    started = time.perf_counter()
    async def execute(command):
        if driving_race:
            await actor.post({'type':'vehicle.configure','settings':{'speed_kph':100,'gear':'D'}})
            await actor.queue.join()
        receipt = await actor.simulator.execute(command['command_id'])
        await actor.post({'type':'vehicle.receipt','command_id':command['command_id'],'receipt_id':receipt['receipt_id']})
    async def consume_audio():
        first = None
        while True:
            item = await audio.get()
            if isinstance(item, bytes):
                if first is None and any(item[32:]): first=(time.perf_counter()-started)*1000
            else:
                audio_events.append(item)
                if item['type'] in {'audio.ended','audio.error'}:
                    return first
    try:
        await actor.post({'type':'audio.configure','enabled':True})
        await actor.post({'type':'input','text':text})
        async with asyncio.timeout(120):
            audio_job=asyncio.create_task(consume_audio()); jobs.append(audio_job)
            while True:
                event=await control.get(); events.append(event)
                if event['type']=='vehicle.command': jobs.append(asyncio.create_task(execute(event)))
                if event['type']=='stream.done': break
            first_pcm=await audio_job
        await actor.trace_queue.join()
        traces=actor.db.traces_after(actor.id,limit=1000)
        speeches=[r['data'] for r in traces if r['kind']=='audio.text']
        receipts=[e for e in events if e['type']=='vehicle.result']
        result={'case':name,'input':text,'done':event,'first_nonzero_pcm_ms':first_pcm,
                'receipts':receipts,'speech':speeches,'audio_events':audio_events,'traces':traces}
        if name=='chat':
            result['passed']=actor.current.chat_streaming and actor.current.model_calls==2 and first_pcm is not None
        else:
            spoken=[r for r in traces if r['kind']=='vehicle.dispatch_feedback']
            received=[r for r in traces if r['kind']=='vehicle.receipt']
            result['passed']=bool(spoken and received and first_pcm is not None and spoken[0]['trace_id']<received[0]['trace_id'])
            result['passed'] &= all(e['result']['ok'] is (not driving_race) for e in receipts)
        return result
    finally:
        for job in jobs: job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        await actor.close(); await ark.client.aclose(); await tts.aclose()


async def main():
    report={'observed_at':utcnow(),'real_model':True,'real_tts':True,'simulated_vehicle':True,'browser_render_tested':False,'cases':[]}
    with tempfile.TemporaryDirectory() as directory:
        for name,text,race in [('window','打开主驾车窗',False),
                               ('failure','打开主驾车窗',True),
                               ('parallel','打开后视镜加热，并打开全部车内照明',False),
                               ('chat','用三句话介绍一下秋天适合散步的原因。',False)]:
            record=await run_case(Path(directory),name,text,driving_race=race)
            report['cases'].append(record)
            Path('docs/engineering-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps({'case':name,'passed':record['passed'],'first_nonzero_pcm_ms':record['first_nonzero_pcm_ms']},ensure_ascii=False),flush=True)
    report['passed']=all(case['passed'] for case in report['cases'])
    Path('docs/engineering-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': asyncio.run(main())
