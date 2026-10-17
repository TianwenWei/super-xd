// Isolated microphone/protocol fakes: these never request a real device or ASR.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const activationSource = fs.readFileSync(new URL('../src/audioActivation.ts', import.meta.url), 'utf8');
const activationCompiled = ts.transpileModule(activationSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const activationModule = `data:text/javascript;base64,${Buffer.from(activationCompiled).toString('base64')}`;
const source = fs.readFileSync(new URL('../src/StreamingRecognition.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { StreamingRecognition } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const handsFreeSource = fs.readFileSync(new URL('../src/HandsFreeRecognition.ts', import.meta.url), 'utf8');
const handsFreeCompiled = ts.transpileModule(handsFreeSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { HandsFreeRecognition } = await import(`data:text/javascript;base64,${Buffer.from(handsFreeCompiled.replace("from './audioActivation'", `from '${activationModule}'`)).toString('base64')}`);
const speechSource = fs.readFileSync(new URL('../src/StreamingSpeech.ts', import.meta.url), 'utf8');
const speechCompiled = ts.transpileModule(speechSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const textSource = fs.readFileSync(new URL('../src/text.ts', import.meta.url), 'utf8');
const textCompiled = ts.transpileModule(textSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const textModule = `data:text/javascript;base64,${Buffer.from(textCompiled).toString('base64')}`;
const { StreamingSpeech } = await import(`data:text/javascript;base64,${Buffer.from(speechCompiled.replace("from './text'", `from '${textModule}'`).replace("from './audioActivation'", `from '${activationModule}'`)).toString('base64')}`);
globalThis.location = { protocol: 'http:', host: 'localhost' };
let sockets = [], nodes = [], contexts = [];
globalThis.WebSocket = class {
  static OPEN = 1; static CONNECTING = 0;
  readyState = 0; bufferedAmount = 0; sent = [];
  constructor(url) { this.url = url; sockets.push(this); }
  send(message) { this.sent.push(message); }
  open() { this.readyState = 1; this.onopen?.(); }
  receive(message) { this.onmessage?.({ data: JSON.stringify(message) }); }
  close() { this.readyState = 3; this.onclose?.(); }
};
const connection = () => ({ connect() {}, disconnect() {} });
globalThis.AudioContext = class {
  state = 'running'; sampleRate = 48000; destination = {}; audioWorklet = { addModule: async () => {} };
  constructor() { contexts.push(this); }
  resume() { return Promise.resolve(); }
  createMediaStreamSource() { return connection(); }
  createGain() { return { ...connection(), gain: { value: 1 } }; }
  close() { this.state = 'closed'; return Promise.resolve(); }
};
globalThis.AudioWorkletNode = class {
  constructor() { this.sent = []; this.port = { postMessage: m => this.sent.push(m) }; nodes.push(this); }
  connect() {} disconnect() {}
  emit(data) { this.port.onmessage?.({ data }); }
};
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
function media() { const track = { stopped: 0, stop() { this.stopped++; } }; return { track, stream: { getTracks: () => [track] } }; }
function harness(options = {}) {
  sockets = []; nodes = []; contexts = [];
  const device = media(); const captures = [], phases = [], transcripts = [], finals = [], errors = [], settled = []; let records = [];
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: options.getUserMedia || (async () => device.stream) } } });
  let engine;
  engine = new StreamingRecognition({
    capture: (id, active) => captures.push({ id, active }), unlockSpeech: async () => {}, phase: v => phases.push(v), transcript: v => transcripts.push(v),
    final: async (text, id) => { await options.beforeSubmit?.promise; await options.enableSpeech?.(); if (engine.isCurrent(id)) { finals.push({ text, id }); engine.markSubmitted(id, performance.now()); } },
    settled: reason => { settled.push(reason); options.onSettled?.(reason); }, records: r => records = r, error: e => errors.push(e),
  });
  return { engine, device, captures, phases, transcripts, finals, errors, settled, records: () => records, socket: () => sockets.at(-1), node: () => nodes.at(-1) };
}
async function ready(h) {
  await h.engine.start('session-id');
  const socket = h.socket(); const startId = h.captures.at(-1).id;
  socket.open(); socket.receive({ type: 'asr.ready', capture_id: startId });
  return startId;
}
function flush(h, id) { h.engine.stop(); h.node().emit({ type: 'pcm', capture_id: id, buffer: new ArrayBuffer(20) }); h.node().emit({ type: 'flushed', capture_id: id }); }
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };

for (const reason of ['user_cancel', 'disconnected', 'disposed']) test(`late microphone permission after ${reason} stops tracks and opens no ASR socket`, async () => {
  const permission = deferred(), device = media(), h = harness({ getUserMedia: () => permission.promise });
  const start = h.engine.start('session'); h.engine.cancel(reason); permission.resolve(device.stream); await start;
  assert.equal(device.track.stopped, 1); assert.equal(sockets.length, 0); assert.equal(nodes.length, 0);
  assert.equal(contexts[0].state, 'closed'); assert.equal(h.engine.active, false);
  assert.deepEqual(h.captures.map(v => v.active), [true, false]); assert.equal(h.finals.length, 0);
});
test('partials replace preview, early/wrong finals do not submit, whole final submits once only after explicit stop', async () => {
  const h = harness(); const id = await ready(h), socket = h.socket();
  assert.match(socket.url, /\/ws\/session-id\/asr$/);
  assert.deepEqual(JSON.parse(socket.sent[0]), { type: 'asr.start', capture_id: id, format: 'pcm_s16le', sample_rate: 16000, channels: 1 });
  socket.receive({ type: 'asr.partial', capture_id: id, text: '把空调' }); socket.receive({ type: 'asr.partial', capture_id: id, text: '把空调调到二十三度' });
  socket.receive({ type: 'asr.final', capture_id: id, text: 'early result' }); assert.equal(h.finals.length, 0);
  assert.equal(h.transcripts.at(-1), '把空调调到二十三度');
  flush(h, id); assert.equal(h.device.track.stopped, 1); assert.equal(JSON.parse(socket.sent.at(-1)).type, 'asr.stop');
  socket.receive({ type: 'asr.final', capture_id: 'old-id', text: 'wrong' }); assert.equal(h.finals.length, 0);
  socket.receive({ type: 'asr.final', capture_id: id, text: '把空调调到二十三度' });
  socket.receive({ type: 'asr.final', capture_id: id, text: 'duplicate' }); await tick();
  assert.deepEqual(h.finals.map(v => v.text), ['把空调调到二十三度']); assert.equal(h.records()[0].status, 'submitted');
  assert.ok(h.records()[0].submitted_perf_ms >= h.records()[0].final_received_perf_ms);
  assert.equal(h.engine.active, false); assert.equal(h.errors.length, 0);
});
for (const reason of ['text_input', 'disconnected']) test(`new ${reason} during final-to-TTS await invalidates pending final`, async () => {
  const beforeSubmit = deferred(), h = harness({ beforeSubmit }); const id = await ready(h); flush(h, id);
  h.socket().receive({ type: 'asr.final', capture_id: id, text: 'old final' }); h.engine.cancel(reason);
  beforeSubmit.resolve(); await tick();
  assert.equal(h.finals.length, 0); assert.equal(h.records()[0].status, 'cancelled'); assert.equal(h.engine.active, false);
});
test('cancel rejects late packets and old socket results and releases every microphone track', async () => {
  const h = harness(); const id = await ready(h), socket = h.socket(), node = h.node();
  h.engine.cancel(); const messages = socket.sent.length;
  node.emit({ type: 'pcm', capture_id: id, buffer: new ArrayBuffer(3200) });
  socket.receive({ type: 'asr.final', capture_id: id, text: 'old final' }); await tick();
  assert.equal(socket.sent.length, messages); assert.equal(h.device.track.stopped, 1); assert.equal(h.finals.length, 0);
  assert.equal(JSON.parse(socket.sent.at(-1)).type, 'asr.cancel');
});
test('empty final and recognition error do not submit and release recording reservation', async () => {
  const h = harness(); const id = await ready(h); flush(h, id); h.socket().receive({ type: 'asr.final', capture_id: id, text: '  ' }); await tick();
  assert.equal(h.finals.length, 0); assert.equal(h.records()[0].status, 'error'); assert.equal(h.captures.at(-1).active, false);
  assert.match(h.errors.at(-1), /没有识别到/);
  const h2 = harness(); const id2 = await ready(h2); h2.socket().receive({ type: 'asr.error', capture_id: id2, message: 'provider unavailable' });
  assert.equal(h2.finals.length, 0); assert.equal(h2.device.track.stopped, 1); assert.equal(h2.captures.at(-1).active, false);
});
test('backpressure and malformed capture PCM fail closed without sending partial recognition as a query', async () => {
  const h = harness(); const id = await ready(h); h.socket().bufferedAmount = 256001;
  h.node().emit({ type: 'pcm', capture_id: id, buffer: new ArrayBuffer(3200) });
  assert.equal(h.engine.active, false); assert.equal(h.finals.length, 0); assert.equal(h.device.track.stopped, 1);
  const h2 = harness(); const id2 = await ready(h2); h2.node().emit({ type: 'pcm', capture_id: id2, buffer: new ArrayBuffer(6402) });
  assert.equal(h2.engine.active, false); assert.equal(h2.records()[0].pcm_bytes, 0); assert.equal(h2.device.track.stopped, 1);
});
test('ASR socket disconnect releases media and never submits the current preview', async () => {
  const h = harness(); const id = await ready(h);
  h.socket().receive({ type: 'asr.partial', capture_id: id, text: '把所有车窗关闭' }); h.socket().close();
  assert.equal(h.finals.length, 0); assert.equal(h.device.track.stopped, 1); assert.equal(h.captures.at(-1).active, false);
});
test('cancel while final waits for real Speech enable invalidates late enable as well as the query', async () => {
  sockets = []; contexts = []; nodes = []; const sent = [];
  const speech = new StreamingSpeech({ send: value => sent.push(value), phase: () => {}, metrics: () => {}, records: () => {}, error: () => {}, allowed: () => true });
  speech.connect('session'); sockets.at(-1).open(); await speech.setEnabled(true); await speech.setEnabled(false);
  const speechContext = contexts[0], resumed = deferred(); speechContext.resume = () => resumed.promise;
  const h = harness({ enableSpeech: () => speech.setEnabled(true), onSettled: reason => { if (reason !== 'final') void speech.setEnabled(false); } });
  const id = await ready(h); flush(h, id);
  const enabledBefore = sent.filter(value => value.type === 'audio.configure' && value.enabled).length;
  h.socket().receive({ type: 'asr.final', capture_id: id, text: 'late enable' }); await tick();
  h.engine.cancel('user_cancel'); resumed.resolve(); await tick(); await tick();
  assert.equal(sent.filter(value => value.type === 'audio.configure' && value.enabled).length, enabledBefore);
  assert.equal(h.finals.length, 0); assert.equal(h.engine.active, false); speech.dispose();
});


test('real continuous capture uploads during TTS and barge-in clears playback without closing the microphone', async () => {
  sockets=[]; nodes=[]; contexts=[];
  const device=media(), constraints=[], sent=[], errors=[], finals=[]; let capturing=false;
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async value=>{constraints.push(value);return device.stream;}}}});
  const speech=new StreamingSpeech({send:value=>sent.push(value),phase:()=>{},metrics:()=>{},records:()=>{},error:error=>errors.push(error),allowed:()=>!capturing});
  speech.connect('full-duplex'); const audio=sockets[0]; audio.open();
  let listen;
  listen=new HandsFreeRecognition({unlockSpeech:()=>speech.setEnabled(true),phase:()=>{},transcript:()=>{},records:()=>{},error:error=>errors.push(error),
    capture:(_id,active)=>{capturing=active;if(active)void speech.cancel('voice_activity');},
    final:async(text,id)=>{capturing=false;listen.markSubmitted(id,performance.now());finals.push(text);}});
  await listen.start('full-duplex');
  const uplink=sockets.at(-1), microphone=nodes.at(-1), player=nodes[0]; uplink.open();
  const listenId=JSON.parse(uplink.sent[0]).listen_id;
  const event=(type,more={})=>uplink.receive({type,listen_id:listenId,capture_id:'utterance',...more});
  event('voice.ready');
  assert.deepEqual(constraints[0].audio,{channelCount:1,sampleRate:16000,echoCancellation:true,noiseSuppression:true,autoGainControl:true});
  speech.submit('request','显式离线回复',performance.now(),[]);
  const playback='a'.repeat(32);
  audio.receive({type:'audio.started',turn_id:'reply',playback_id:playback,client_request_id:'request',sample_rate:24000,format:'pcm_s16le',channels:1});
  const pcm=new ArrayBuffer(36); new Uint8Array(pcm,0,32).set(new TextEncoder().encode(playback));new DataView(pcm).setInt16(32,100,true);
  audio.onmessage({data:pcm});
  assert.equal(player.sent.at(-1).type,'pcm');
  microphone.emit({type:'pcm',capture_id:listenId,buffer:new ArrayBuffer(1024)});
  assert.equal(uplink.sent.at(-1).byteLength,1024); assert.equal(device.track.stopped,0);
  event('voice.speech_start');
  assert.equal(player.sent.at(-1).type,'clear');
  assert.equal(listen.active,true); assert.equal(device.track.stopped,0);
  microphone.emit({type:'pcm',capture_id:listenId,buffer:new ArrayBuffer(1024)});
  assert.equal(uplink.sent.at(-1).byteLength,1024);
  event('voice.speech_end');event('voice.final',{text:'下一句'});await tick();
  assert.deepEqual(finals,['下一句']);assert.equal(listen.active,true);
  assert.equal(sent.some(value=>value.type==='audio.configure'&&!value.enabled),true); // Initial connection is unready.
  assert.equal(sent.filter(value=>value.type==='audio.configure').at(-1).enabled,true);
  assert.deepEqual(errors,[]);listen.dispose();speech.dispose();
});
