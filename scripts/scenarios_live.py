"""Live complex-intent task using vehicle context, update and persona switch."""
if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import asyncio,json,time
from pathlib import Path
from scripts.benchmark import Harness
from scripts.vehicle_device import execute_vehicle
from backend.models import utcnow

async def main():
 h=Harness('http://127.0.0.1:8000');await h.connect();timeline=[];report={'at':utcnow(),'session_id':h.sid}
 try:
  report['initial_control']=await h.ask('把左前车窗打开到40%','control','set_window_position',lambda s:s['windows']['positions']['driver']==40)
  await h.send({'type':'task.create','goal':'我有点冷又听到风声，请结合当前车况提出舒适调整方案。不要联网，只提出模拟车控建议，不要直接执行。'})
  task_id=None;updated=False;started=time.perf_counter()
  async with asyncio.timeout(300):
   while True:
    e=json.loads(await h.ws.recv())
    if e['type']=='vehicle.command':h.device_jobs.append(asyncio.create_task(execute_vehicle(h.http,h.base,h.sid,e,h.ws)))
    if e['type'] in {'task.event','ui.card'}:
     task=e.get('task',{});task_id=task.get('id') or task_id
     timeline.append({k:task.get(k) for k in ['id','status','revision','summary','steps']})
     if task.get('status')=='running' and not updated:
      await h.send({'type':'persona.set','preset':'playful'})
      await h.send({'type':'task.update','task_id':task_id,'text':'先关车窗，温度不要超过24度','expected_revision':task['revision']})
      updated=True
     if task.get('status')=='waiting_clarification':
      report.setdefault('clarification_questions',[]).append(task.get('question'))
      await h.send({'type':'task.reply','task_id':task_id,'text':'先关闭所有车窗，空调设为24度，风量2档，只给建议','expected_revision':task['revision']})
     if task.get('status') in {'failed','cancelled'}:
      report['task']=task;break
    if e['type']=='stream.done' and e.get('turn_id'):
     snapshot=(await h.http.get('/api/sessions/'+h.sid)).json()
     task=next((t for t in snapshot['tasks'] if t['id']==task_id),None)
     if task and task['status']=='completed':
      report['task']=task;report['final_text']=e['text'];report['final_persona']=snapshot['persona']['id'];report['vehicle_after']=snapshot['vehicle'];break
  report['elapsed_seconds']=round(time.perf_counter()-started,3);report['timeline']=timeline
  task=report.get('task',{});result=task.get('result') or {}
  report['checks']={'completed':task.get('status')=='completed','same_task_updated':updated and task.get('revision',0)>=2,'condition_retained':'先关车窗，温度不要超过24度' in task.get('constraints',[]),'vehicle_context':task.get('context',{}).get('vehicle',{}).get('windows',{}).get('positions',{}).get('driver')==40,'neutral_suggestions':bool(result.get('suggested_actions')),'no_web_tools':not any(o.get('tool') in {'search_web','extract_web'} for o in result.get('tool_observations',[])),'current_persona':report.get('final_persona')=='playful' and report.get('final_text','').startswith('结果出炉啦！'),'no_automatic_execution':report.get('vehicle_after',{}).get('windows',{}).get('positions',{}).get('driver')==40}
 except Exception as exc:report['error']=str(exc)
 finally:
  await h.close();Path('docs/scenarios-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k not in {'task','timeline','vehicle_after'}},ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
