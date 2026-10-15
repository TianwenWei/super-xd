import type { AudioMeasurement, SpeechMetrics, TaskCardData } from './types';

interface AudioStarted { type: string; turn_id: string; playback_id: string; client_request_id?: string; sample_rate: number; format: string; channels: number; [key: string]: unknown }
interface Callbacks { send: (value: Record<string, unknown>) => void; phase: (value: 'idle' | 'synthesizing' | 'playing') => void; metrics: (turn: string, metrics: SpeechMetrics) => void; records: (records: AudioMeasurement[]) => void; error: (message: string) => void; allowed: (turn: string) => boolean }
interface Query { id: string; text: string; submitted: number; epoch: number; superseded: boolean; tasks: Array<{id: string; status: string}> }
interface Playback { turn: string; id: string; record: AudioMeasurement; inputEnded: boolean }

export class StreamingSpeech {
  private context: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private initializing: Promise<void> | null = null;
  private socket: WebSocket | null = null;
  private retry: ReturnType<typeof setTimeout> | undefined;
  private recordTimer: ReturnType<typeof setTimeout> | undefined;
  private session = '';
  private closed = false;
  private enableGeneration = 0;
  private enabled = false;
  private current: Playback | null = null;
  private focusedTurn = '';
  private stopping = new Map<string, { turn: string; done: () => void; promise: Promise<void> }>();
  private rejected = new Set<string>();
  private rejectedTurns = new Set<string>();
  private queries = new Map<string, Query>();
  private turns = new Map<string, string>();
  private measurements = new Map<string, AudioMeasurement>();
  private provider = 'volcengine-speech-tts';
  private voice = 'xiaohe2.0';
  private connectionDiagnostic: Record<string, unknown> | undefined;
  private enableRequestedPerf: number | undefined;
  private enableReadyPerf: number | undefined;
  private decoder = new TextDecoder('ascii');
  constructor(private callbacks: Callbacks) {}

