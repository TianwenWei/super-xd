import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChatMessage, TaskCardData, TraceEvent, TracePage, TraceTarget } from './types';
import { collapseWhitespace } from './text';

const EVENT_LABELS: Record<string, string> = {
  'model.started': '模型开始生成', 'model.completed': '模型生成完成', 'model.error': '模型请求失败',
  'tool.started': '开始调用工具', 'tool.completed': '工具返回结果', 'tool.error': '工具调用失败',
  'turn.started': '对话轮次开始', 'turn.completed': '对话轮次完成', 'turn.interrupted': '对话已打断',
  'model.cancelled': '模型生成已取消',
  'vehicle.dispatched': '车控命令下发', 'vehicle.receipt': '车辆实际执行回执',
  'task.created': '后台任务已登记', 'task.running': '后台任务执行中', 'task.update': '任务条件已更新',
  'task.reply': '任务收到补充回答', 'task.cancel': '任务取消', 'task.completed': '后台任务完成',
  'audio.text': '实际合成文本',
};
const STATUS_LABELS: Record<string, string> = { started: '进行中', running: '进行中', completed: '已完成', success: '成功', error: '失败', failed: '失败', interrupted: '已打断', cancelled: '已取消', stale: '迟到记录', queued: '排队中', received: '已接收', pending: '等待中' };
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? String(value);
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const rawText = (value: unknown) => typeof value === 'string' ? value : pretty(value);
const short = (value: string) => value.slice(0, 8);

function CopyButton({ value, label = '复制' }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    try { await navigator.clipboard.writeText(value); setCopied(true); setError(false); clearTimeout(timer.current); timer.current = setTimeout(() => setCopied(false), 1800); }
    catch { setError(true); }
  }
  return <span className="trace-copy-control"><button type="button" onClick={copy}>{copied ? '已复制' : label}</button>{error && <small role="status">复制失败，请选择原文复制。</small>}</span>;
}

function RawBlock({ label, value, missing }: { label: string; value: unknown; missing?: string }) {
  const present = value !== undefined;
  const text = present ? rawText(value) : '';
  return <section className="trace-raw-block"><div className="trace-block-heading"><h4>{label}</h4>{present && <CopyButton value={text} label={`复制${label}`}/>}</div>{present ? <pre tabIndex={0}>{text === '' ? '（字段为空）' : text}</pre> : <p className="trace-missing">{missing || '服务端未返回此字段。'}</p>}</section>;
}

const EventCard = memo(function EventCard({ event, onCall }: { event: TraceEvent; onCall: (id: string) => void }) {
  const data = object(event.data);
  const output = event.kind === 'model.cancelled' ? object(data.partial) : data;
  const message = object(output.message);
  const meta = object(output.meta);
  const isModelOutput = event.kind === 'model.completed' || (event.kind === 'model.cancelled' && !!data.partial);
  const toolName = data.name ?? data.tool_name;
  return <details className={`trace-event ${event.kind.includes('error') || event.status === 'error' ? 'trace-event-error' : ''}`}>
    <summary><span className="trace-event-marker"/><span className="trace-event-title"><strong>{EVENT_LABELS[event.kind] || event.kind}{typeof toolName === 'string' && <span> · {toolName}</span>}</strong><span className="trace-event-kind mono">{event.kind}</span></span><span className="trace-event-status">{STATUS_LABELS[event.status] || event.status || '状态未返回'}</span><span className="trace-event-time mono">#{event.trace_id}<time dateTime={event.created_at}>{event.created_at ? new Date(event.created_at).toLocaleTimeString('zh-CN', { hour12: false }) : '时间未返回'}</time></span><span className="trace-chevron">⌄</span></summary>
    <div className="trace-event-body">
      <dl className="trace-event-meta"><div><dt>turn</dt><dd>{event.turn_id || '未关联'}</dd></div><div><dt>task</dt><dd>{event.task_id || '未关联'}</dd></div>{event.revision != null && <div><dt>revision</dt><dd>{event.revision}</dd></div>}{event.step != null && <div><dt>step</dt><dd>{event.step}</dd></div>}{event.call_id && <div><dt>call_id</dt><dd><button onClick={() => onCall(event.call_id!)} title="查看同一调用的开始与结束">{event.call_id}</button></dd></div>}{typeof data.model_call_id === 'string' && <div><dt>model_call_id</dt><dd><button onClick={() => onCall(data.model_call_id as string)} title="查看关联模型调用与工具事件">{data.model_call_id}</button></dd></div>}</dl>
      {isModelOutput && <div className="trace-model-output"><div className="trace-model-caption"><strong>{event.kind === 'model.cancelled' ? '取消前返回的模型片段' : '模型原文'}</strong><span>{event.status === 'stale' ? '这是迟到记录，未作为当前回答。' : '以下为服务端保存的模型消息，最终用户反馈请看对话区。'}</span></div><dl className="trace-model-meta"><div><dt>model</dt><dd>{String(meta.model ?? '未返回')}</dd></div><div><dt>role</dt><dd>{String(message.role ?? '未返回')}</dd></div><div><dt>finish_reason</dt><dd>{String(meta.finish_reason ?? '未返回')}</dd></div><div><dt>elapsed_ms</dt><dd>{meta.elapsed_ms == null ? '未返回' : String(meta.elapsed_ms)}</dd></div><div><dt>first_delta_ms</dt><dd>{meta.first_delta_ms == null ? '未返回' : String(meta.first_delta_ms)}</dd></div></dl><RawBlock label="content" value={message.content} missing="content 字段未返回。"/>{message.content === null && <p className="trace-missing">content=null（空内容 / 仅工具调用）</p>}<RawBlock label="tool_calls" value={message.tool_calls}/><RawBlock label="reasoning_content" value={message.reasoning_content} missing="reasoning_content 未返回；此处不推测或补写模型思考。"/>{typeof message.reasoning_content === 'string' && message.reasoning_content && <p className="trace-missing">来源：该次模型响应 message.reasoning_content，原文保留。</p>}<RawBlock label="usage" value={meta.usage} missing="usage 未返回。"/>{meta.usage === null && <p className="trace-missing">usage=null；服务端未提供 token 统计。</p>}<RawBlock label="meta" value={output.meta}/></div>}
      {!isModelOutput && event.kind !== 'audio.text' && <RawBlock label="事件数据" value={event.data}/>}
      {event.kind === 'audio.text' && <section className="trace-model-output"><div className="trace-model-caption"><strong>实际送入语音服务的文本</strong><span>来源与字数取自服务端记录；完整报告与模型原文仍可独立回看。</span></div><dl className="trace-model-meta"><div><dt>source</dt><dd>{String(data.source ?? '未返回')}</dd></div><div><dt>original_chars</dt><dd>{String(data.original_chars ?? '未返回')}</dd></div><div><dt>spoken_chars</dt><dd>{String(data.spoken_chars ?? '未返回')}</dd></div></dl><RawBlock label="实际合成文本" value={data.text}/></section>}
      <details className="trace-full-json"><summary>展开完整事件 JSON</summary><RawBlock label="完整事件 JSON" value={event}/></details>
    </div>
  </details>;
});

