import type { RecognitionMeasurement, RecognitionPhase } from './types';

interface Callbacks {
  capture: (id: string, active: boolean) => void;
  unlockSpeech: () => Promise<void>;
  phase: (phase: RecognitionPhase) => void;
  transcript: (text: string) => void;
  final: (text: string, id: string) => Promise<void>;
  records: (records: RecognitionMeasurement[]) => void;
  error: (message: string) => void;
}
interface Listener {
  id: string; stream?: MediaStream; context?: AudioContext; source?: MediaStreamAudioSourceNode;
  node?: AudioWorkletNode; gain?: GainNode; socket?: WebSocket; ready: boolean;
  timer?: ReturnType<typeof setTimeout>; capture?: RecognitionMeasurement; finalConsumed: boolean;
}

/** The microphone remains live during ASR finalization and assistant playback. */
export class HandsFreeRecognition {
  private current: Listener | null = null;
  private measurements: RecognitionMeasurement[] = [];
  constructor(private callbacks: Callbacks) {}
  get active() { return this.current !== null; }
  isCurrent(id: string) { return this.current?.capture?.capture_id === id; }
  private valid(listener: Listener) { return this.current === listener; }

  async start(session: string) {
    this.cancel();
    const listener: Listener = { id: crypto.randomUUID(), ready: false, finalConsumed: false };
    this.current = listener;
    this.callbacks.transcript(''); this.callbacks.phase('requesting');
    listener.timer = setTimeout(() => this.fail(listener, '免按键对话启动超时，请重试。'), 60000);
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('麦克风需要本机 localhost 或 HTTPS 页面。');
      const context = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      listener.context = context;
      // Both audio unlocks run in the initial user gesture.
      const initialized = Promise.all([context.resume(), context.audioWorklet.addModule('/pcm-capture-worklet.js'), this.callbacks.unlockSpeech()]);
      void initialized.catch(() => {});
      const stream = await navigator.mediaDevices.getUserMedia({ audio: {
        channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      }, video: false });
      if (!this.valid(listener)) { stream.getTracks().forEach(track => track.stop()); return; }
      listener.stream = stream;
      for (const track of stream.getTracks()) track.addEventListener?.('ended', () => this.fail(listener, '麦克风已断开，监听已停止。'));
      await initialized;
      if (!this.valid(listener)) return;
      if (context.state !== 'running') throw new Error('音频未解锁，请重新开启免按键对话。');
      const node = new AudioWorkletNode(context, 'super-xd-pcm-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      listener.node = node; listener.source = context.createMediaStreamSource(stream); listener.gain = context.createGain();
      listener.gain.gain.value = 0;
      node.port.onmessage = ({ data }) => {
        if (!this.valid(listener) || !listener.ready || data.capture_id !== listener.id || data.type !== 'pcm') return;
        if (!(data.buffer instanceof ArrayBuffer) || data.buffer.byteLength !== 1024) { this.fail(listener, '免按键音频帧无效，监听已停止。'); return; }
        if (listener.socket?.readyState !== WebSocket.OPEN || listener.socket.bufferedAmount > 32000) { this.fail(listener, '语音上传积压或连接中断，监听已停止。'); return; }
        const record = listener.capture;
        if (record?.status === 'pending' && record.stopped_perf_ms === undefined && !listener.finalConsumed) {
          record.first_pcm_sent_perf_ms ??= performance.now(); record.pcm_bytes += data.buffer.byteLength; record.pcm_packets++;
        }
        listener.socket.send(data.buffer);
      };
      node.onprocessorerror = () => this.fail(listener, '麦克风音频处理失败，监听已停止。');
      listener.source.connect(node); node.connect(listener.gain); listener.gain.connect(context.destination);
      this.callbacks.phase('connecting');
      const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${encodeURIComponent(session)}/listen`);
      listener.socket = socket;
      socket.onopen = () => { if (this.valid(listener)) socket.send(JSON.stringify({ type: 'voice.start', listen_id: listener.id, format: 'pcm_s16le', sample_rate: 16000, channels: 1 })); };
      socket.onmessage = event => {
        if (!this.valid(listener) || typeof event.data !== 'string') return;
        try { this.message(listener, JSON.parse(event.data)); }
        catch { this.fail(listener, '免按键语音服务返回了无效消息。'); }
      };
      socket.onerror = () => this.fail(listener, '免按键语音连接失败，监听已停止。');
      socket.onclose = () => this.fail(listener, '免按键语音连接已断开，监听已停止。');
    } catch (error) {
      const name = error instanceof Error ? error.name : '';
      this.fail(listener, name === 'NotAllowedError' ? '未获得麦克风权限，无法开启免按键对话。'
        : error instanceof Error ? error.message : String(error));
    }
  }

  private message(listener: Listener, message: Record<string, any>) {
    if (message.listen_id !== listener.id) return;
    if (message.type === 'voice.ready') {
      if (listener.ready) return;
      listener.ready = true; clearTimeout(listener.timer);
      listener.node?.port.postMessage({ type: 'start', capture_id: listener.id, sample_rate: 16000, frame_samples: 512 });
      this.callbacks.phase('listening');
    } else if (message.type === 'voice.speech_start' && listener.ready && typeof message.capture_id === 'string') {
      if (listener.capture?.capture_id === message.capture_id) return;
      this.releaseCapture(listener, 'superseded');
      const now = performance.now();
      const record: RecognitionMeasurement = { capture_id: message.capture_id, started_perf_ms: now, ready_perf_ms: now,
        context_sample_rate: listener.context?.sampleRate, pcm_bytes: 0, pcm_packets: 0, status: 'pending',
        mode: 'handsfree', vad_inference_ms: message.vad_ms };
      listener.capture = record; listener.finalConsumed = false; this.measurements.push(record);
      this.callbacks.capture(record.capture_id, true); this.callbacks.transcript(''); this.callbacks.phase('recording'); this.publish();
    } else if (message.type === 'voice.error' && (!message.capture_id || this.isCurrent(message.capture_id))) {
      this.fail(listener, typeof message.message === 'string' ? message.message : '免按键语音失败，监听已停止。');
    } else if (listener.capture && message.capture_id === listener.capture.capture_id && !listener.finalConsumed) {
      const record = listener.capture;
      if (message.type === 'voice.partial' && typeof message.text === 'string') {
        record.first_partial_perf_ms ??= performance.now(); this.callbacks.transcript(message.text);
      } else if (message.type === 'voice.speech_end') {
        record.stopped_perf_ms = performance.now(); this.callbacks.phase('recognizing');
      } else if (message.type === 'voice.final' && record.stopped_perf_ms !== undefined) {
        listener.finalConsumed = true;
        const text = typeof message.text === 'string' ? message.text.trim() : '';
        record.final_received_perf_ms = performance.now(); record.final_text = text; record.provider_metrics = message.metrics;
        if (!text || text.length > 4000) {
          record.status = 'error'; record.error = !text ? '没有识别到有效语音。' : '识别内容超过4000字。';
          this.callbacks.error(record.error); this.releaseCapture(listener, 'empty_final'); this.callbacks.phase('listening'); return;
        }
        this.callbacks.transcript(text); this.callbacks.phase('finalizing'); this.publish();
        void this.callbacks.final(text, record.capture_id).then(() => {
          if (!this.valid(listener) || listener.capture !== record) return;
          this.releaseCapture(listener, 'final'); this.callbacks.transcript(''); this.callbacks.phase('listening');
        }).catch(error => {
          if (listener.capture === record) this.fail(listener, error instanceof Error ? error.message : String(error));
        });
      }
    }
  }

  markSubmitted(id: string, at: number, requestId?: string) {
    if (!this.isCurrent(id) || !this.current?.finalConsumed) return;
    const record = this.current.capture!;
    record.submitted_perf_ms = at; record.client_request_id = requestId; record.status = 'submitted'; this.publish();
  }
  private releaseCapture(listener: Listener, reason: string) {
    const record = listener.capture;
    if (!record) return;
    listener.capture = undefined;
    if (record.status === 'pending') record.status = 'cancelled';
    record.finished_perf_ms = performance.now(); record.reason = reason;
    this.callbacks.capture(record.capture_id, false); this.publish();
  }
  cancel(reason = 'user_cancel') {
    const listener = this.current;
    if (!listener) return;
    this.current = null; clearTimeout(listener.timer);
    this.releaseCapture(listener, reason);
    listener.node?.port.postMessage({ type: 'clear' }); listener.node?.disconnect();
    listener.source?.disconnect(); listener.gain?.disconnect();
    listener.stream?.getTracks().forEach(track => track.stop()); void listener.context?.close();
    if (listener.socket?.readyState === WebSocket.OPEN) listener.socket.send(JSON.stringify({ type: 'voice.stop', listen_id: listener.id }));
    listener.socket?.close(); this.callbacks.phase('idle'); this.callbacks.transcript('');
  }
  dispose() { this.cancel('disposed'); }
  private fail(listener: Listener, message: string) {
    if (!this.valid(listener)) return;
    if (listener.capture) { listener.capture.status = 'error'; listener.capture.error = message; }
    this.callbacks.error(message); this.cancel('error');
  }
  private publish() { this.callbacks.records(this.measurements.map(record => ({ ...record }))); }
}
