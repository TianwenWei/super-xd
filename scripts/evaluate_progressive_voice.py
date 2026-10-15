"""One real synthetic-ASR -> Mini/Pro -> receipt -> Xiaohe PCM smoke.

No physical microphone/browser/acoustic measurement. Default makes no requests.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from scripts.evaluate_voice_interactions import (
    Harness, fixture, normalize_transcript, semantic_errors, transport_errors, utcnow,
)

SCENARIO = {'id':'cold_mirror_voice','text':'打开后视镜加热。',
            'commands':['set_mirror_heating'],'allowed_changes':['mirrors.heating'],
            'semantic':'mirror','wait_drain':True}


async def run(args):
    if not args.run:
        print(json.dumps({'requests_sent':0,'scenario':SCENARIO},ensure_ascii=False)); return 0
    output = Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    report = {'started_at':utcnow(),'base':args.base,'expected':SCENARIO,
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'scope':'Real paced synthetic Xiaohe input -> Volc ASR final -> Mini/Pro -> actual mock commit -> Python nonzero PCM receipt. Drain is simulated.',
              'physical_microphone_tested':False,'browser_audio_tested':False,
              'attempt':{'asr':{},'status':'pending'},'traces':[],'errors':[]}
    with output.open('x') as handle: json.dump(report,handle,ensure_ascii=False,indent=2)
    harness = Harness(args.base)
    attempt = report['attempt']
    try:
        response = await harness.http.get('/api/config');response.raise_for_status()
        report['configured_models'] = response.json()['model']
        await harness.connect();report['session_id'] = harness.sid
        pcm,attempt['fixture'] = await fixture(SCENARIO['text'],Path(args.fixture_dir))
        attempt['before'] = (await harness.snapshot())['vehicle']
        text = await harness.recognize(pcm,attempt['asr'])
        attempt['asr_text_matches_fixture'] = normalize_transcript(text)==normalize_transcript(SCENARIO['text'])
        await harness.query(text,attempt,SCENARIO)
        snapshot = await harness.snapshot();attempt['after'] = snapshot['vehicle']
        report['final_snapshot'] = snapshot;attempt['status'] = 'observed'
        # Allow the async archive to catch up; no query retry or evaluator model.
        await asyncio.sleep(.25)
        report['traces'] = await harness.traces()
        issues = semantic_errors(SCENARIO,attempt,report['traces'])
        issues.extend(transport_errors(SCENARIO,attempt,harness.consumer.records[attempt['playback_id']]))
        if not attempt['asr_text_matches_fixture']:issues.append('asr_transcript_changed_fixture_content')
        if attempt['commands'] and attempt['commands'][0]['arguments']!={'enabled':True}:issues.append('wrong_mirror_arguments')
        if attempt['after']['mirrors']['heating'] is not True:issues.append('mirror_not_heated')
        if snapshot['tasks']:issues.append('unexpected_task')
        models = [event for event in report['traces'] if event['kind']=='model.completed']
        for agent,key in [('talker','talker'),('vehicle_planner','vehicle_planner')]:
            observed = [event['data'].get('meta',{}).get('model') for event in models if event['data'].get('agent')==agent]
            if not observed or any(model!=report['configured_models'][key] for model in observed):issues.append('missing_or_wrong_'+agent+'_model')
        report['errors'].extend(issues)
    except Exception as exc:
        report['errors'].append(type(exc).__name__+': '+str(exc))
    finally:
        await harness.close()
        report.update(finished_at=utcnow(),events=harness.events,audio_records=list(harness.consumer.records.values()),
                      transport_errors=harness.errors)
        report['pass'] = attempt['status']=='observed' and not report['errors'] and not harness.errors
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'pass':report['pass'],'query_to_python_nonzero_pcm_ms':attempt.get('query_to_first_nonzero_pcm_received_ms'),'errors':report['errors']},ensure_ascii=False))
    return 0 if report['pass'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--base',default='http://127.0.0.1:8003')
    parser.add_argument('--output',default='docs/progressive-voice-live.json')
    parser.add_argument('--fixture-dir',default='data/voice-interaction-fixtures')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
