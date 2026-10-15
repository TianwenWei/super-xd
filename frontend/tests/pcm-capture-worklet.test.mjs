// Isolated capture tests use synthetic inputs; they never access a microphone or live ASR.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../public/pcm-capture-worklet.js', import.meta.url), 'utf8');
function capture(rate = 48000) {
  const events = [], transfers = [];
  let Processor, processorName;
  const sandbox = {
    sampleRate: rate,
    AudioWorkletProcessor: class {
      constructor() { this.port = { postMessage: (message, transfer) => { events.push(message); transfers.push(transfer); } }; }
    },
    registerProcessor: (name, cls) => { processorName = name; Processor = cls; },
  };
  vm.runInNewContext(source, sandbox);
  assert.equal(processorName, 'super-xd-pcm-capture');
  const instance = new Processor();
  const send = data => instance.port.onmessage({ data });
  const process = channels => {
    const length = Math.max(128, ...channels.map(channel => channel.length));
    const outputs = [[new Float32Array(length).fill(1), new Float32Array(length).fill(-1)], [new Float32Array(length).fill(0.5)]];
    assert.equal(instance.process([channels], outputs), true);
    for (const bus of outputs) for (const channel of bus) assert.ok(channel.every(sample => sample === 0), 'all output buses must remain silent');
  };
  const feed = (channels, blocks = [128]) => {
    let offset = 0, block = 0;
    while (offset < channels[0].length) {
      const end = Math.min(offset + blocks[block++ % blocks.length], channels[0].length);
      process(channels.map(channel => channel.subarray(offset, end)));
      offset = end;
    }
  };
  const pcm = () => events.filter(event => event.type === 'pcm');
  const samples = () => pcm().flatMap(event => {
    const view = new DataView(event.buffer);
    return Array.from({ length: view.byteLength / 2 }, (_, index) => view.getInt16(index * 2, true));
  });
  return { events, transfers, send, process, feed, pcm, samples };
}
const start = (p, captureId = 'capture-a', frameSamples = 1600) => p.send({ type: 'start', capture_id: captureId, sample_rate: 16000, frame_samples: frameSamples });

for (const rate of [44100, 48000]) {
  test(`${rate}Hz input has exact 16k duration and 100ms frames across block boundaries`, () => {
    const p = capture(rate);
    start(p);
    p.feed([new Float32Array(rate).fill(0.5)], [128, 73, 257, 1]);
    assert.equal(p.pcm().length, 10);
    for (const event of p.pcm()) {
      assert.equal(event.capture_id, 'capture-a');
      assert.equal(event.buffer.byteLength, 3200);
    }
    assert.equal(p.samples().length, 16000);
    assert.ok(p.samples().every(sample => sample === 16384));
    p.send({ type: 'flush', capture_id: 'capture-a' });
    assert.equal(p.pcm().length, 10, 'an exact duration must not add a tail sample');
    assert.equal(p.events.at(-1).type, 'flushed');
    for (let index = 0; index < p.events.length; index++) {
      const event = p.events[index];
      if (event.type === 'pcm') assert.equal(p.transfers[index][0], event.buffer);
    }
  });

  test(`${rate}Hz resampling retains the same phase and waveform for different block sizes`, () => {
    const input = Float32Array.from({ length: rate + 131 }, (_, index) => Math.sin(index * 0.071) * 0.8);
    const whole = capture(rate), split = capture(rate);
    start(whole); start(split);
    whole.process([input]);
    split.feed([input], [1, 127, 61, 256]);
    whole.send({ type: 'flush', capture_id: 'capture-a' });
    split.send({ type: 'flush', capture_id: 'capture-a' });
    assert.deepEqual(split.samples(), whole.samples());
    assert.equal(split.samples().length, Math.floor(input.length * 16000 / rate));
  });
}