  configure(provider: string, voice: string) { this.provider = provider; this.voice = voice; }
  async unlock() {
    if (!this.context) this.context = new AudioContext({ latencyHint: 'interactive' });
    const context = this.context;
    // resume() is invoked in the original click/Enter gesture, before any await.
    const resumed = context.resume();
    if (!this.initializing) this.initializing = context.audioWorklet.addModule('/pcm-player-worklet.js').then(() => {
      const node = new AudioWorkletNode(context, 'super-xd-pcm-player', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
      node.port.onmessage = event => this.onWorklet(event.data);
      node.onprocessorerror = () => { this.callbacks.error('浏览器音频处理器发生错误，请刷新后重新启用朗读。'); this.finish('error', 'audio_processor_error'); };
      node.connect(context.destination); this.node = node;
    }).catch(error => { this.initializing = null; throw error; });
    await Promise.all([resumed, this.initializing]);
    if (context.state !== 'running') throw new Error('浏览器音频尚未解锁，请再次点击火山朗读。');
  }
  async setEnabled(value: boolean): Promise<boolean> {
    const generation = ++this.enableGeneration;
    if (!value) { this.enabled = false; this.cancel('disabled'); this.callbacks.send({ type: 'audio.configure', enabled: false }); return false; }
    this.enableRequestedPerf = performance.now(); this.enableReadyPerf = undefined;
    await this.unlock();
    await this.waitForSocket();
    if (generation !== this.enableGeneration || this.closed) return false;
    this.enabled = true;
    this.enableReadyPerf = performance.now();
    this.callbacks.send({ type: 'audio.configure', enabled: true });
    return true;
  }
  private async waitForSocket() {
    const socket = this.socket;
    if (!socket) throw new Error('音频连接尚未就绪，请稍后重新启用朗读。');
    if (socket.readyState === WebSocket.OPEN) return;
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => { clearTimeout(timer); socket.removeEventListener('open', opened); socket.removeEventListener('close', failed); socket.removeEventListener('error', failed); };
      const opened = () => { cleanup(); if (this.socket === socket && !this.closed) resolve(); else reject(new Error('音频连接已切换。')); };
      const failed = () => { cleanup(); reject(new Error('音频连接未能建立，请重新启用朗读。')); };
      const timer = setTimeout(failed, 8000);
      socket.addEventListener('open', opened); socket.addEventListener('close', failed); socket.addEventListener('error', failed);
    });
  }
  connect(session: string) {
    if (this.session === session && (this.socket?.readyState === WebSocket.OPEN || this.socket?.readyState === WebSocket.CONNECTING)) return;
    this.disconnect(); this.session = session; this.closed = false; this.connectionDiagnostic = undefined;
    this.openSocket();
  }
  private openSocket() {
    if (this.closed || !this.session) return;
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${encodeURIComponent(this.session)}/audio`);
    socket.binaryType = 'arraybuffer'; this.socket = socket;
    socket.onopen = () => { if (this.socket === socket && !this.closed) this.callbacks.send({ type: 'audio.configure', enabled: this.enabled }); };
    socket.onmessage = event => {
      if (this.closed || this.socket !== socket) return;
      if (typeof event.data === 'string') { try { this.metadata(JSON.parse(event.data)); } catch { this.callbacks.error('音频服务返回了无效消息。'); } }
      else if (event.data instanceof ArrayBuffer) this.pcm(event.data);
    };
    socket.onclose = event => {
      if (this.socket !== socket || this.closed) return;
      this.cancel('audio_disconnected'); this.socket = null;
      if (event.code !== 4404) this.retry = setTimeout(() => this.openSocket(), 2000);
    };
    socket.onerror = () => { if (this.enabled && !this.closed) this.callbacks.error('火山音频连接中断，正在重新连接。'); };
  }
  disconnect() {
    this.closed = true; clearTimeout(this.retry); this.cancel('disconnected');
    const socket = this.socket; this.socket = null; socket?.close();
  }
  dispose() { this.disconnect(); clearTimeout(this.recordTimer); this.node?.disconnect(); void this.context?.close(); }

  submit(id: string, text: string, submitted: number, tasks: TaskCardData[]) {
    this.queries.forEach(query => { query.superseded = true; });
    const stopped = this.cancel('new_input');
    this.measurements.forEach(record => { if (record.status === 'pending' && !record.playback_id) { record.status = 'cancelled'; record.error = 'new_input'; record.finished_perf_ms = submitted; } });
    const query = { id, text, submitted, epoch: performance.timeOrigin + submitted, superseded: false, tasks: tasks.map(task => ({ id: task.id, status: task.status })) };
    this.queries.set(id, query);
    if (this.enabled) this.measurements.set(`request:${id}`, { client_request_id: id, query: text, turn_id: '', submit_perf_ms: submitted, submit_epoch_ms: query.epoch, pcm_bytes: 0, pcm_packets: 0, pcm_samples: 0, status: 'pending', vehicle_receipts: [], task_ids: [], background_tasks: query.tasks, diagnostics: { connection_at_submit: this.connectionDiagnostic ?? null, audio_enable_requested_perf_ms: this.enableRequestedPerf, audio_enable_ready_perf_ms: this.enableReadyPerf } });
    this.emitRecords();
    if (this.enabled) void this.unlock().catch(error => this.callbacks.error(String(error)));
    return stopped;
  }
  turnStarted(turn: string, requestId?: string) {
    if (this.current && this.current.turn !== turn) this.cancel('new_turn');
    this.focusedTurn = turn;
    if (requestId) {
      this.turns.set(turn, requestId);
      const pending = this.measurements.get(`request:${requestId}`);
      if (pending) { pending.turn_id = turn; this.measurements.set(turn, pending); this.measurements.delete(`request:${requestId}`); this.emitRecords(); }
    }
  }
  acceptsTurn(turn: string, requestId?: string) { return !this.rejectedTurns.has(turn) && !(requestId && this.queries.get(requestId)?.superseded); }
  interrupted(turn: string) { this.rejectedTurns.add(turn); if (this.current?.turn === turn) this.cancel('turn_interrupted'); }
  annotate(turn: string, kind: 'vehicle' | 'task', data: Record<string, unknown>) {
    const record = this.measurements.get(turn); if (!record) return;
    if (kind === 'vehicle') record.vehicle_receipts.push({ ...data, received_perf_ms: performance.now() });
    else if (typeof data.task_id === 'string' && !record.task_ids.includes(data.task_id)) { record.task_ids.push(data.task_id); record.task_registered_perf_ms = performance.now(); }
    this.emitRecords();
  }
  metadata(message: Record<string, any>) {
    if (message.type === 'audio.connection') { this.connectionDiagnostic = { ...message, received_perf_ms: performance.now() }; return; }
    const id = message.playback_id as string; const turn = message.turn_id as string;
    if (message.type === 'audio.started') {
      const started = message as AudioStarted;
      const query = started.client_request_id ? this.queries.get(started.client_request_id) : undefined;
      if (!this.enabled || this.closed || !this.node || this.rejected.has(id) || this.rejectedTurns.has(turn) || !this.callbacks.allowed(turn) || query?.superseded) return;
      if (!/^[a-fA-F0-9]{32}$/.test(id) || started.format !== 'pcm_s16le' || started.sample_rate !== 24000 || started.channels !== 1) { this.callbacks.error('音频格式不符合 PCM s16le / mono / 24kHz 约定。'); return; }
      if (this.current?.id === id) return;
      if (this.current) this.cancel('replaced');
      if (started.client_request_id) this.turnStarted(turn, started.client_request_id);
      const record = this.measurements.get(turn) || { client_request_id: started.client_request_id, query: query?.text, turn_id: turn, submit_perf_ms: query?.submitted, submit_epoch_ms: query?.epoch, pcm_bytes: 0, pcm_packets: 0, pcm_samples: 0, status: 'pending' as const, vehicle_receipts: [], task_ids: [], background_tasks: query?.tasks || [] };
      Object.assign(record, { playback_id: id, audio_started_perf_ms: performance.now(), context_state: this.context?.state, context_sample_rate: this.context?.sampleRate, input_sample_rate: started.sample_rate });
      record.diagnostics = { ...record.diagnostics, connection_at_audio_start: this.connectionDiagnostic ?? null };
      this.measurements.set(turn, record); this.current = { turn, id, record, inputEnded: false };
      this.node.port.postMessage({ type: 'start', playback_id: id, sample_rate: started.sample_rate });
      this.callbacks.phase('synthesizing'); this.callbacks.send({ type: 'playback', speaking: true, turn_id: turn, playback_id: id }); this.publish(record, true);
    } else if (message.type === 'audio.metrics') {
      const record = this.measurements.get(turn);
      if (record && (!id || record.playback_id === id)) { record.diagnostics = { ...record.diagnostics, ...(message.metrics || message.data || message) }; this.publish(record, false); }
    } else if (this.current && this.current.id === id && this.current.turn === turn) {
      if (message.type === 'audio.ended') { this.current.inputEnded = true; this.current.record.audio_ended_perf_ms = performance.now(); this.node?.port.postMessage({ type: 'end', playback_id: id }); this.publish(this.current.record, true); }
      else if (message.type === 'audio.cancelled') this.finish('cancelled', message.reason || 'server_cancelled');
      else if (message.type === 'audio.error') { const detail = message.message || message.error || '服务端语音生成失败'; this.callbacks.error(`火山朗读未完成：${typeof detail === 'string' ? detail : JSON.stringify(detail)}`); this.finish('error', typeof detail === 'string' ? detail : JSON.stringify(detail)); }
    }
  }
  private pcm(buffer: ArrayBuffer) {
    if (buffer.byteLength <= 32 || (buffer.byteLength - 32) % 2 !== 0) return;
    const id = this.decoder.decode(new Uint8Array(buffer, 0, 32));
    const playback = this.current;
    if (!playback || playback.id !== id || playback.inputEnded || this.rejected.has(id) || this.rejectedTurns.has(playback.turn) || !this.enabled || !this.callbacks.allowed(playback.turn) || !this.node) return;
    const now = performance.now(); const record = playback.record; const first = record.first_pcm_perf_ms === undefined;
    if (first) record.first_pcm_perf_ms = now;
    let firstNonzero = false;
    if (record.first_nonzero_pcm_perf_ms === undefined) { const samples = new DataView(buffer, 32); for (let offset = 0; offset < samples.byteLength; offset += 2) { if (samples.getInt16(offset, true) !== 0) { record.first_nonzero_pcm_perf_ms = now; firstNonzero = true; break; } } }
    record.pcm_bytes += buffer.byteLength - 32; record.pcm_packets++; record.pcm_samples += (buffer.byteLength - 32) / 2;
    const pcm = buffer.slice(32);
    this.node.port.postMessage({ type: 'pcm', playback_id: id, buffer: pcm }, [pcm]);
    if (first) record.first_play_scheduled_perf_ms = performance.now();
    if (first || firstNonzero) this.publish(record, true); else this.queueRecords();
  }
  private onWorklet(message: Record<string, any>) {
    if (message.type === 'stopped') {
      const pending = this.stopping.get(message.playback_id);
      if (pending) {
        this.callbacks.send({ type: 'audio.played', turn_id: pending.turn,
          playback_id: message.playback_id, played_ms: message.played_ms });
        pending.done(); this.stopping.delete(message.playback_id);
      }
      return;
    }
    const playback = this.current;
    if (!playback || message.playback_id !== playback.id || this.rejected.has(playback.id)) return;
    const now = performance.now(); const record = playback.record;
    if (message.type === 'first-render') { record.first_render_observed_perf_ms = now; record.render_context_time = message.context_time; record.render_current_frame = message.current_frame; record.context_state = this.context?.state; record.status = 'playing'; this.callbacks.phase('playing'); this.publish(record, true); }
    else if (message.type === 'first-nonzero-render') { record.first_nonzero_render_observed_perf_ms = now; record.nonzero_render_context_time = message.context_time; record.nonzero_render_current_frame = message.current_frame; this.publish(record, true); }
    else if (message.type === 'drained' && playback.inputEnded) { this.callbacks.send({type: 'audio.played', turn_id: playback.turn, playback_id: playback.id, played_ms: message.rendered_output_samples * 1000 / (this.context?.sampleRate || 48000)}); record.drained_perf_ms = now; record.rendered_output_samples = message.rendered_output_samples; this.finish(record.first_nonzero_pcm_perf_ms === undefined ? 'error' : 'ended', record.first_nonzero_pcm_perf_ms === undefined ? 'no_nonzero_pcm_received' : undefined); }
  }
  cancel(reason = 'user_stop'): Promise<void> {
    if (this.current) {
      const playback = this.current;
      let done!: () => void;
      const promise = new Promise<void>(resolve => { done = resolve; });
      this.stopping.set(playback.id, {turn: playback.turn, done, promise});
      // Clear and sample the render counter on the worklet's own thread.
      this.callbacks.send({ type: 'audio.cancel', turn_id: playback.turn, playback_id: playback.id });
      this.finish('cancelled', reason);
      setTimeout(() => { const pending = this.stopping.get(playback.id); if (pending) { pending.done(); this.stopping.delete(playback.id); } }, 150);
      return promise;
    }
    if (this.focusedTurn) { this.rejectedTurns.add(this.focusedTurn); this.callbacks.send({ type: 'audio.cancel', turn_id: this.focusedTurn }); }
    this.callbacks.phase('idle');
    return Promise.all([...this.stopping.values()].map(item => item.promise)).then(() => undefined);
  }
  private finish(status: 'ended' | 'cancelled' | 'error', reason?: string) {
    const playback = this.current; if (!playback) return;
    this.rejected.add(playback.id); if (status !== 'ended') this.rejectedTurns.add(playback.turn);
    playback.record.status = status; playback.record.finished_perf_ms = performance.now(); if (reason) playback.record.error = reason;
    if (status !== 'ended') this.node?.port.postMessage({ type: 'clear', playback_id: playback.id });
    this.callbacks.send({ type: 'playback', speaking: false, turn_id: playback.turn, playback_id: playback.id });
    this.current = null; this.callbacks.phase('idle'); this.publish(playback.record, true);
  }
  getMetrics(turn: string): SpeechMetrics | undefined { const record = this.measurements.get(turn); return record ? this.metrics(record) : undefined; }
  private metrics(record: AudioMeasurement): SpeechMetrics {
    const difference = (time?: number) => record.submit_perf_ms !== undefined && time !== undefined ? time - record.submit_perf_ms : undefined;
    return { provider: this.provider, voice: this.voice, query_to_first_frame_ms: difference(record.first_pcm_perf_ms), query_to_first_nonzero_frame_ms: difference(record.first_nonzero_pcm_perf_ms), query_to_first_play_scheduled_ms: difference(record.first_play_scheduled_perf_ms), query_to_first_render_observed_ms: difference(record.first_render_observed_perf_ms), query_to_first_nonzero_render_observed_ms: difference(record.first_nonzero_render_observed_perf_ms), first_frame_from_audio_started_ms: record.first_pcm_perf_ms !== undefined && record.audio_started_perf_ms !== undefined ? record.first_pcm_perf_ms - record.audio_started_perf_ms : undefined, pcm_packets: record.pcm_packets, pcm_bytes: record.pcm_bytes, diagnostics: record.diagnostics };
  }
  private publish(record: AudioMeasurement, report: boolean) {
    const metrics = this.metrics(record); this.callbacks.metrics(record.turn_id, metrics); this.emitRecords();
    if (report && record.playback_id) this.callbacks.send({ type: 'audio.client_metrics', turn_id: record.turn_id, playback_id: record.playback_id, metrics: { ...record, ...metrics } });
  }
  private emitRecords() { this.callbacks.records([...this.measurements.values()].map(record => ({ ...record, vehicle_receipts: [...record.vehicle_receipts], task_ids: [...record.task_ids], background_tasks: [...record.background_tasks] }))); }
  private queueRecords() { if (!this.recordTimer) this.recordTimer = setTimeout(() => { this.recordTimer = undefined; this.emitRecords(); }, 250); }
}
