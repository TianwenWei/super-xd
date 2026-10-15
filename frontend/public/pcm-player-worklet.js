/* Genuine s16le PCM only. Empty output while waiting is never counted as a render. */
class PCMPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.reset(null);
    this.port.onmessage = ({ data }) => {
      if (data.type === 'start') {
        this.reset(data.playback_id);
        this.inputRate = data.sample_rate;
      } else if (data.type === 'clear') {
        if (data.playback_id && data.playback_id !== this.id) return;
        this.port.postMessage({ type: 'stopped', playback_id: this.id,
          rendered_output_samples: this.rendered, played_ms: this.rendered * 1000 / sampleRate });
        this.reset(null);
      } else if (data.playback_id === this.id && data.type === 'pcm') {
        const bytes = new DataView(data.buffer);
        const samples = new Float32Array(bytes.byteLength / 2);
        for (let index = 0; index < samples.length; index++) samples[index] = bytes.getInt16(index * 2, true) / 32768;
        if (samples.length) this.chunks.push(samples);
      } else if (data.playback_id === this.id && data.type === 'end') {
        this.ended = true;
      }
    };
  }
  reset(id) {
    this.id = id;
    this.inputRate = 24000;
    this.chunks = [];
    this.chunkOffset = 0;
    this.phase = 0;
    this.current = null;
    this.next = null;
    this.ended = false;
    this.drained = false;
    this.firstRender = false;
    this.firstNonzero = false;
    this.rendered = 0;
  }
  read() {
    if (!this.chunks.length) return null;
    const value = this.chunks[0][this.chunkOffset++];
    if (this.chunkOffset >= this.chunks[0].length) { this.chunks.shift(); this.chunkOffset = 0; }
    return value;
  }
  notify(type, offset) {
    this.port.postMessage({ type, playback_id: this.id, context_time: currentTime + offset / sampleRate, current_frame: currentFrame + offset, rendered_output_samples: this.rendered });
  }
  process(inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    output.fill(0);
    if (!this.id || this.drained) return true;
    const ratio = this.inputRate / sampleRate;
    for (let index = 0; index < output.length; index++) {
      if (this.current === null) this.current = this.read();
      if (this.current === null) {
        if (this.ended) { this.drained = true; this.notify('drained', index); }
        break;
      }
      if (this.next === null) this.next = this.read();
      // Retain a boundary sample while waiting for the next real PCM packet.
      if (this.next === null && !this.ended) break;
      const value = this.current + ((this.next ?? this.current) - this.current) * this.phase;
      output[index] = value;
      this.rendered++;
      if (!this.firstRender) { this.firstRender = true; this.notify('first-render', index); }
      if (!this.firstNonzero && value !== 0) { this.firstNonzero = true; this.notify('first-nonzero-render', index); }
      this.phase += ratio;
      while (this.phase >= 1) { this.phase -= 1; this.current = this.next; this.next = this.read(); }
    }
    return true;
  }
}
registerProcessor('super-xd-pcm-player', PCMPlayer);
