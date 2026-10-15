import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../public/pcm-player-worklet.js', import.meta.url), 'utf8');
function player(rate = 48000) {
  const events = [];
  let Processor;
  const sandbox = { sampleRate: rate, currentTime: 0, currentFrame: 0, AudioWorkletProcessor: class { constructor() { this.port = { postMessage: m => events.push(m) }; } }, registerProcessor: (_, cls) => { Processor = cls; } };
  vm.runInNewContext(source, sandbox);
  const instance = new Processor();
  const send = data => instance.port.onmessage({ data });
  const process = () => { const out = new Float32Array(128); instance.process([], [[out]]); sandbox.currentFrame += 128; sandbox.currentTime = sandbox.currentFrame / rate; return out; };
  return { events, send, process };
}
function pcm(samples) { const buffer = new ArrayBuffer(samples.length * 2); const view = new DataView(buffer); samples.forEach((v, i) => view.setInt16(i * 2, v, true)); return buffer; }
for (const rate of [44100, 48000]) test(`24k PCM resamples and drains naturally at ${rate}`, () => {
  const p = player(rate); p.send({ type: 'start', playback_id: 'a', sample_rate: 24000 });
  p.send({ type: 'pcm', playback_id: 'a', buffer: pcm(Array(2400).fill(16384)) });
  p.process(); assert.equal(p.events.filter(e => e.type === 'drained').length, 0);
  p.send({ type: 'end', playback_id: 'a' });
  for (let n = 0; n < 100; n++) p.process();
  const drain = p.events.find(e => e.type === 'drained'); assert.ok(drain);
  assert.ok(Math.abs(drain.rendered_output_samples - rate / 10) <= 1);
  assert.equal(p.events.filter(e => e.type === 'first-render').length, 1);
  assert.equal(p.events.filter(e => e.type === 'first-nonzero-render').length, 1);
});
test('waiting and empty PCM never count as rendered audio; silence is not nonzero', () => {
  const p = player(); p.send({ type: 'start', playback_id: 'a', sample_rate: 24000 });
  p.process(); p.send({ type: 'pcm', playback_id: 'a', buffer: pcm([]) }); p.process(); assert.equal(p.events.length, 0);
  p.send({ type: 'pcm', playback_id: 'a', buffer: pcm([0, 0, 0]) }); p.send({ type: 'end', playback_id: 'a' }); p.process();
  assert.ok(p.events.some(e => e.type === 'first-render')); assert.ok(!p.events.some(e => e.type === 'first-nonzero-render'));
});
test('clear silences queued old audio and old identities cannot reinsert it', () => {
  const p = player(); p.send({ type: 'start', playback_id: 'a', sample_rate: 24000 }); p.send({ type: 'pcm', playback_id: 'a', buffer: pcm(Array(1000).fill(12000)) });
  assert.ok(p.process().some(v => v !== 0)); p.send({ type: 'clear' });
  p.send({ type: 'pcm', playback_id: 'a', buffer: pcm(Array(1000).fill(12000)) }); p.send({ type: 'end', playback_id: 'a' }); assert.ok(p.process().every(v => v === 0));
  p.send({ type: 'start', playback_id: 'b', sample_rate: 24000 }); p.send({ type: 'pcm', playback_id: 'a', buffer: pcm([12000,12000]) }); p.process();
  assert.ok(!p.events.some(e => e.playback_id === 'b'));
});
test('clear reports rendered duration rather than queued audio and binds playback identity', () => {
  const p = player(48000);
  p.send({type:'start', playback_id:'old', sample_rate:24000});
  p.send({type:'pcm', playback_id:'old', buffer:pcm(Array(24000).fill(12000))});
  p.process(); p.process();
  p.send({type:'clear', playback_id:'wrong'});
  assert.ok(!p.events.some(e=>e.type==='stopped'));
  p.send({type:'clear', playback_id:'old'});
  const stopped=p.events.find(e=>e.type==='stopped');
  assert.equal(stopped.rendered_output_samples,256);
  assert.equal(stopped.played_ms,256/48);
  assert.ok(p.process().every(v=>v===0));
});
