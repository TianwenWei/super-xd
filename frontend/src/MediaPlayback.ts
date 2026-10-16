import type { CockpitCommand, CockpitReceipt, MediaState } from './cockpitTypes';

export function playableMediaUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value) return undefined;
  try {
    const url = new URL(value, 'http://cockpit.local');
    if (value.startsWith('/')) return url.origin === 'http://cockpit.local' && url.pathname.startsWith('/api/media/files/') ? url.pathname + url.search : undefined;
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

interface Callbacks { changed: (state: MediaState, version: number, event?: 'ended') => void; valid: (turn: string) => boolean }
/** HTML media is the execution authority. Intent and temporary audio focus are separate. */
export class MediaPlayback {
  private audio: HTMLMediaElement | null = null;
  private video: HTMLMediaElement | null = null;
  private element: HTMLMediaElement | null = null;
  private state: MediaState = { status: 'closed', position_seconds: 0, volume: 30, desired_status: 'closed' };
  private version = 0;
  private epoch = 0;
  private pending: AbortController | null = null;
  private pendingTurn = '';
  private focus = new Set<string>();
  private receipts = new Map<string, Promise<CockpitReceipt>>();
  private listeners: Array<() => void> = [];
  private lastProgress = 0;
  private disposed = false;
  private pauseEvents = new WeakMap<HTMLMediaElement, number>();
  constructor(private callbacks: Callbacks, private timeoutMs = 12000, private device?: { resolveMedia: (id: string, kind?: import('./cockpitTypes').MediaKind) => { source_url?: string; url?: string } | undefined; isDeviceBlob: (url: unknown) => boolean }) {}

  attach(audio: HTMLMediaElement, video: HTMLMediaElement) {
    this.audio = audio; this.video = video;
    for (const element of [audio, video]) for (const name of ['timeupdate', 'durationchange', 'playing', 'pause', 'ended', 'error', 'waiting', 'stalled']) {
      const listener = () => {
        if (this.element !== element || this.disposed) return;
        if (name === 'timeupdate' && performance.now() - this.lastProgress < 750) return;
        this.lastProgress = performance.now();
        if (name === 'pause') {
          const expected = this.pauseEvents.get(element) || 0;
          if (expected) this.pauseEvents.set(element, expected - 1);
          else { this.epoch++; this.pending?.abort(); this.state.desired_status = 'paused'; }
        }
        if (name === 'waiting' || name === 'stalled') this.state.buffering = true;
        if (name === 'playing') this.state.buffering = false;
        if (name === 'ended') this.state = { ...this.state, status: 'stopped', desired_status: 'stopped' };
        if (name === 'error') this.state = { ...this.state, status: 'stopped', desired_status: 'stopped', error: element.error?.message || '媒体加载或解码失败。' };
        this.publish(name === 'ended' ? 'ended' : undefined);
      };
      element.addEventListener(name, listener); this.listeners.push(() => element.removeEventListener(name, listener));
    }
  }
  snapshot() { return { ...this.state }; }
  get stateVersion() { return this.version; }
  private publish(event?: 'ended') {
    const element = this.element;
    if (element) {
      this.state.position_seconds = Number.isFinite(element.currentTime) ? element.currentTime : 0;
      this.state.duration_seconds = Number.isFinite(element.duration) && element.duration > 0 ? element.duration : undefined;
      if (this.state.status !== 'closed' && this.state.status !== 'stopped') this.state.status = element.paused ? 'paused' : 'playing';
    }
    this.callbacks.changed(this.snapshot(), this.version, event);
  }
  private event(element: HTMLMediaElement, name: string, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      const cleanup = () => { clearTimeout(timer); element.removeEventListener(name, completed); element.removeEventListener('error', failed); signal.removeEventListener('abort', aborted); };
      const completed = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error(element.error?.message || '媒体加载或解码失败。')); };
      const aborted = () => { cleanup(); reject(new DOMException('操作已被新指令替代。', 'AbortError')); };
      const timer = setTimeout(() => { cleanup(); reject(new Error('播放器未在时限内确认操作。')); }, this.timeoutMs);
      element.addEventListener(name, completed); element.addEventListener('error', failed); signal.addEventListener('abort', aborted, { once: true });
      if (signal.aborted) aborted();
    });
  }
  private async start(element: HTMLMediaElement, pending: AbortController) {
    if (!element.paused && !element.ended && element.readyState >= 3) return;
    const started = this.event(element, 'playing', pending.signal);
    // Observe both promises even if autoplay is rejected before the playing event.
    try {
      const play = element.play().then(() => {
        if (pending.signal.aborted && this.element === element && this.state.desired_status !== 'playing') this.pauseElement(element);
      });
      await Promise.all([play, started]);
    }
    catch (error) { pending.abort(); await started.catch(() => {}); throw error; }
  }
  private pauseElement(element: HTMLMediaElement | null) {
    if (!element || element.paused) return;
    this.pauseEvents.set(element, (this.pauseEvents.get(element) || 0) + 1); element.pause();
  }
  private async pause(element: HTMLMediaElement, signal: AbortSignal) {
    if (element.paused) return;
    const paused = this.event(element, 'pause', signal); this.pauseElement(element); await paused;
  }
  private async seek(element: HTMLMediaElement, seconds: number, signal: AbortSignal) {
    if (!Number.isFinite(element.duration)) throw new Error('当前直播或媒体没有可跳转的时长。');
    if (seconds < 0 || seconds > element.duration) throw new Error('跳转位置超出媒体时长。');
    if (Math.abs(element.currentTime - seconds) < 0.05) return;
    const completed = this.event(element, 'seeked', signal); element.currentTime = seconds; await completed;
  }
  async execute(command: CockpitCommand): Promise<CockpitReceipt> {
    if (this.receipts.has(command.command_id)) return this.receipts.get(command.command_id)!;
    const result = this.run(command); this.receipts.set(command.command_id, result);
    if (this.receipts.size > 200) this.receipts.delete(this.receipts.keys().next().value!);
    return result;
  }
  private async run(command: CockpitCommand): Promise<CockpitReceipt> {
    const receipt = (ok: boolean, error?: { code: string; message: string }): CockpitReceipt => ({ type: 'cockpit.receipt', kind: 'control', request_id: command.request_id, session_id: command.session_id, target_device: command.target_device, command_id: command.command_id, turn_id: command.turn_id, ok, state_version: this.version, state: this.snapshot(), ...(error ? { error } : {}) });
    const deadline = command.deadline_at ? Date.parse(command.deadline_at) : Infinity;
    if (!(deadline > Date.now())) return receipt(false, { code: 'device_timeout', message: '播放器指令已超过截止时间。' });
    if (this.disposed || !this.callbacks.valid(command.turn_id)) return receipt(false, { code: 'stale_command', message: '指令已过期，播放器未执行。' });
    if (command.expected_state_version !== this.version) return receipt(false, { code: 'state_conflict', message: '播放器状态已变化，请重新获取状态。' });
    this.pending?.abort();
    const epoch = ++this.epoch, pending = new AbortController(); this.pending = pending; this.pendingTurn = command.turn_id;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; pending.abort(); }, Math.min(this.timeoutMs, command.timeout_ms || this.timeoutMs, deadline - Date.now()));
    const current = () => !this.disposed && !timedOut && Date.now() < deadline && epoch === this.epoch && this.callbacks.valid(command.turn_id);
    try {
      const args = command.arguments;
      if (['play', 'next', 'previous'].includes(command.action)) {
        const rawSource = args.source_url || this.device?.resolveMedia(String(args.device_content_id || args.content_id || ''), args.kind)?.source_url || this.device?.resolveMedia(String(args.device_content_id || args.content_id || ''), args.kind)?.url;
        const source = this.device?.isDeviceBlob(rawSource) ? String(rawSource) : playableMediaUrl(rawSource);
        if (!source) throw new Error('未取得可播放的真实媒体地址。FM 硬件调谐不能作为网络音频播放。');
        if (!['music', 'video', 'radio'].includes(args.kind)) throw new Error('不支持此媒体类型。');
        const element = args.kind === 'video' ? this.video : this.audio;
        if (!element) throw new Error('播放器尚未就绪。');
        this.pauseElement(this.element);
        // load() cancels the prior resource and pending play promise before replacing it.
        this.pauseElement(element); element.src = source; element.load(); this.element = element;
        this.state = { content_id: args.content_id, kind: args.kind, title: args.title || args.content_id, status: 'paused', desired_status: 'playing', position_seconds: 0, volume: this.state.volume };
        element.volume = this.mustPause() ? 0 : this.effectiveVolume();
        await this.start(element, pending);
        if (!current()) throw new DOMException('操作已过期。', 'AbortError');
        this.state.status = 'playing';
        if (this.mustPause()) { await this.pause(element, pending.signal); this.state.status = 'paused'; }
      } else {
        const element = this.element;
        if (!element && !['exit', 'volume'].includes(command.action)) throw new Error('当前没有已选中的媒体。');
        if (command.action === 'pause') { this.state.desired_status = 'paused'; await this.pause(element!, pending.signal); this.state.status = 'paused'; }
        else if (command.action === 'resume') { this.state.desired_status = 'playing'; if (!this.mustPause()) await this.start(element!, pending); this.state.status = element!.paused ? 'paused' : 'playing'; }
        else if (command.action === 'stop') { this.state.desired_status = 'stopped'; await this.pause(element!, pending.signal); if (Number.isFinite(element!.duration)) await this.seek(element!, 0, pending.signal); this.state.status = 'stopped'; }
        else if (command.action === 'exit') { this.state.desired_status = 'closed'; this.pauseElement(this.element); if (this.element) { this.element.removeAttribute('src'); this.element.load(); } this.element = null; this.state = { content_id: null, kind: null, title: '', status: 'closed', desired_status: 'closed', position_seconds: 0, volume: this.state.volume }; }
        else if (command.action === 'seek') await this.seek(element!, Number(args.position_seconds), pending.signal);
        else if (command.action === 'volume') { const volume = Number(args.volume); if (!Number.isFinite(volume) || volume < 0 || volume > 100) throw new Error('播放器音量应为零到一百。'); this.state.volume = volume; if (element) element.volume = this.effectiveVolume(); }
        else throw new Error('播放器不支持此操作。');
      }
      if (!current()) throw new DOMException('操作已过期。', 'AbortError');
      this.version = command.expected_state_version + 1; this.state.error = undefined; this.applyFocus(); this.publish();
      return receipt(true);
    } catch (error) {
      pending.abort();
      if (epoch === this.epoch) {
        if (['play', 'next', 'previous', 'resume', 'stop'].includes(command.action) || pending.signal.aborted && !current()) {
          this.pauseElement(this.element); this.state.status = this.element ? 'paused' : 'closed'; this.state.desired_status = 'paused';
        }
        this.state.error = error instanceof Error ? error.message : String(error); this.publish();
      }
      return receipt(false, { code: timedOut ? 'device_timeout' : error instanceof Error && error.name === 'NotAllowedError' ? 'autoplay_blocked' : error instanceof Error && error.name === 'AbortError' ? 'stale_command' : 'playback_failed', message: error instanceof Error ? error.message : String(error) });
    } finally { clearTimeout(timer); if (this.pending === pending) { this.pending = null; this.pendingTurn = ''; this.applyFocus(); } }
  }
  private mustPause() { return this.focus.has('capture') || (this.state.kind === 'video' && this.focus.has('assistant')); }
  private effectiveVolume() { return this.state.volume / 100 * (this.focus.has('assistant') || this.focus.has('listening') ? 0.18 : 1); }
  setFocus(reason: 'assistant' | 'capture' | 'listening', active: boolean) {
    if (active === this.focus.has(reason)) return;
    if (active) this.focus.add(reason); else this.focus.delete(reason);
    this.applyFocus();
  }
  private applyFocus() {
    const element = this.element;
    if (!element) return;
    element.volume = this.effectiveVolume(); this.state.focus_reason = this.mustPause() ? 'speech' : this.focus.size ? 'ducking' : undefined; this.state.focus_source = [...this.focus].join(',') || undefined;
    if (this.mustPause()) { this.pauseElement(element); this.publish(); return; }
    if (this.state.desired_status === 'playing' && element.paused && !this.pending) {
      const epoch = this.epoch;
      // A later manual pause or source change invalidates this asynchronous resume.
      void element.play().then(() => {
        if (epoch !== this.epoch || this.element !== element || this.state.desired_status !== 'playing' || this.mustPause()) { if (this.element === element && this.state.desired_status !== 'playing') this.pauseElement(element); return; }
        this.state.status = 'playing'; this.publish();
      }).catch(error => { if (epoch === this.epoch) { this.state.error = `自动恢复失败：${error instanceof Error ? error.message : String(error)}`; this.state.status = 'paused'; this.publish(); } });
    } else this.publish();
  }
  invalidate(turn: string) {
    if (this.pending && this.pendingTurn === turn && !this.callbacks.valid(turn)) { this.pending.abort(); this.pauseElement(this.element); }
  }
  synchronizeVersion(version: number) { if (Number.isInteger(version) && version >= 0) this.version = version; }
  disconnect() { this.epoch++; this.pending?.abort(); this.pauseElement(this.element); this.state.desired_status = 'paused'; this.state.status = this.element ? 'paused' : 'closed'; this.publish(); }
  dispose() { this.disposed = true; this.disconnect(); this.listeners.forEach(remove => remove()); this.listeners = []; }
}
