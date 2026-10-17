"""Real Xiaohe2.0 probe, safe metadata only; never print credential values."""
if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import asyncio
import hashlib
import json
from pathlib import Path
from backend.settings import ROOT
from backend.models import utcnow
from backend.tts import VolcanoTTS, XIAOHE_VOICE, TTS_RESOURCE_ID

async def main():
    tts=VolcanoTTS()
    result={'at':utcnow(),'provider':'volcengine-speech','voice':XIAOHE_VOICE,'resource':TTS_RESOURCE_ID,'configured':tts.info()['configured'],'samples':[]}
    try:
        for i,text in enumerate(['晚上好，今天想聊些什么？','主驾车窗开度已经设为百分之五十。','后台还在继续查询，你可以先和我聊天，或者调整空调。']):
            try:
                sample=await tts.synthesize_with_metrics(text)
                path=ROOT/'data'/f'xiaohe-demo-{i+1}.mp3';path.write_bytes(sample['audio'])
                result['samples'].append({'text':text,'ok':True,'first_packet_ms':round(sample['first_packet_ms'],1),'total_ms':round(sample['total_ms'],1),'audio_bytes':len(sample['audio']),'sha256':hashlib.sha256(sample['audio']).hexdigest(),'file':str(path)})
            except RuntimeError as exc:
                result['samples'].append({'text':text,'ok':False,'error':str(exc)})
                break
    finally:await tts.aclose()
    (ROOT/'docs'/'tts-live.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
