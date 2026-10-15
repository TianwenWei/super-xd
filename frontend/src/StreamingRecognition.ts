import type { RecognitionMeasurement, RecognitionPhase } from './types';

interface Callbacks {
  capture: (id: string, active: boolean) => void;
  unlockSpeech: () => Promise<void>;
  phase: (phase: RecognitionPhase) => void;
  transcript: (text: string) => void;
  final: (text: string, id: string) => Promise<void>;
  settled: (reason: string) => void;
  records: (records: RecognitionMeasurement[]) => void;
  error: (message: string) => void;
}
interface Capture {
  id: string; generation: number; record: RecognitionMeasurement;
  stream?: MediaStream; context?: AudioContext; source?: MediaStreamAudioSourceNode;
  node?: AudioWorkletNode; gain?: GainNode; socket?: WebSocket;
  ready: boolean; stopRequested: boolean; stopSent: boolean; finalConsumed: boolean;
  timer?: ReturnType<typeof setTimeout>; flushTimer?: ReturnType<typeof setTimeout>;
}

/** One explicit click-to-start / click-to-finish capture; partials never submit. */
export class StreamingRecognition {
  private current: Capture | null = null;
  private generation = 0;
  private measurements: RecognitionMeasurement[] = [];
  constructor(private callbacks: Callbacks) {}
  isCurrent(id: string) { return this.current?.id === id; }
  get active() { return this.current !== null; }
  private valid(capture: Capture) { return this.current === capture && capture.generation === this.generation; }

