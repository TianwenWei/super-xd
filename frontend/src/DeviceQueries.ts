import type { CockpitCommand, CockpitReceipt, MediaItem, MediaKind } from './cockpitTypes';

export interface DeviceQueryContext { signal: AbortSignal; command: CockpitCommand }
export type DeviceQueryHandler = (arguments_: Record<string, unknown>, context: DeviceQueryContext) => Promise<Record<string, unknown>>;

/** Registered device SDKs perform queries; cloud providers never run through this registry. */
export class DeviceQueryRegistry {
  private handlers = new Map<string, DeviceQueryHandler>();
  private pending = new Map<string, { turn: string; controller: AbortController }>();
  private media = new Map<string, MediaItem>();
  private blobs = new Set<string>();
  private disposed = false;
  private receipts = new Map<string, Promise<CockpitReceipt>>();
  constructor(private valid: (turn: string) => boolean) {
    this.register('media', 'search', async args => {
      if (!['music', 'video', 'radio'].includes(String(args.kind)) || typeof args.query !== 'string' || !args.query.trim()) throw new Error('设备媒体搜索参数无效。');
      const words = args.query.toLocaleLowerCase().split(' ').filter(Boolean);
      const limit = typeof args.max_results === 'number' ? Math.max(1, Math.min(10, args.max_results)) : 5;
      const rows = [...this.media.values()].filter(item => item.kind === args.kind).map(item => {
        const text = `${item.title} ${item.artist || ''}`.toLocaleLowerCase();
        return { item, score: words.filter(word => text.includes(word)).length };
      }).filter(row => row.score > 0).sort((a, b) => b.score - a.score).slice(0, limit);
      return { ok: true, kind: 'media', provider: 'device_catalog', items: rows.map(({ item }) => this.publicItem(item)) };
    });
    this.register('media', 'resolve', async args => {
      const item = this.resolveMedia(String(args.content_id || ''), typeof args.kind === 'string' ? args.kind as MediaKind : undefined);
      if (!item) throw new Error('此内容不在本机媒体目录中。');
      // A device URL or blob capability stays on this device; the cloud sees only identity.
      return { ok: true, item: this.publicItem(item) };
    });
  }
  register(domain: string, action: string, handler: DeviceQueryHandler): () => void {
    const key = `${domain}:${action}`; this.handlers.set(key, handler);
    return () => { if (this.handlers.get(key) === handler) this.handlers.delete(key); };
  }
  private publicItem(item: MediaItem) { const { source_url: _source, url: _url, ...metadata } = item; return { ...metadata, provider: 'device_catalog' }; }
  setMediaCatalog(items: MediaItem[]) {
    for (const item of items) if (item.content_id && (item.source_url || item.url)) this.media.set(`${item.kind}:${item.content_id}`, { ...item });
  }
  importMediaFiles(files: File[]) {
    const added: MediaItem[] = [];
    for (const file of files) {
      const kind: MediaKind | null = file.type.startsWith('audio/') ? 'music' : file.type.startsWith('video/') ? 'video' : null;
      if (!kind) continue;
      const url = URL.createObjectURL(file); this.blobs.add(url);
      const item: MediaItem = { content_id: `device:${crypto.randomUUID()}`, title: file.name, kind, artist: '本机文件', source_url: url, provider: 'device_catalog' };
      this.media.set(`${item.kind}:${item.content_id}`, item); added.push(this.publicItem(item));
    }
    return added;
  }
  resolveMedia(id: string, kind?: MediaKind): MediaItem | undefined {
    const matching = kind ? [this.media.get(`${kind}:${id}`)].filter((item): item is MediaItem => !!item) : [...this.media.values()].filter(item => item.content_id === id);
    return matching.length === 1 ? { ...matching[0] } : undefined;
  }
  isDeviceBlob(url: unknown) { return typeof url === 'string' && this.blobs.has(url); }
  async execute(command: CockpitCommand): Promise<CockpitReceipt> {
    if (this.receipts.has(command.command_id)) return this.receipts.get(command.command_id)!;
    const receipt = this.run(command); this.receipts.set(command.command_id, receipt);
    if (this.receipts.size > 200) this.receipts.delete(this.receipts.keys().next().value!);
    return receipt;
  }
  private async run(command: CockpitCommand): Promise<CockpitReceipt> {
    const base = { type: 'cockpit.receipt' as const, kind: 'query' as const, command_id: command.command_id, request_id: command.request_id,
      session_id: command.session_id, target_device: command.target_device, turn_id: command.turn_id };
    const handler = this.handlers.get(`${command.domain}:${command.action}`);
    if (!handler) return { ...base, ok: false, error: { code: 'unsupported_capability', message: '这台设备没有注册此查询能力；未使用其他服务替代。' } };
    if (this.disposed || !this.valid(command.turn_id)) return { ...base, ok: false, error: { code: 'stale_command', message: '查询请求已过期。' } };
    const deadline = command.deadline_at ? Date.parse(command.deadline_at) - Date.now() : Infinity;
    const timeout = Math.min(command.timeout_ms || 12000, deadline);
    if (!(timeout > 0)) return { ...base, ok: false, error: { code: 'device_timeout', message: '设备查询已超过截止时间。' } };
    const controller = new AbortController(); this.pending.set(command.command_id, { turn: command.turn_id, controller });
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const aborted = new Promise<never>((_, reject) => {
        controller.signal.addEventListener('abort', () => reject(new DOMException('设备查询已取消或超时。', 'AbortError')), { once: true });
        timer = setTimeout(() => controller.abort(), timeout);
      });
      const result = await Promise.race([handler(command.arguments, { signal: controller.signal, command }), aborted]);
      if (controller.signal.aborted || this.disposed || !this.valid(command.turn_id)) throw new DOMException('查询结果已过期。', 'AbortError');
      return { ...base, ok: true, result };
    } catch (error) {
      return { ...base, ok: false, error: { code: !this.valid(command.turn_id) ? 'stale_command' : controller.signal.aborted ? 'device_timeout' : 'device_query_failed', message: error instanceof Error ? error.message : String(error) } };
    } finally { clearTimeout(timer); this.pending.delete(command.command_id); }
  }
  invalidate(turn: string) { for (const pending of this.pending.values()) if (pending.turn === turn) pending.controller.abort(); }
  disconnect() { for (const pending of this.pending.values()) pending.controller.abort(); this.pending.clear(); }
  dispose() { this.disposed = true; this.disconnect(); for (const blob of this.blobs) URL.revokeObjectURL(blob); this.blobs.clear(); }
}
