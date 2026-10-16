// Explicit microphone/protocol doubles; live VAD/ASR evidence is recorded separately.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const source = fs.readFileSync(new URL('../src/HandsFreeRecognition.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { HandsFreeRecognition } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
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
  const captures = [], phases = [], finals = [], errors = []; let records = [];
  let engine;
  engine = new HandsFreeRecognition({ capture: (id, active) => captures.push({ id, active }), unlockSpeech: async () => {},
    phase: phase => phases.push(phase), transcript: () => {}, records: r => records = r, error: e => errors.push(e),
    final: async (text, id) => { await options.finalGate?.promise; if (engine.isCurrent(id)) { engine.markSubmitted(id, performance.now()); finals.push(text); } },
  });
  return { engine, track, stream, captures, phases, finals, errors, records: () => records, socket: () => sockets.at(-1), node: () => nodes.at(-1) };
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