  async start(sessionId: string) {
    this.cancel('replaced');
    const id = crypto.randomUUID();
    const capture: Capture = { id, generation: ++this.generation, ready: false, stopRequested: false, stopSent: false, finalConsumed: false,
      record: { capture_id: id, started_perf_ms: performance.now(), status: 'pending', pcm_bytes: 0, pcm_packets: 0 } };
    this.current = capture; this.measurements.push(capture.record);
    // The control socket orders the recording reservation before playback(false).
    this.callbacks.capture(id, true); this.callbacks.transcript(''); this.callbacks.phase('requesting'); this.publish();
    capture.timer = setTimeout(() => this.fail(capture, '录音等待超时，请重新开始。'), 60000);
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('浏览器不支持麦克风采集，请使用本机 localhost 或 HTTPS 页面。');
      // Invoke both resume paths in the original user gesture, before any await.
      const speechUnlock = this.callbacks.unlockSpeech().catch(() => { /* ASR may still work; final reports TTS failure. */ });
      const context = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      capture.context = context;
      const resumed = context.resume();
      // Attach a rejection handler immediately while the permission prompt is open.
      const initialized = Promise.all([resumed, context.audioWorklet.addModule('/pcm-capture-worklet.js')]);
      void initialized.catch(() => {});
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
      if (!this.valid(capture)) { stream.getTracks().forEach(track => track.stop()); return; }
      capture.stream = stream;
      stream.getTracks().forEach(track => track.addEventListener?.('ended', () => {
        if (this.valid(capture) && !capture.stopSent) this.fail(capture, '麦克风已断开，本次未发送。');
      }));
      await Promise.all([initialized, speechUnlock]);
      if (!this.valid(capture)) return;
      if (context.state !== 'running') throw new Error('麦克风音频尚未解锁，请重新点击开始说话。');
      capture.record.context_sample_rate = context.sampleRate;
      const node = new AudioWorkletNode(context, 'super-xd-pcm-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      capture.node = node; capture.source = context.createMediaStreamSource(stream); capture.gain = context.createGain();
      capture.gain.gain.value = 0; // Never monitor microphone audio through the speakers.
      node.port.onmessage = event => this.worklet(capture, event.data);
      node.onprocessorerror = () => this.fail(capture, '麦克风音频处理失败，请重新开始。');
      capture.source.connect(node); node.connect(capture.gain); capture.gain.connect(context.destination);
      this.callbacks.phase('connecting');
      const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${encodeURIComponent(sessionId)}/asr`);
      capture.socket = socket;
      socket.onopen = () => { if (this.valid(capture)) socket.send(JSON.stringify({ type: 'asr.start', capture_id: id, format: 'pcm_s16le', sample_rate: 16000, channels: 1 })); };
      socket.onmessage = event => {
        if (!this.valid(capture) || typeof event.data !== 'string') return;
        try { this.message(capture, JSON.parse(event.data)); }
        catch { this.fail(capture, '语音识别返回了无效消息。'); }
      };
      socket.onerror = () => this.fail(capture, '语音识别连接失败，请重试或使用文字输入。');
      socket.onclose = () => { if (this.valid(capture) && !capture.finalConsumed) this.fail(capture, '语音识别连接已断开，本次未发送。'); };
    } catch (error) {
      if (!this.valid(capture)) return;
      const name = error instanceof Error ? error.name : '';
      const message = name === 'NotAllowedError' ? '未获得麦克风权限，可重试或使用文字 / 豆包输入法。'
        : name === 'NotFoundError' ? '未找到麦克风，请连接麦克风或使用文字输入。'
        : name === 'NotReadableError' ? '无法读取麦克风，可能被其他应用占用。'
        : error instanceof Error ? error.message : String(error);
      this.fail(capture, message);
    }
  }

  stop() {
    const capture = this.current;
    if (!capture || !capture.ready || capture.stopRequested) return;
    capture.stopRequested = true; capture.record.stop_requested_perf_ms = performance.now();
    clearTimeout(capture.timer); this.callbacks.phase('recognizing');
    capture.timer = setTimeout(() => this.fail(capture, '最终识别等待超时，本次未发送。'), 15000);
    capture.flushTimer = setTimeout(() => this.fail(capture, '麦克风数据未能正常结束，本次未发送。'), 1500);
    capture.node?.port.postMessage({ type: 'flush', capture_id: capture.id }); this.publish();
  }

  private worklet(capture: Capture, message: Record<string, any>) {
    if (!this.valid(capture) || message.capture_id !== capture.id || !capture.ready || capture.stopSent) return;
    if (message.type === 'pcm' && message.buffer instanceof ArrayBuffer) {
      const buffer = message.buffer as ArrayBuffer;
      if (!buffer.byteLength || buffer.byteLength > 6400 || buffer.byteLength % 2) { this.fail(capture, '麦克风返回了无效 PCM 数据，本次未发送。'); return; }
      if (capture.socket?.readyState !== WebSocket.OPEN) { this.fail(capture, '语音识别连接不可用，本次未发送。'); return; }
      if (capture.socket.bufferedAmount > 256000) { this.fail(capture, '语音上传积压，本次已取消，请重试。'); return; }
      capture.record.first_pcm_sent_perf_ms ??= performance.now(); capture.record.pcm_bytes += buffer.byteLength; capture.record.pcm_packets++;
      capture.socket.send(buffer);
    } else if (message.type === 'flushed' && capture.stopRequested) {
      clearTimeout(capture.flushTimer); capture.stopSent = true;
      capture.record.stopped_perf_ms = performance.now(); this.releaseMedia(capture);
      if (capture.socket?.readyState === WebSocket.OPEN) capture.socket.send(JSON.stringify({ type: 'asr.stop', capture_id: capture.id }));
      else this.fail(capture, '语音识别连接不可用，本次未发送。');
      this.publish();
    }
  }

  private message(capture: Capture, message: Record<string, any>) {
    if (message.capture_id !== capture.id) return;
    if (message.type === 'asr.ready' && !capture.ready && !capture.stopRequested) {
      capture.ready = true; capture.record.ready_perf_ms = performance.now();
      capture.node?.port.postMessage({ type: 'start', capture_id: capture.id, sample_rate: 16000, frame_samples: 1600 });
      this.callbacks.phase('recording'); this.publish();
    } else if (message.type === 'asr.partial' && !capture.finalConsumed) {
      if (typeof message.text === 'string') { capture.record.first_partial_perf_ms ??= performance.now(); this.callbacks.transcript(message.text); }
    } else if (message.type === 'asr.final' && capture.stopSent && !capture.finalConsumed) {
      capture.finalConsumed = true; clearTimeout(capture.timer); this.releaseMedia(capture);
      const text = typeof message.text === 'string' ? message.text.trim() : '';
      capture.record.final_received_perf_ms = performance.now(); capture.record.final_text = text; capture.record.provider_metrics = message.metrics;
      if (!text) { this.fail(capture, '没有识别到有效语音，本次未发送。'); return; }
      if (text.length > 4000) { this.fail(capture, '识别内容超过4000字，本次未发送，请分段说。'); return; }
      this.callbacks.transcript(text); this.callbacks.phase('finalizing'); this.publish();
      capture.socket?.close();
      void this.callbacks.final(text, capture.id).then(() => {
        if (this.valid(capture)) this.finish(capture, capture.record.submitted_perf_ms === undefined ? 'cancelled' : 'submitted', 'final');
      }).catch(error => this.fail(capture, error instanceof Error ? error.message : String(error)));
    } else if (message.type === 'asr.error' && !capture.finalConsumed) this.fail(capture, typeof message.message === 'string' ? message.message : '语音识别失败，本次未发送。');
  }

  markSubmitted(id: string, submittedAt: number, requestId?: string) {
    const capture = this.current;
    if (capture?.id === id && capture.finalConsumed) { capture.record.submitted_perf_ms = submittedAt; capture.record.client_request_id = requestId; this.publish(); }
  }
  cancel(reason = 'user_cancel') { if (this.current) this.finish(this.current, 'cancelled', reason); }
  dispose() { this.cancel('disposed'); }
  private fail(capture: Capture, message: string) {
    if (!this.valid(capture)) return;
    capture.record.error = message; this.callbacks.error(message); this.finish(capture, 'error', 'error');
  }
  private releaseMedia(capture: Capture) {
    capture.node?.port.postMessage({ type: 'clear' });
    capture.stream?.getTracks().forEach(track => track.stop()); capture.stream = undefined;
    capture.source?.disconnect(); capture.node?.disconnect(); capture.gain?.disconnect();
    if (capture.context && capture.context.state !== 'closed') void capture.context.close().catch(() => {});
    capture.context = undefined;
  }
  private finish(capture: Capture, status: RecognitionMeasurement['status'], reason: string) {
    if (!this.valid(capture)) return;
    this.current = null; this.generation++; clearTimeout(capture.timer); clearTimeout(capture.flushTimer);
    this.releaseMedia(capture);
    if (capture.socket?.readyState === WebSocket.OPEN && !capture.finalConsumed) capture.socket.send(JSON.stringify({ type: 'asr.cancel', capture_id: capture.id }));
    capture.socket?.close(); capture.record.status = status; capture.record.reason = reason; capture.record.finished_perf_ms = performance.now();
    this.callbacks.capture(capture.id, false); this.callbacks.phase('idle'); this.callbacks.transcript(''); this.publish(); this.callbacks.settled(reason);
  }
  private publish() { this.callbacks.records(this.measurements.map(record => ({ ...record }))); }
}
