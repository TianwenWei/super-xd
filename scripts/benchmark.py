"""Real Ark/Thinker/Tavily session benchmark. No mocked model or information tool.
The WS harness drives the delayed simulated device and returns its bound receipt.
"""
if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import argparse
import asyncio
import json
import math
import time
from pathlib import Path
import httpx
import websockets
from scripts.vehicle_device import execute_vehicle
from backend.models import utcnow
from backend.session import effective_text

CHATS = ['晚上好，陪我聊两句。', '用一句话形容秋天。', '给我一句轻松的鼓励。', '你喜欢安静的雨天吗？', '讲一个简短的小笑话。', '如果周末可以完全放空，你会怎么安排？']
CONTROLS = [
    ('把空调温度设为23度', 'set_temperature', lambda s:s['climate']['temperatures']['driver']==23),
    ('关闭全部车窗', 'close_all_windows', lambda s:all(x==0 for x in s['windows']['positions'].values())),
    ('把音量设为40', 'set_volume', lambda s:s['media']['volume']==40),
    ('把左前车窗打开到20%', 'set_window_position', lambda s:s['windows']['positions']['driver']==20),
    ('打开空调', 'set_climate_power', lambda s:s['climate']['power'] is True),
    ('风量设为3档', 'set_fan_speed', lambda s:s['climate']['fan_speed']==3),
    ('主驾座椅加热设为1档', 'set_seat_heating', lambda s:s['seats']['positions']['driver']['heating']==1),
    ('打开前排车内灯', 'set_interior_light', lambda s:s['lights']['interior']['front'] is True),
    ('暂停音乐', 'control_playback', lambda s:s['media']['playback']=='paused'),
    ('把模拟导航目的地设为上海虹桥站', 'set_navigation_destination', lambda s:s['navigation']['destination']=='上海虹桥站'),
]

def percentile(values, p):
    if not values: return None
    values = sorted(values)
    ix = (len(values)-1)*p
    lo, hi = math.floor(ix), math.ceil(ix)
    return round(values[lo] + (values[hi]-values[lo])*(ix-lo),1)


def summarize(samples, kind):
    relevant = [s for s in samples if s['kind']==kind]
    field = 'first_effective_ms' if kind=='chat' else 'receipt_ms'
    valid = [s[field] for s in relevant if s['success'] and s.get(field) is not None]
    return {'attempts':len(relevant), 'successful':len(valid), 'p50_ms':percentile(valid,.5), 'p95_ms':percentile(valid,.95),
            'within_1s_rate':round(sum(s['success'] and s.get(field) is not None and s[field]<=1000 for s in relevant)/len(relevant),3) if relevant else None,
            'within_2s_rate':round(sum(s['success'] and s.get(field) is not None and s[field]<=2000 for s in relevant)/len(relevant),3) if relevant else None,
            'p50_target_ms':2000, 'target_met': percentile(valid,.5)<=2000 if valid else False,
            'statistics_note':'P50/P95 of successful attempts; completion rates include all attempts (failures count as misses).'}