test('multiple channels mix to mono, samples saturate, and byte order is little endian', () => {
  const p = capture(16000);
  start(p, 'capture-a', 8);
  p.process([
    new Float32Array([1, -1, 0.5, -0.5, 4, -4, 0, 1]),
    new Float32Array([0, 0, -0.5, 0.5, 0, 0, 1, -1]),
  ]);
  assert.deepEqual(p.samples(), [16384, -16384, 0, 0, 32767, -32768, 16384, 0]);
  assert.deepEqual([...new Uint8Array(p.pcm()[0].buffer)].slice(0, 4), [0x00, 0x40, 0x00, 0xc0]);
});

test('box averaging attenuates a 48k input alternating above the output Nyquist limit', () => {
  const p = capture(48000);
  start(p, 'capture-a', 2);
  p.process([new Float32Array([1, -1, 1, -1, 1, -1])]);
  assert.deepEqual(p.samples(), [10922, -10923]);
});

test('flush emits only the remaining complete samples, then stops capture', () => {
  const p = capture(48000);
  start(p);
  p.process([new Float32Array(4800 + 300 + 1).fill(0.25)]);
  assert.equal(p.pcm().length, 1);
  p.send({ type: 'flush', capture_id: 'capture-a' });
  assert.equal(p.pcm().length, 2);
  assert.equal(p.pcm()[1].buffer.byteLength, 200);
  assert.equal(p.samples().length, 1700);
  assert.equal(p.events.at(-1).type, 'flushed');
  assert.equal(p.events.at(-1).capture_id, 'capture-a');
  const count = p.events.length;
  p.process([new Float32Array(4800).fill(1)]);
  p.send({ type: 'flush', capture_id: 'capture-a' });
  assert.equal(p.events.length, count, 'flush is terminal and acknowledged once');
});

test('clear drops partial frames and phase, and stale IDs cannot flush or stop a new capture', () => {
  const p = capture();
  start(p);
  p.process([new Float32Array(4799).fill(1)]);
  assert.equal(p.pcm().length, 0);
  p.send({ type: 'clear' });
  p.send({ type: 'flush', capture_id: 'capture-a' });
  p.process([new Float32Array(4800).fill(1)]);
  assert.equal(p.events.length, 0);
  start(p, 'capture-b');
  p.send({ type: 'flush', capture_id: 'capture-a' });
  p.send({ type: 'stop', capture_id: 'capture-a' });
  p.process([new Float32Array(4800).fill(-0.5)]);
  assert.equal(p.pcm().length, 1);
  assert.equal(p.pcm()[0].capture_id, 'capture-b');
  assert.ok(p.samples().every(sample => sample === -16384));
});

test('stop discards pending audio and no-input processing never fabricates PCM', () => {
  const p = capture();
  p.process([new Float32Array(4800).fill(1)]);
  start(p);
  p.process([]);
  assert.equal(p.events.length, 0);
  p.process([new Float32Array(300).fill(1)]);
  p.send({ type: 'stop', capture_id: 'capture-a' });
  p.process([new Float32Array(4800).fill(1)]);
  p.send({ type: 'flush', capture_id: 'capture-a' });
  assert.equal(p.events.length, 0);
});

test('frame sizes never exceed 6400 bytes and invalid start configuration does not capture', () => {
  const p = capture(16000);
  start(p, 'capture-a', 3200);
  p.process([new Float32Array(6400).fill(0.2)]);
  assert.equal(p.pcm().length, 2);
  assert.ok(p.pcm().every(event => event.buffer.byteLength === 6400));
  const count = p.events.length;
  for (const frameSamples of [3201, 0, -1, 1.5]) {
    start(p, 'invalid', frameSamples);
    p.process([new Float32Array(6400).fill(1)]);
    p.send({ type: 'flush', capture_id: 'invalid' });
  }
  p.send({ type: 'start', capture_id: 'invalid-rate', sample_rate: 24000, frame_samples: 1600 });
  p.process([new Float32Array(6400).fill(1)]);
  assert.equal(p.events.length, count);
});
