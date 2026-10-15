/* Microphone input only: mono 16kHz s16le frames, with a silent output bus. */
class PCMCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.reset();
    this.port.onmessage = ({ data }) => {
      if (!data) return;
      if (data.type === 'clear') {
        this.reset();
      } else if (data.type === 'start') {
        this.reset();
        const frameSamples = data.frame_samples ?? 1600;
        if (typeof data.capture_id !== 'string' || !data.capture_id || data.sample_rate !== 16000 ||
            !Number.isInteger(frameSamples) || frameSamples < 1 || frameSamples > 3200 ||
            !Number.isFinite(sampleRate) || sampleRate <= 0) return;
        this.id = data.capture_id;
        this.frameSamples = frameSamples;
        this.allocateFrame();
      } else if (this.id && data.capture_id === this.id && data.type === 'flush') {
        const id = this.id;
        this.emitFrame();
        // A fraction of one output sample is discarded rather than padded with invented audio.
        this.reset();
        this.port.postMessage({ type: 'flushed', capture_id: id });
      } else if (this.id && data.capture_id === this.id && data.type === 'stop') {
        this.reset();
      }
    };
  }
  reset() {
    this.id = null;
    this.frameSamples = 1600;
    this.frameBuffer = null;
    this.frameView = null;
    this.frameLength = 0;
    this.weight = 0;
    this.area = 0;
  }
  allocateFrame() {
    this.frameBuffer = new ArrayBuffer(this.frameSamples * 2);
    this.frameView = new DataView(this.frameBuffer);
    this.frameLength = 0;
  }
  append(value) {
    const clipped = Math.max(-1, Math.min(1, value));
    const integer = Math.round(clipped * (clipped < 0 ? 32768 : 32767));
    this.frameView.setInt16(this.frameLength * 2, integer, true);
    this.frameLength++;
    if (this.frameLength === this.frameSamples) this.emitFrame();
  }
  emitFrame() {
    if (!this.id || !this.frameLength) return;
    const buffer = this.frameLength === this.frameSamples ? this.frameBuffer : this.frameBuffer.slice(0, this.frameLength * 2);
    this.port.postMessage({ type: 'pcm', capture_id: this.id, buffer }, [buffer]);
    this.allocateFrame();
  }
  process(inputs, outputs) {
    // This node may be connected to destination to keep processing alive; never monitor the mic.
    for (const bus of outputs) for (const channel of bus) channel.fill(0);
    const channels = inputs[0];
    if (!this.id || !channels?.length) return true;
    let frames = 0;
    for (const channel of channels) frames = Math.max(frames, channel.length);
    for (let index = 0; index < frames; index++) {
      let mono = 0;
      for (const channel of channels) {
        const sample = channel[index];
        if (Number.isFinite(sample)) mono += sample;
      }
      mono /= channels.length;
      // Integer time units retain phase exactly across render blocks at 44.1kHz and 48kHz.
      // Integrating each output interval is a box low-pass before downsampling, not decimation.
      let remaining = 16000;
      while (remaining > 0) {
        const taken = Math.min(remaining, sampleRate - this.weight);
        this.area += mono * taken;
        this.weight += taken;
        remaining -= taken;
        if (this.weight === sampleRate) {
          this.append(this.area / sampleRate);
          this.weight = 0;
          this.area = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('super-xd-pcm-capture', PCMCapture);
