import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const source=fs.readFileSync(new URL('../src/DeviceQueries.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {DeviceQueryRegistry}=await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
function command(action,args={},domain='media'){return {type:'cockpit.command',kind:'query',execution_location:'device',request_id:'request',command_id:'request',session_id:'session',target_device:'browser:session',turn_id:'turn',domain,action,arguments:args,timeout_ms:100,deadline_at:new Date(Date.now()+1000).toISOString()};}
test('device catalog query returns real registered metadata and keeps URL capabilities on device',async()=>{
  const registry=new DeviceQueryRegistry(()=>true);
  registry.setMediaCatalog([{content_id:'a',kind:'music',title:'A real local song',source_url:'/api/media/files/a',provider:'device_catalog'}]);
  const found=await registry.execute(command('search',{kind:'music',query:'local'}));assert.equal(found.ok,true);assert.equal(found.result.items[0].content_id,'a');assert.equal(found.result.items[0].source_url,undefined);assert.equal(found.state,undefined);assert.equal(found.state_version,undefined);
  const resolve=command('resolve',{content_id:'a'});resolve.command_id=resolve.request_id='resolve';const resolved=await registry.execute(resolve);
  assert.equal(resolved.result.item.content_id,'a');assert.equal(resolved.result.item.source_url,undefined);assert.equal(registry.resolveMedia('a').source_url,'/api/media/files/a');registry.dispose();
});
test('no local POI handler fails clearly and never calls a cloud replacement',async()=>{
  const registry=new DeviceQueryRegistry(()=>true);const result=await registry.execute(command('search_places',{near:'Paris'},'places'));
  assert.equal(result.ok,false);assert.equal(result.error.code,'unsupported_capability');assert.equal(result.request_id,'request');assert.equal(result.target_device,'browser:session');registry.dispose();
});
test('explicit device SDK registration gets structured arguments and request identity',async()=>{
  const registry=new DeviceQueryRegistry(()=>true);let calls=0;
  registry.register('places','search_places',async(args,ctx)=>{calls++;assert.equal(args.near,'Paris');assert.equal(ctx.command.session_id,'session');return {ok:true,kind:'places',provider:'device-sdk',places:[]};});
  const c=command('search_places',{near:'Paris'},'places');const a=await registry.execute(c);const b=await registry.execute(c);
  assert.equal(a.ok,true);assert.deepEqual(a,b);assert.equal(calls,1);registry.dispose();
});
test('timeout and interrupted turn discard late SDK query results',async()=>{
  let valid=true;const registry=new DeviceQueryRegistry(()=>valid);let resolve;
  registry.register('places','search_places',()=>new Promise(done=>{resolve=done;}));
  const c=command('search_places',{},'places');c.timeout_ms=5;const timed=await registry.execute(c);assert.equal(timed.error.code,'device_timeout');resolve({ok:true,places:[{name:'late'}]});
  const interrupted=command('search_places',{},'places');interrupted.command_id=interrupted.request_id='second';const pending=registry.execute(interrupted);valid=false;registry.invalidate('turn');assert.equal((await pending).error.code,'stale_command');resolve({ok:true,places:[]});registry.dispose();
});

test('duplicate provider IDs in different media kinds cannot resolve across CP boundaries',()=>{
  const registry=new DeviceQueryRegistry(()=>true);registry.setMediaCatalog([{content_id:'same',kind:'music',title:'Audio',source_url:'/api/media/files/audio'},{content_id:'same',kind:'video',title:'Video',source_url:'/api/media/files/video'}]);
  assert.equal(registry.resolveMedia('same'),undefined);assert.equal(registry.resolveMedia('same','music').source_url,'/api/media/files/audio');assert.equal(registry.resolveMedia('same','video').source_url,'/api/media/files/video');registry.dispose();
});
