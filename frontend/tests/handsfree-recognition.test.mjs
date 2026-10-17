// Explicit microphone/protocol doubles; live VAD/ASR evidence is recorded separately.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const activationSource = fs.readFileSync(new URL('../src/audioActivation.ts', import.meta.url), 'utf8');
const activationCompiled = ts.transpileModule(activationSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const activationModule = `data:text/javascript;base64,${Buffer.from(activationCompiled).toString('base64')}`;
const source = fs.readFileSync(new URL('../src/HandsFreeRecognition.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { HandsFreeRecognition } = await import(`data:text/javascript;base64,${Buffer.from(compiled.replace("from './audioActivation'", `from '${activationModule}'`)).toString('base64')}`);
globalThis.location = { protocol: 'http:', host: 'localhost' };
let sockets, nodes, contexts;
globalThis.WebSocket = class {
  static OPEN = 1;
  readyState = 0; bufferedAmount = 0; sent = [];
  constructor() { sockets.push(this); }
  send(data) { this.sent.push(data); }
  open() { this.readyState = 1; this.onopen?.(); }
  message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
  close() { this.readyState = 3; this.onclose?.(); }
};
const connected = () => ({ connect() {}, disconnect() {} });
globalThis.AudioContext = class {
  state = 'running'; sampleRate = 48000; destination = {}; audioWorklet = { addModule: async () => {} };
  constructor() { contexts.push(this); }
  resume() { return Promise.resolve(); }
  createMediaStreamSource() { return connected(); }
  createGain() { return { ...connected(), gain: { value: 1 } }; }
  close() { this.state = 'closed'; return Promise.resolve(); }
};
globalThis.AudioWorkletNode = class {
  constructor() { this.sent = []; this.port = { postMessage: m => this.sent.push(m) }; nodes.push(this); }
  connect() {} disconnect() {}
  emit(data) { this.port.onmessage?.({ data }); }
};
function deferred() { let resolve, reject; const promise = new Promise((a,b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
const tick = async () => { for (let i=0; i<8; i++) await Promise.resolve(); };
function harness(options = {}) {
  sockets = []; nodes = []; contexts = [];
  const track = { stopped: 0, stop() { this.stopped++; } }, stream = { getTracks: () => [track] };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: options.media || (async () => stream) } } });
  const captures = [], phases = [], finals = [], errors = [], failures = []; let records = [];
  let engine;
  engine = new HandsFreeRecognition({ capture: (id, active) => captures.push({ id, active }), unlockSpeech: async () => {},
    phase: phase => phases.push(phase), transcript: () => {}, records: r => records = r, error: (e, failure) => { errors.push(e); failures.push(failure); },
    final: async (text, id) => { await options.finalGate?.promise; if (engine.isCurrent(id)) { engine.markSubmitted(id, performance.now()); finals.push(text); } },
  });
  return { engine, track, stream, captures, phases, finals, errors, failures, records: () => records, socket: () => sockets.at(-1), node: () => nodes.at(-1) };
}
async function ready(h) {
  await h.engine.start('session'); h.socket().open();
  const id = JSON.parse(h.socket().sent[0]).listen_id;
  h.socket().message({ type: 'voice.ready', listen_id: id }); return id;
}
function event(h, listen, type, capture='a', more={}) { h.socket().message({ type, listen_id: listen, capture_id: capture, ...more }); }

test('continuous capture stays open through final and reply; next speech reserves a fresh interruption', async () => {
  const h=harness(), id=await ready(h);
  assert.equal(h.phases.at(-1), 'listening');
  assert.equal(h.node().sent.at(-1).frame_samples, 512);
  event(h,id,'voice.speech_start'); event(h,id,'voice.speech_end'); event(h,id,'voice.final','a',{text:'打开空调'});
  await tick(); assert.deepEqual(h.finals,['打开空调']); assert.equal(h.track.stopped,0); assert.equal(h.engine.active,true);
  assert.equal(h.records()[0].status,'submitted'); assert.equal(h.phases.at(-1),'listening');
  h.node().emit({type:'pcm',capture_id:id,buffer:new ArrayBuffer(1024)});
  assert.equal(h.socket().sent.at(-1).byteLength,1024);
  event(h,id,'voice.speech_start','b'); assert.equal(h.captures.at(-1).id,'b');
  h.engine.dispose(); assert.equal(h.track.stopped,1);
});
test('new speech during pending final drops old final and keeps the microphone running', async () => {
  const finalGate=deferred(), h=harness({finalGate}), id=await ready(h);
  event(h,id,'voice.speech_start'); event(h,id,'voice.speech_end'); event(h,id,'voice.final','a',{text:'old'});
  event(h,id,'voice.speech_start','b'); finalGate.resolve(); await tick();
  event(h,id,'voice.final','a',{text:'late'}); assert.deepEqual(h.finals,[]); assert.equal(h.engine.isCurrent('b'),true);
  assert.equal(h.records()[0].status,'cancelled'); h.engine.dispose();
});
test('early, duplicate and wrong-listener finals cannot submit', async () => {
  const h=harness(), id=await ready(h);
  event(h,id,'voice.speech_start'); event(h,id,'voice.final','a',{text:'early'});
  event(h,'wrong','voice.speech_end'); event(h,id,'voice.speech_end');
  event(h,id,'voice.final','a',{text:'valid'}); event(h,id,'voice.final','a',{text:'duplicate'});
  await tick(); assert.deepEqual(h.finals,['valid']); h.engine.dispose();
});
test('cancel before microphone permission resolves releases late media and creates no socket', async () => {
  const permission=deferred(), h=harness({media:()=>permission.promise});
  const start=h.engine.start('session'); h.engine.cancel(); permission.resolve(h.stream); await start;
  assert.equal(h.track.stopped,1); assert.equal(sockets.length,0); assert.equal(contexts[0].state,'closed');
  assert.equal(h.engine.active,false); assert.equal(h.phases.at(-1),'idle');
});
test('cancel during connection closes capture and ignores late socket readiness or speech', async () => {
  const h=harness(); await h.engine.start('session');
  const socket=h.socket(), node=h.node();
  assert.equal(h.phases.at(-1),'connecting');
  h.engine.cancel('user_cancel');
  socket.open();
  socket.message({type:'voice.ready',listen_id:'late-listener'});
  socket.message({type:'voice.speech_start',listen_id:'late-listener',capture_id:'late-capture'});
  node.emit({type:'pcm',capture_id:'late-listener',buffer:new ArrayBuffer(1024)});
  await tick();
  assert.equal(socket.sent.length,0,'A cancelled connection cannot start a server listener or upload PCM.');
  assert.equal(h.track.stopped,1); assert.equal(contexts[0].state,'closed');
  assert.equal(h.engine.active,false); assert.equal(h.phases.at(-1),'idle');
  assert.deepEqual(h.captures,[]); assert.deepEqual(h.finals,[]); assert.equal(sockets.length,1);
});
test('cancel drops both an in-flight final callback and later server packets without restarting capture', async () => {
  const finalGate=deferred(), h=harness({finalGate}), id=await ready(h), socket=h.socket(), node=h.node();
  event(h,id,'voice.speech_start'); event(h,id,'voice.speech_end'); event(h,id,'voice.final','a',{text:'pending'});
  assert.equal(h.phases.at(-1),'finalizing');
  h.engine.cancel('user_cancel'); finalGate.resolve(); await tick();
  const sent=socket.sent.length;
  event(h,id,'voice.final','a',{text:'late'}); event(h,id,'voice.speech_start','late-capture');
  node.emit({type:'pcm',capture_id:id,buffer:new ArrayBuffer(1024)}); await tick();
  assert.deepEqual(h.finals,[]); assert.equal(h.engine.active,false); assert.equal(h.phases.at(-1),'idle');
  assert.equal(h.track.stopped,1); assert.equal(contexts[0].state,'closed');
  assert.equal(h.records()[0].status,'cancelled'); assert.equal(h.records()[0].reason,'user_cancel');
  assert.equal(socket.sent.length,sent); assert.equal(sockets.length,1);
  assert.deepEqual(JSON.parse(socket.sent.at(-1)),{type:'voice.stop',listen_id:id});
});
test('backpressure stops microphone and releases the capture without submitting partial text', async () => {
  const h=harness(), id=await ready(h); event(h,id,'voice.speech_start');
  h.socket().bufferedAmount=32001; h.node().emit({type:'pcm',capture_id:id,buffer:new ArrayBuffer(1024)});
  assert.equal(h.engine.active,false); assert.equal(h.track.stopped,1); assert.deepEqual(h.finals,[]); assert.equal(h.captures.at(-1).active,false);
});
test('server failure and empty final never enter Talker', async () => {
  const h=harness(), id=await ready(h); event(h,id,'voice.speech_start'); event(h,id,'voice.speech_end');
  event(h,id,'voice.final','a',{text:' '}); await tick(); assert.deepEqual(h.finals,[]); assert.equal(h.engine.active,true);
  event(h,id,'voice.error',null,{message:'provider failed'}); assert.equal(h.engine.active,false);
});

test('typing discards only the current segment while continuous PCM and the next turn remain live', async () => {
  const h=harness(), listen=await ready(h);
  event(h,listen,'voice.speech_start','old');
  h.engine.discardCapture('text_input');
  assert.equal(h.engine.active,true);
  assert.equal(h.track.stopped,0);
  assert.equal(h.captures.at(-1).active,false);
  h.node().emit({type:'pcm',capture_id:listen,buffer:new ArrayBuffer(1024)});
  assert.equal(h.socket().sent.at(-1).byteLength,1024);
  event(h,listen,'voice.speech_end','old'); event(h,listen,'voice.final','old',{text:'discarded'});
  event(h,listen,'voice.speech_start','new'); event(h,listen,'voice.speech_end','new');
  event(h,listen,'voice.final','new',{text:'下一句'}); await tick();
  assert.deepEqual(h.finals,['下一句']);
  assert.equal(h.engine.active,true); h.engine.dispose();
});

test('a suspended live capture context resumes instead of silently showing listening without PCM', async () => {
  const h=harness(); await ready(h); const context=contexts[0];
  let resumes=0;
  context.resume=async()=>{resumes++; context.state='running';};
  context.state='suspended'; context.onstatechange?.(); await tick();
  assert.equal(resumes,1);
  assert.equal(context.state,'running');
  assert.equal(h.engine.active,true); assert.equal(h.track.stopped,0); h.engine.dispose();
});


test('provider auth errors are classified and stop once without automatic retry', async () => {
  const h=harness(), id=await ready(h); event(h,id,'voice.speech_start');
  const count=sockets.length;
  event(h,id,'voice.error','a',{message:'Provider rejected authorization',provider_code:'auth'});
  await tick();
  assert.deepEqual(h.failures.at(-1),{kind:'provider',providerCode:'auth'});
  assert.equal(h.errors.at(-1).startsWith('语音识别服务授权失败'),true);
  assert.equal(h.engine.active,false); assert.equal(h.track.stopped,1);
  assert.equal(sockets.length,count); assert.deepEqual(h.finals,[]);
});