export default function TraceDrawer({ open, sessionId, target, messages, tasks, onClose }: { open: boolean; sessionId: string; target: TraceTarget; messages: ChatMessage[]; tasks: TaskCardData[]; onClose: () => void }) {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [scope, setScope] = useState<TraceTarget>({ type: 'session' });
  const [view, setView] = useState<'all' | 'models'>('all');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [recordingError, setRecordingError] = useState('');
  const [updatedAt, setUpdatedAt] = useState('');
  const [follow, setFollow] = useState(false);
  const [callFilter, setCallFilter] = useState<string | null>(null);
  const selectCall = useCallback((id: string) => { setCallFilter(id); setView('all'); }, []);
  const afterId = useRef(0);
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setEvents([]); afterId.current = 0; setError(''); setRecordingError(''); setUpdatedAt(''); }, [sessionId]);
  useEffect(() => { setScope(target); setView('all'); }, [target]);
  useEffect(() => { setCallFilter(null); }, [scope]);

  useEffect(() => {
    if (!open || !sessionId) return;
    let disposed = false;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function sync() {
      setLoading(true);
      try {
        let hasMore = true;
        while (hasMore && !disposed) {
          const cursor = afterId.current;
          const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/traces?after_id=${cursor}`, { signal: abort.signal, cache: 'no-store' });
          if (!response.ok) throw new Error(`轨迹读取失败（${response.status}）`);
          const page = await response.json() as TracePage;
          if (disposed) return;
          if (!Array.isArray(page.events) || !Number.isSafeInteger(page.next_after_id) || page.events.some(event => !Number.isSafeInteger(event.trace_id))) throw new Error('轨迹接口返回格式不完整');
          if (page.recording_error) setRecordingError(typeof page.recording_error === 'string' ? page.recording_error : pretty(page.recording_error));
          else if (Object.hasOwn(page, 'recording_error')) setRecordingError('');
          if (page.has_more && page.next_after_id <= cursor) throw new Error('轨迹分页游标未推进，请稍后重试');
          if (page.events.length) setEvents(current => { const merged = new Map(current.map(event => [event.trace_id, event])); page.events.forEach(event => merged.set(event.trace_id, event)); return [...merged.values()].sort((a, b) => (Date.parse(a.created_at) - Date.parse(b.created_at) || a.trace_id - b.trace_id)); });
          afterId.current = Math.max(cursor, page.next_after_id);
          hasMore = page.has_more;
        }
        if (!disposed) { setError(''); setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false })); }
      } catch (e) { if (!disposed && !abort.signal.aborted) setError(e instanceof Error ? e.message : String(e)); }
      finally { if (!disposed) { setLoading(false); timer = setTimeout(sync, 1000); } }
    }
    void sync();
    return () => { disposed = true; abort.abort(); clearTimeout(timer); };
  }, [open, sessionId]);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
      if (event.key === 'Tab') {
        const focusable = [...(panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), select, input, summary, pre[tabindex]') || [])].filter(element => element.getClientRects().length > 0);
        if (!focusable?.length) return;
        const first = focusable[0]; const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); previous?.focus(); };
  }, [open, onClose]);

  const turnOptions = useMemo(() => {
    const ids = new Set(events.map(event => event.turn_id).filter((id): id is string => !!id));
    messages.forEach(message => { if (message.turnId) ids.add(message.turnId); });
    return [...ids].map(id => ({ id, label: messages.find(message => message.turnId === id && message.role === 'assistant')?.text || '对话轮次' }));
  }, [events, messages]);
  const taskOptions = useMemo(() => {
    const ids = new Set(events.map(event => event.task_id).filter((id): id is string => !!id));
    tasks.forEach(task => ids.add(task.id));
    return [...ids].map(id => ({ id, label: tasks.find(task => task.id === id)?.goal || '后台任务' }));
  }, [events, tasks]);
  const scoped = useMemo(() => events.filter(event => scope.type === 'session' || (scope.type === 'turn' ? event.turn_id : event.task_id) === scope.id), [events, scope]);
  const called = useMemo(() => callFilter ? scoped.filter(event => event.call_id === callFilter || object(event.data).model_call_id === callFilter) : scoped, [scoped, callFilter]);
  const visible = useMemo(() => view === 'models' ? called.filter(event => event.kind.startsWith('model.')) : called, [called, view]);
  const copyJson = useMemo(() => pretty(visible), [visible]);
  useEffect(() => { if (open && follow) endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }); }, [open, follow, visible.length, scope, view]);
  if (!open) return null;
  const selectValue = scope.type === 'session' ? 'session' : `${scope.type}:${scope.id}`;
  return <div className="trace-backdrop" onClick={onClose}><section className="trace-drawer" ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="trace-title" onClick={event => event.stopPropagation()}>
    <header className="trace-header"><div><span className="eyebrow">原始记录 · 持续同步</span><h2 id="trace-title">模型输出 / Agent 轨迹</h2><p>回看模型消息、工具调用与实际执行；最终用户反馈显示在对话区。</p></div><button ref={closeRef} className="icon-button trace-close" onClick={onClose} aria-label="关闭模型输出与 Agent 轨迹">×</button></header>
    <div className="trace-controls"><label className="trace-scope">查看范围<select value={selectValue} onChange={event => { const value = event.target.value; if (value === 'session') setScope({ type: 'session' }); else { const colon = value.indexOf(':'); setScope({ type: value.slice(0, colon) as 'turn' | 'task', id: value.slice(colon + 1) }); } }}><option value="session">全会话</option>{!!turnOptions.length && <optgroup label="对话轮次">{turnOptions.map(turn => <option key={turn.id} value={`turn:${turn.id}`}>{short(turn.id)} · {collapseWhitespace(turn.label).slice(0, 45)}</option>)}</optgroup>}{!!taskOptions.length && <optgroup label="后台任务">{taskOptions.map(task => <option key={task.id} value={`task:${task.id}`}>{short(task.id)} · {task.label.slice(0, 45)}</option>)}</optgroup>}</select></label><div className="trace-view-tabs" role="tablist" aria-label="轨迹展示方式"><button role="tab" aria-selected={view === 'all'} onClick={() => setView('all')}>完整轨迹 <span>{scoped.length}</span></button><button role="tab" aria-selected={view === 'models'} onClick={() => setView('models')}>模型原文 <span>{scoped.filter(event => event.kind === 'model.completed').length}</span></button></div></div>
    <div className="trace-toolbar"><span className="trace-sync" role="status"><i className={error ? 'has-error' : ''}/>{error ? '同步失败，自动重试' : loading ? '正在同步' : updatedAt ? `已同步 ${updatedAt}` : '等待读取'}</span><label><input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)}/>跟随最新</label><CopyButton value={copyJson} label="复制当前记录 JSON"/></div>
    {error && <div className="trace-error" role="alert">{error}。已读取记录仍可查看。</div>}
    {recordingError && <div className="trace-error" role="alert"><strong>轨迹写入失败，记录可能不完整。</strong><pre>{recordingError}</pre></div>}
    {callFilter && <div className="trace-call-filter"><span>关联调用 <code>{callFilter}</code></span><button onClick={() => setCallFilter(null)}>显示全部调用 ×</button></div>}
    <div className="trace-events">{visible.length ? visible.map(event => <EventCard key={event.trace_id} event={event} onCall={selectCall}/>) : <div className="trace-empty"><strong>{loading ? '正在读取轨迹…' : '这个范围还没有轨迹'}</strong><p>{view === 'models' ? '模型请求开始或结束后，原始消息会显示在这里。' : '对话、车控与后台任务的事件会实时追加。可切换“全会话”查看其他记录。'}</p></div>}<div ref={endRef}/></div>
    <footer className="trace-footer"><span className="mono">SESSION {short(sessionId) || '—'} · 缓存 {events.length} 条</span><span>关闭保留缓存，下次增量读取</span></footer>
  </section></div>;
}
