// Isolated protocol tests. Synthetic PCM here is never included in live measurements.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const source = fs.readFileSync(new URL('../src/StreamingSpeech.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { StreamingSpeech } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
let sockets = [], nodes = [];
globalThis.location = { protocol: 'http:', host: 'localhost' };
globalThis.WebSocket = class { static OPEN=1; static CONNECTING=0; readyState=1; constructor() { sockets.push(this); } close() {} };
globalThis.AudioContext = class { state='running'; sampleRate=44100; destination={}; audioWorklet={addModule:async()=>{}}; resume(){return Promise.resolve();} close(){return Promise.resolve();} };
globalThis.AudioWorkletNode = class { constructor() { this.sent=[]; this.port={postMessage:m=>this.sent.push(m)}; nodes.push(this); } connect(){} disconnect(){} };
const id = digit => digit.repeat(32);
function packet(playbackId, samples=[0,123,45]) { const buffer=new ArrayBuffer(32+samples.length*2); new Uint8Array(buffer,0,32).set(new TextEncoder().encode(playbackId)); const view=new DataView(buffer,32); samples.forEach((v,i)=>view.setInt16(i*2,v,true)); return buffer; }
async function setup(allowed=()=>true) { sockets=[]; nodes=[]; const sent=[], errors=[]; let records=[]; const engine=new StreamingSpeech({send:m=>sent.push(m),phase:()=>{},metrics:()=>{},records:r=>records=r,error:e=>errors.push(e),allowed}); engine.connect('session'); await engine.setEnabled(true); return {engine,sent,errors,records:()=>records,socket:sockets.at(-1),node:nodes.at(-1)}; }
function start(engine, request='request-a', turn='turn-a', playback=id('a')) { engine.metadata({type:'audio.started',turn_id:turn,playback_id:playback,client_request_id:request,sample_rate:24000,format:'pcm_s16le',channels:1}); }
test('connection metadata without playback ID is safe and snapshot is retained', async()=>{ const h=await setup(); h.engine.metadata({type:'audio.connection',pool_ready:1}); h.engine.submit('request-a','query',performance.now(),[]); assert.equal(h.records()[0].diagnostics.connection_at_submit.pool_ready,1); assert.equal(h.errors.length,0); h.engine.dispose(); });
test('audio can precede control turn.started and first nonzero is only measured on a real sample', async()=>{ const h=await setup(); h.engine.submit('request-a','query',performance.now(),[]); start(h.engine); h.socket.onmessage({data:packet(id('a'),[0,0])}); assert.ok(h.records()[0].first_pcm_perf_ms!==undefined); assert.equal(h.records()[0].first_nonzero_pcm_perf_ms,undefined); h.socket.onmessage({data:packet(id('a'),[0,3])}); h.engine.turnStarted('turn-a','request-a'); assert.ok(h.records()[0].first_nonzero_pcm_perf_ms!==undefined); assert.equal(h.records().length,1); assert.equal(h.records()[0].pcm_bytes,8); h.engine.dispose(); });
test('new query immediately clears old queue and rejects late old starts, packets and renders', async()=>{ const h=await setup(); h.engine.submit('request-a','old',performance.now(),[]); start(h.engine); h.socket.onmessage({data:packet(id('a'))}); const bytes=h.records()[0].pcm_bytes; h.engine.submit('request-b','new',performance.now(),[]); assert.equal(h.node.sent.at(-1).type,'clear'); start(h.engine); h.socket.onmessage({data:packet(id('a'))}); h.node.port.onmessage({data:{type:'first-render',playback_id:id('a')}}); assert.equal(h.records()[0].pcm_bytes,bytes); assert.equal(h.records()[0].status,'cancelled'); assert.equal(h.records()[0].first_render_observed_perf_ms,undefined); start(h.engine,'request-b','turn-b',id('b')); h.socket.onmessage({data:packet(id('b'))}); assert.ok(h.records()[1].first_nonzero_pcm_perf_ms!==undefined); h.engine.dispose(); });
test('provider input ended retains speaking until Worklet queue drains', async()=>{ const h=await setup(); h.engine.submit('request-a','query',performance.now(),[]); start(h.engine); h.socket.onmessage({data:packet(id('a'))}); h.engine.metadata({type:'audio.ended',turn_id:'turn-a',playback_id:id('a')}); assert.equal(h.sent.filter(m=>m.type==='playback'&&m.speaking===false).length,0); assert.equal(h.records()[0].status,'pending'); h.node.port.onmessage({data:{type:'drained',playback_id:id('a'),rendered_output_samples:6}}); assert.equal(h.sent.filter(m=>m.type==='playback'&&m.speaking===false).length,1); assert.equal(h.records()[0].status,'ended'); h.engine.dispose(); });
test('recording gate clears old playback and rejects newly arriving speech starts and PCM', async()=>{
  let recording=false; const h=await setup(()=>!recording);
  h.engine.submit('request-a','old',performance.now(),[]); start(h.engine); h.socket.onmessage({data:packet(id('a'))});
  const bytes=h.records()[0].pcm_bytes; recording=true; await h.engine.setEnabled(false);
  assert.equal(h.node.sent.at(-1).type,'clear');
  h.socket.onmessage({data:packet(id('a'))}); start(h.engine,'request-b','background',id('b')); h.socket.onmessage({data:packet(id('b'))});
  assert.equal(h.records()[0].pcm_bytes,bytes); assert.equal(h.records().some(record=>record.turn_id==='background'),false);
  // Gate still protects the mic even if an external caller attempts an enable.
  await h.engine.setEnabled(true); start(h.engine,'request-c','another-background',id('c'));
  h.socket.onmessage({data:packet(id('c'))}); assert.equal(h.records().some(record=>record.turn_id==='another-background'),false);
  recording=false; h.engine.dispose();
});
test('cancel waits for worklet progress and reports it under the cancelled playback', async()=>{
  const h=await setup(); h.engine.submit('request-a','old',performance.now(),[]); start(h.engine);
  h.socket.onmessage({data:packet(id('a'))});
  let resolved=false; const stopped=h.engine.cancel('user_stop').then(()=>{resolved=true;});
  await Promise.resolve(); assert.equal(resolved,false);
  h.node.port.onmessage({data:{type:'stopped',playback_id:id('a'),rendered_output_samples:22050,played_ms:500}});
  await stopped;
  const report=h.sent.find(m=>m.type==='audio.played');
  assert.equal(report.turn_id,'turn-a'); assert.equal(report.playback_id,id('a')); assert.equal(report.played_ms,500);
  h.engine.dispose();
});