class Harness:
    def __init__(self, base):
        self.base=base.rstrip('/')
        self.http=httpx.AsyncClient(base_url=self.base, timeout=15)
        self.events=[]; self.device_jobs=[]
    async def connect(self):
        self.sid=(await self.http.post('/api/sessions')).json()['session_id']
        self.ws=await websockets.connect(self.base.replace('http','ws',1)+'/ws/'+self.sid)
        await self.ws.recv()
    async def send(self, data):
        await self.ws.send(json.dumps(data,ensure_ascii=False))
    async def ask(self, query, kind, expected=None, state_check=None):
        at=utcnow();start=time.perf_counter(); turn=None;buf='';first=None;receipts=[];calls=[];traces=[]
        await self.send({'type':'input','text':query})
        async with asyncio.timeout(100):
            while True:
                e=json.loads(await self.ws.recv()); self.events.append(e)
                if e['type']=='vehicle.command':
                    self.device_jobs.append(asyncio.create_task(execute_vehicle(self.http,self.base,self.sid,e,self.ws)))
                    if e.get('turn_id')==turn: calls.append({'name':e['name'],'arguments':e['arguments']})
                if e['type']=='turn.started' and e.get('text')==query:
                    turn=e['turn_id']
                if e.get('turn_id')!=turn:continue
                if e['type']=='stream.delta':
                    buf+=e['text']
                    if first is None and effective_text(buf):first=(time.perf_counter()-start)*1000
                if e['type']=='vehicle.result':receipts.append({'elapsed_ms':(time.perf_counter()-start)*1000,'result':e['result']})
                if e['type']=='stream.done':
                    error=e.get('metrics',{}).get('error')
                    success=not error and bool(buf) and (not calls if kind=='chat' else True)
                    if kind=='control':success=bool(receipts) and all(r['result']['ok'] for r in receipts) and len(calls)==1 and calls[0]['name']==expected and state_check(receipts[-1]['result']['state'])
                    # Inspect audit tool history; this proves the hot path did not load a skill.
                    snapshot=(await self.http.get('/api/sessions/'+self.sid)).json()
                    traces=[c['function']['name'] for m in snapshot['history'] for c in m.get('tool_calls',[])]
                    return {'query':query,'kind':kind,'at':at,'success':bool(success),'first_effective_ms':round(first,1) if first else None,
                            'receipt_ms':round(max(r['elapsed_ms'] for r in receipts),1) if receipts else None,
                            'total_ms':round((time.perf_counter()-start)*1000,1),'calls':calls,'text':buf,'metrics':e.get('metrics'),
                            'background_active':[{'id':t['id'],'status':t['status'],'revision':t['revision']} for t in snapshot['tasks'] if t['status'] in {'queued','running','waiting_clarification'}],
                            'hot_path_only':not any(x in {'load_skill','discover_tools'} for x in traces), 'error':error}
    async def close(self):
        await asyncio.gather(*self.device_jobs)
        await self.ws.close();await self.http.aclose()


async def run_phase(base, name, pairs, background=False):
    h=Harness(base);await h.connect();samples=[]
    try:
        if background:
            await h.send({'type':'task.create','goal':'请真实调研法国巴黎科学工业城与自然历史博物馆适合带孩子参观的差异。读取两家官方来源，核实开放日、交通和儿童活动；给出详细比较并说明查询时间。'})
            await h.send({'type':'task.create','goal':'请真实调研Tavily Search和Extract API的区别、失败情况及来源追溯。读取至少三个官方页面，给出对实时车载助手的工程建议和查询时间。'})
            await asyncio.sleep(.5)
        for i in range(pairs):
            for kind in ('chat','control'):
                if kind=='chat':query=CHATS[i%len(CHATS)]; args=(query,kind)
                else:
                    query,expected,check=CONTROLS[i%len(CONTROLS)];args=(query,kind,expected,check)
                try:sample=await h.ask(*args)
                except Exception as exc:sample={'query':query,'kind':kind,'success':False,'error':str(exc),'at':utcnow()}
                samples.append(sample)
                print(name,kind,i+1,'OK' if sample['success'] else 'FAIL',sample.get('first_effective_ms'),sample.get('receipt_ms'),flush=True)
        tasks=(await h.http.get('/api/sessions/'+h.sid)).json()['tasks']
        result={'name':name,'session_id':h.sid,'samples':samples,'chat':summarize(samples,'chat'),'control':summarize(samples,'control'),'tasks':tasks}
        if background:
            for t in tasks:
                if t['status'] in {'queued','running','waiting_clarification'}:await h.send({'type':'task.cancel','task_id':t['id'],'expected_revision':t['revision']})
        return result
    finally:await h.close()


async def main():
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8000');p.add_argument('--pairs',type=int,default=10);p.add_argument('--output',default='docs/latency-live.json');args=p.parse_args()
    report={'at':utcnow(),'method':'Real Ark over local WS harness; submit to first effective text or validated simulated state commit/result. No browser speech latency. Warm server connections; sequential queries within one session; all failures retained.','target_ms':2000,'phases':[]}
    for name,bg in [('baseline',False),('background_running',True)]:
        report['phases'].append(await run_phase(args.base,name,args.pairs,bg))
        Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({p['name']:{'chat':p['chat'],'control':p['control']} for p in report['phases']},ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
