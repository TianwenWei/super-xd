import { useCallback, useEffect, useRef, useState } from 'react';
import type { SpeakerSeat, ChatMessage, Config, PersonaConfig, Receipt, SessionSnapshot, AudioMeasurement, RecognitionMeasurement, RecognitionPhase, TaskCardData, TraceTarget } from './types';
import TraceDrawer from './TraceDrawer';
import LookupResults from './LookupResults';
import VehicleSimulatorControls from './VehicleSimulatorControls';
import VehicleCalls from './VehicleCalls';
import bydLogo from './assets/byd-logo.svg';
import { applyVehicleReceipt, bindVehicleCommand, liveVehicleCall, mergeVehicleCalls, snapshotVehicleCalls } from './vehicleCallViews';
import { StreamingSpeech } from './StreamingSpeech';
import { StreamingRecognition } from './StreamingRecognition';
import { HandsFreeRecognition } from './HandsFreeRecognition';
import AudioMeasurements from './AudioMeasurements';
import { QUERY_TO_NONZERO_PCM_TARGET_LABEL, pcmTargetClass } from './performanceTarget';
import './recognition.css';
import './vehicle-changes.css';
import { useVehicleChanges } from './useVehicleChanges';
import { vehiclePathKey } from './vehicleChanges';
import type { VehicleChange } from './vehicleChanges';

const STATUS: Record<string, string> = { queued: '排队中', running: '正在处理', waiting_clarification: '等你补充', cancelling: '正在取消', completed: '已完成', failed: '未完成', cancelled: '已取消' };
const LABELS: Record<string, string> = { _meta: '车辆信息', speed_kph: '车速 km/h', power: '电源', enabled: '开启', temperature: '温度', temperature_c: '温度 °C', fan_speed: '风量', mode: '模式', recirculation: '内循环', ac: '制冷', humidity: '湿度', air_quality: '空气质量', driver: '主驾', passenger: '副驾', front_left: '左前', front_right: '右前', rear_left: '左后', rear_right: '右后', position: '位置', heating: '加热', ventilation: '通风', massage: '按摩', height: '高度', lumbar: '腰托', recline: '靠背', locked: '已锁定', folded: '已折叠', brightness: '亮度', color: '颜色', interior: '车内灯', headlights: '前照灯', ambient: '氛围灯', volume: '音量', playing: '播放中', source: '音源', track: '曲目', destination: '目的地', waypoints: '途经点', preferences: '偏好', open: '开启', level: '级别', mute: '静音', balance: '声道平衡', bass: '低音', treble: '高音', repeat: '循环', shuffle: '随机播放', auto: '自动', front: '前部', rear: '后部', speed: '速度', rain_sensing: '雨量感应', defrost: '除霜', auto_lock: '自动落锁', child_lock: '儿童锁', stopped: '已停止', route: '模拟行程', active: '启用' };
Object.assign(LABELS, { location:'当前位置', location_country:'国家/地区', gear:'档位', call_active:'通话中', timezone:'车辆时区', simulated:'模拟状态', charging:'充电中', temperatures:'各温区 °C', front_passenger:'副驾', airflow_mode:'送风模式', face:'面部', feet:'脚部', face_feet:'面部与脚部', windshield:'前挡风', front_defrost:'前挡风除雾', rear_defrost:'后挡风除雾', air_purifier:'空气净化', fragrance:'香氛强度', humidity_target:'目标湿度 %', temperature_unit:'温度单位', celsius:'摄氏度', fahrenheit:'华氏度', sync_zones:'温区同步', rear_power:'后排空调', pm25_ug_m3:'PM2.5 μg/m³', co2_ppm:'CO₂ ppm', positions:'各位置', memories:'记忆档位', steering_heating:'方向盘加热', steering_position:'方向盘位置', reach:'前后', easy_entry:'便捷上下车', fore_aft:'前后位置', backrest:'靠背角度', cushion_length:'坐垫长度', side_support:'侧翼支撑', headrest:'头枕', occupied:'座位有人', off:'关闭', low:'低', medium:'中', high:'高', pulse:'脉冲', wave:'波浪', anti_pinch:'防夹保护', sunroof:'天窗', sunshade:'遮阳帘', rain_close:'下雨自动关窗', left:'左侧', right:'右侧', horizontal:'水平角度', vertical:'垂直角度', reverse_tilt:'倒车自动下翻', ambient_color:'氛围灯颜色', warm:'暖色', cool:'冷色', blue:'蓝色', white:'白色', red:'红色', ambient_brightness:'氛围灯亮度', headlight_mode:'前照灯模式', high_beam:'远光灯', front_fog:'前雾灯', rear_fog:'后雾灯', reading:'阅读灯', follow_me_home_seconds:'伴我回家 秒', hazard:'危险警示灯', doors:'各车门', walk_away_lock:'离车自动落锁', charge_port_locked:'充电口锁定', moving:'正在移动', last_action:'最近动作', idle:'空闲', interval_seconds:'间隔 秒', rain_sensitivity:'雨量灵敏度', washer_fluid_pct:'清洗液余量 %', wash_count:'清洗次数', last_wash:'最近清洗', playback:'播放状态', paused:'已暂停', bluetooth:'蓝牙', radio:'收音机', usb:'USB', title:'曲目名称', track_index:'曲目序号', radio_frequency_mhz:'收音频率 MHz', fader:'前后平衡', equalizer:'均衡器', flat:'标准', home:'家', work:'公司', avoid_tolls:'避开收费', avoid_highways:'避开高速', prefer_fastest:'优先最快', notice:'模拟说明' });
const EXAMPLES = ['有点热，空调调到 22 度', '把四个车窗都关上', '今天过得怎么样？', '查一下巴黎周末适合带孩子去的室内活动，给我来源'];
const initialPersona: PersonaConfig = { id: 'warm', name: '温暖自然', tone: '温暖、自然、体贴', sentence_length: 'short', catchphrase_frequency: 'low', voice: 'xiaohe2.0' };

function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    send: <><path d="m5 12 14-7-6 14-2-6-6-1Z"/><path d="m11 13 8-8"/></>,
    car: <><path d="m5 10 2-5h10l2 5M4 10h16v8H4zM6 18v2m12-2v2M7 14h2m6 0h2"/></>,
    sound: <><path d="M4 9h4l5-4v14l-5-4H4zM17 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/></>,
    mic: <><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M6 10v2a6 6 0 0 0 12 0v-2M12 18v3m-3 0h6"/></>,
    stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
    spark: <><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/></>,
    clock: <><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></>,
    arrow: <path d="m9 5 7 7-7 7"/>,
    check: <path d="m5 12 4 4L19 6"/>,
    settings: <><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/></>,
    close: <path d="m6 6 12 12M18 6 6 18"/>,
    link: <><path d="m10 14 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 0 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/></>,
    trace: <><path d="M8 5h12M8 12h12M8 19h12M4 5h.01M4 12h.01M4 19h.01"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.spark}</svg>;
}

function sourceName(url: string): string { try { return new URL(url).hostname; } catch { return url; } }

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未设置';
  if (typeof value === 'boolean') return value ? '开' : '关';
  if (Array.isArray(value)) return value.length ? value.map(formatValue).join('、') : '无';
  if (typeof value === 'object') return Object.entries(value as Record<string, unknown>).map(([k, v]) => `${LABELS[k] || k}: ${formatValue(v)}`).join(' · ');
  return LABELS[String(value)] || String(value);
}

function StateRows({ value, depth = 0, path = [], changes = new Map() }: { value: unknown; depth?: number; path?: string[]; changes?: Map<string, VehicleChange> }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return <span>{formatValue(value)}</span>;
  return <div className={`state-rows depth-${depth}`}>{Object.entries(value as Record<string, unknown>).map(([key, item]) => {
    const itemPath = [...path, key], change = changes.get(vehiclePathKey(itemPath));
    return typeof item === 'object' && item !== null && !Array.isArray(item)
      ? <div className="state-nest" key={key}><span className="state-label">{LABELS[key] || key.replaceAll('_', ' ')}</span><StateRows value={item} path={itemPath} changes={changes} depth={depth + 1}/></div>
      : <div className={`state-row ${change ? 'state-row-changed' : ''}`} key={`${key}:${change?.sequence || 0}`} data-state-path={vehiclePathKey(itemPath)}><span>{LABELS[key] || key.replaceAll('_', ' ')}</span><strong className={typeof item === 'number' ? 'state-value-numeric' : undefined}>{change && <><span className="state-previous">{formatValue(change.before)}</span><span className="state-change-arrow" aria-hidden="true">→</span><span className="sr-only">变为</span></>}<span className={change ? 'state-current' : undefined}>{formatValue(item)}</span></strong></div>;
  })}</div>;
}

function TaskCard({ task, send, onTrace }: { task: TaskCardData; send: (value: Record<string, unknown>) => void; onTrace: () => void }) {
  const [text, setText] = useState('');
  const [editing, setEditing] = useState(false);
  const [now, setNow] = useState(Date.now());
  const terminal = ['completed', 'failed', 'cancelled'].includes(task.status);
  useEffect(() => {
    if (terminal || task.status === 'waiting_clarification') return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [terminal, task.status]);
  const clock = task.deadline_paused_at ? Date.parse(task.deadline_paused_at) : now;
  const elapsed = task.deadline_at ? Math.min(60, Math.max(0, Math.floor(60 - (Date.parse(task.deadline_at) - clock) / 1000))) : null;
  const partial = !!task.result && typeof task.result === 'object' && 'partial' in task.result && task.result.partial === true;
  const report = task.result && typeof task.result === 'object' ? task.result as Record<string, unknown> : null;
  const summary = task.status === 'completed' && typeof report?.spoken_summary === 'string' ? report.spoken_summary : task.summary;
  const action = task.status === 'waiting_clarification' ? 'task.reply' : 'task.update';
  function submit() { if (!text.trim()) return; send({ type: action, task_id: task.id, text: text.trim(), expected_revision: task.revision }); setText(''); setEditing(false); }
  return <article className={`task-card task-${task.status}`}>
    <div className="task-top"><span className="task-status"><i/>{task.status === 'completed' && partial ? '阶段结果' : STATUS[task.status] || task.status}</span><span className="muted mono">v{task.revision} · {task.steps || 0} 步</span></div>
    <h3>{task.goal}</h3>
    {!terminal && elapsed !== null && <p className="muted">{task.status === 'waiting_clarification' ? '等待你补充，计时暂停' : `已用 ${elapsed} 秒 · 最长 60 秒`}</p>}
    {summary && <p>{summary}</p>}
    {task.question && <div className="question">{task.question}</div>}
    {task.error && <details className="task-error"><summary>查看诊断信息</summary>{task.error}</details>}
    {task.result != null && <details className="task-result"><summary>{partial ? '查看阶段结果与未完成项' : '查看调研结果'}</summary><div className="result-text">{typeof task.result === 'string' ? task.result : JSON.stringify(task.result, null, 2)}</div></details>}
    {!!task.sources?.length && <div className="sources">{task.sources.map((source, i) => <a href={source.url} target="_blank" rel="noreferrer" key={`${source.url}-${i}`}><Icon name="link" size={13}/>{source.title || sourceName(source.url)}{source.queried_at && <small>{source.queried_at}</small>}</a>)}</div>}
    <div className="task-actions"><button onClick={() => send({ type: 'task.get', task_id: task.id })}>查进度</button><button onClick={() => setEditing(!editing)}>{task.status === 'waiting_clarification' ? '补充回答' : terminal ? '追加调研' : '追加条件'}</button>{!terminal && <button className="cancel-action" onClick={() => send({ type: 'task.cancel', task_id: task.id, expected_revision: task.revision })}>取消</button>}</div>
    {(editing || task.status === 'waiting_clarification') && <form className="task-edit" onSubmit={e => { e.preventDefault(); submit(); }}><label className="sr-only" htmlFor={`reply-${task.id}`}>补充任务条件</label><textarea id={`reply-${task.id}`} value={text} onChange={e => setText(e.target.value)} placeholder={task.status === 'waiting_clarification' ? '填写你的回答…' : '例如：只考虑室内活动，预算 50 欧以内'}/><button className="small-primary" type="submit" disabled={!text.trim()}>发送补充</button></form>}
    <button className="task-trace-link" onClick={onTrace}><Icon name="trace" size={13}/>模型输出 / Agent 轨迹</button><span className="task-id mono" title={task.id}>{task.id.slice(0, 8)}</span>
  </article>;
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [sessionId, setSessionId] = useState('');
  const [pageId] = useState(() => crypto.randomUUID());
  const [persona, setPersona] = useState<PersonaConfig>(initialPersona);
  const [personaVersion, setPersonaVersion] = useState(1);
  const [custom, setCustom] = useState<PersonaConfig>({ ...initialPersona, id: 'custom', name: '我的助手' });
  const [personaOpen, setPersonaOpen] = useState(false);
  const [showCustom, setShowCustom] = useState(false);
  const [speakerSeat, setSpeakerSeat] = useState<SpeakerSeat>('driver');
  const [vehicle, setVehicle] = useState<Record<string, unknown>>({});
  const vehicleFeedback = useVehicleChanges(vehicle);
  const vehiclePanel = useRef<HTMLElement | null>(null);
  const vehicleGroups = useRef<Record<string, HTMLDetailsElement | null>>({});
  const vehicleSetup = useRef<HTMLDetailsElement | null>(null);
  const changedGroups = new Set([...vehicleFeedback.changes.values()].map(change => change.path[0]));
  const metaChanges = Object.fromEntries([...vehicleFeedback.changes.values()]
    .filter(change => change.path[0] === '_meta').map(change => [change.path[1], change.after]));
  useEffect(() => {
    if (!vehicleFeedback.latestGroups.length) return;
    for (const id of vehicleFeedback.latestGroups) {
      const group = vehicleGroups.current[id];
      if (group) group.open = true;
    }
    // Scroll only the simulator pane so the user's conversation stays in place.
    const timer = window.setTimeout(() => {
      const panel = vehiclePanel.current;
      const firstGroup = vehicleFeedback.latestGroups[0];
      const target = firstGroup === '_meta' ? vehicleSetup.current : vehicleGroups.current[firstGroup];
      if (!panel || !target || panel.clientHeight === 0) return;
      const headerHeight = panel.querySelector('.panel-heading')?.getBoundingClientRect().height || 0;
      const top = panel.scrollTop + target.getBoundingClientRect().top - panel.getBoundingClientRect().top - headerHeight - 6;
      panel.scrollTo({ top: Math.max(0, top), behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    }, 160);
    return () => window.clearTimeout(timer);
  }, [vehicleFeedback.sequence]);
  const [tasks, setTasks] = useState<TaskCardData[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [text, setText] = useState('');
  const [taskGoal, setTaskGoal] = useState('');
  const [showTaskCreate, setShowTaskCreate] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(false);
  const [audioUnlocking, setAudioUnlocking] = useState(false);
  const [audioRecords, setAudioRecords] = useState<AudioMeasurement[]>([]);
  const [recognitionPhase, setRecognitionPhase] = useState<RecognitionPhase>('idle');
  const [recognitionPreview, setRecognitionPreview] = useState('');
  const [recognitionRecords, setRecognitionRecords] = useState<RecognitionMeasurement[]>([]);
  const [handsFreeActive, setHandsFreeActive] = useState(false);
  const recognizing = recognitionPhase !== 'idle' && recognitionPhase !== 'listening';
  const [speechPhase, setSpeechPhase] = useState<'idle' | 'synthesizing' | 'playing'>('idle');
  const speaking = speechPhase !== 'idle';
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState<'chat' | 'vehicle' | 'tasks'>('chat');
  const [traceOpen, setTraceOpen] = useState(false);
  const [traceTarget, setTraceTarget] = useState<TraceTarget>({ type: 'session' });
  const closeTrace = useCallback(() => setTraceOpen(false), []);
  function openTrace(target: TraceTarget) { setTraceTarget(target); setTraceOpen(true); }
  const ws = useRef<WebSocket | null>(null);
  const activeTurn = useRef<string | null>(null);
  const invalidatedTurns = useRef(new Set<string>());
  const speechEngine = useRef<StreamingSpeech | null>(null);
  const recognitionEngine = useRef<StreamingRecognition | null>(null);
  const handsFreeEngine = useRef<HandsFreeRecognition | null>(null);
  const captureActive = useRef(false);
  const captureId = useRef('');
  const voiceVersion = useRef(0);
  const connectedRef = useRef(connected);
  const configRef = useRef(config);
  const speechPreference = useRef(false);
  const submitInputRef = useRef<(value: string, voiceCaptureId?: string) => Promise<boolean>>(async () => false);
  connectedRef.current = connected; configRef.current = config;
  const lastTaskEvent = useRef(0);
  const messagesEnd = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const send = useCallback((value: Record<string, unknown>) => { if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(value)); else setError('会话尚未连接，请等待连接后重试。'); }, []);

  if (!speechEngine.current) speechEngine.current = new StreamingSpeech({
    send: value => { if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(value)); },
    phase: setSpeechPhase,
    metrics: (turn, metrics) => setMessages(current => current.map(row => row.turnId === turn || row.id === turn ? { ...row, speechMetrics: metrics } : row)),
    records: setAudioRecords,
    error: setError,
    allowed: turn => !captureActive.current && !invalidatedTurns.current.has(turn),
  });
  if (!recognitionEngine.current) recognitionEngine.current = new StreamingRecognition({
    capture: (id, active) => {
      if (active) {
        captureId.current = id; captureActive.current = true; voiceVersion.current++;
        send({ type: 'asr.capture', capture_id: id, active: true });
        // Reserve recording first, then playback(false), on the same control WS.
        void speechEngine.current?.setEnabled(false); setAutoSpeak(false);
      } else {
        if (captureId.current === id) { captureActive.current = false; captureId.current = ''; }
        if (ws.current?.readyState === WebSocket.OPEN) send({ type: 'asr.capture', capture_id: id, active: false });
      }
    },
    unlockSpeech: () => speechEngine.current!.unlock(),
    phase: setRecognitionPhase, transcript: setRecognitionPreview, records: setRecognitionRecords, error: setError,
    final: async (finalText, id) => {
      if (!recognitionEngine.current?.isCurrent(id) || !connectedRef.current) return;
      let enabled = false;
      try {
        if (configRef.current?.tts?.configured) enabled = await speechEngine.current!.setEnabled(true);
        else setError('识别已完成；火山朗读尚未配置，本轮使用文字回复。');
      } catch (error) { setError(`识别已完成；火山朗读无法启用：${error instanceof Error ? error.message : String(error)}`); }
      if (!recognitionEngine.current?.isCurrent(id) || !connectedRef.current) return;
      captureActive.current = false; speechPreference.current = enabled; setAutoSpeak(enabled);
      await submitInputRef.current(finalText, id);
    },
    settled: reason => {
      if (reason === 'final') return;
      // Cancel an in-flight final→TTS enable even when the prior preference was off.
      void speechEngine.current?.setEnabled(false);
      if (reason === 'disposed' || reason === 'disconnected' || !speechPreference.current || captureActive.current || !connectedRef.current) return;
      const version = voiceVersion.current;
      void speechEngine.current?.setEnabled(true).then(enabled => {
        if (version === voiceVersion.current && !captureActive.current && connectedRef.current) setAutoSpeak(enabled);
      }).catch(error => setError(`火山朗读无法恢复：${error instanceof Error ? error.message : String(error)}`));
    },
  });
  if (!handsFreeEngine.current) handsFreeEngine.current = new HandsFreeRecognition({
    unlockSpeech: async () => {
      const enabled = await speechEngine.current!.setEnabled(true);
      if (handsFreeEngine.current?.active) { speechPreference.current = enabled; setAutoSpeak(enabled); }
      else void speechEngine.current?.setEnabled(false);
    },
    capture: (id, active) => {
      if (active) {
        captureId.current = id; captureActive.current = true; voiceVersion.current++;
        // The server reserved this capture and interrupted its actor before this event.
        if (activeTurn.current) invalidatedTurns.current.add(activeTurn.current);
        activeTurn.current = null;
        setMessages(current => current.map(row => row.state === 'streaming' ? { ...row, state: 'interrupted' as const } : row));
        void speechEngine.current?.cancel('voice_activity');
      } else {
        if (captureId.current === id) { captureActive.current = false; captureId.current = ''; }
        if (ws.current?.readyState === WebSocket.OPEN) send({ type: 'asr.capture', capture_id: id, active: false });
      }
    },
    phase: phase => { setRecognitionPhase(phase); if (phase === 'idle') setHandsFreeActive(false); },
    transcript: setRecognitionPreview, records: setRecognitionRecords, error: setError,
    final: async (finalText, id) => {
      if (!handsFreeEngine.current?.isCurrent(id) || !connectedRef.current) return;
      captureActive.current = false;
      await submitInputRef.current(finalText, id);
    },
  });
  function toggleHandsFree() {
    if (handsFreeEngine.current?.active) { voiceVersion.current++; handsFreeEngine.current.cancel(); return; }
    recognitionEngine.current?.cancel('handsfree'); setError(''); setHandsFreeActive(true);
    void handsFreeEngine.current?.start(sessionId);
  }
  const stopSpeech = useCallback(() => speechEngine.current?.cancel('user_stop'), []);
  async function toggleSpeech(enabled: boolean) {
    if (recognitionEngine.current?.active || handsFreeEngine.current?.active) return;
    speechPreference.current = enabled;
    setAudioUnlocking(enabled);
    try { const active = await speechEngine.current!.setEnabled(enabled); setAutoSpeak(active); }
    catch (error) { setAutoSpeak(false); setError(`火山音频无法启用：${error instanceof Error ? error.message : String(error)}`); }
    finally { setAudioUnlocking(false); }
  }
  useEffect(() => { messagesEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages]);

  useEffect(() => {
    let disposed = false;
    let reconnect: ReturnType<typeof setTimeout>;
    let socket: WebSocket;
    async function start() {
      try {
        const response = await fetch('/api/config');
        if (!response.ok) throw new Error(`配置读取失败（${response.status}）`);
        const data = await response.json() as Config;
        if (disposed) return;
        setConfig(data); if (data.tts) speechEngine.current?.configure(data.tts.provider, data.tts.voice);
        let id = sessionStorage.getItem('super-xd-session');
        if (!id) { const response = await fetch('/api/sessions', { method: 'POST' }); if (!response.ok) throw new Error(`会话创建失败（${response.status}）`); id = (await response.json()).session_id; sessionStorage.setItem('super-xd-session', id!); }
        if (disposed) return;
        setSessionId(id!);
        connect(id!);
      } catch (e) { if (!disposed) { setConnecting(false); setError(`${e instanceof Error ? e.message : String(e)}。请确认云端会话服务已启动。`); reconnect = setTimeout(start, 4000); } }
    }
    function connect(id: string) {
      setConnecting(true);
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${id}`);
      ws.current = socket;
      socket.onopen = () => { if (disposed) return; connectedRef.current = true; setConnected(true); setConnecting(false); setError(''); speechEngine.current?.connect(id); };
      socket.onclose = event => { if (disposed) return; connectedRef.current = false; recognitionEngine.current?.cancel('disconnected'); handsFreeEngine.current?.cancel('disconnected'); voiceVersion.current++; speechEngine.current?.disconnect(); setConnected(false); if (event.code === 4404) { setConnecting(false); setError('会话已失效，请点击“新建会话”重新开始。原型仅保证连续在线会话。'); return; } setConnecting(true); reconnect = setTimeout(() => connect(id), 2000); };
      socket.onerror = () => { if (!disposed) setError('连接暂时中断，正在重新连接。'); };
      socket.onmessage = event => {
        if (disposed) return;
        let m: Record<string, any>;
        try { m = JSON.parse(event.data); } catch { setError('服务返回了无法识别的消息。'); return; }
        if (m.type === 'session.snapshot') {
          const snapshot = m as unknown as SessionSnapshot;
          setSpeakerSeat(snapshot.speaker?.seat || 'driver');
          if (snapshot.persona) setPersona(snapshot.persona);
          setPersonaVersion(snapshot.persona_version || 1); setVehicle(snapshot.vehicle || {}); setTasks(snapshot.tasks || []);
          setMessages(current => {
            if (current.length) return current.map(row => {
              const turn = snapshot.turns?.find(turn => turn.turn_id === row.turnId);
              return row.role === 'assistant' && turn ? { ...row, vehicleCalls: snapshotVehicleCalls(turn, row.vehicleCalls), lookupResults: turn.lookup_results ?? row.lookupResults } : row;
            });
            if (snapshot.turns?.length) {
              const last = snapshot.turns[snapshot.turns.length - 1];
              if (last.state === 'streaming' || (!last.state && !last.finished)) activeTurn.current = last.turn_id;
              return snapshot.turns.flatMap(turn => {
                const rows: ChatMessage[] = [];
                if (!turn.proactive && turn.text) rows.push({ id: `user-${turn.turn_id}`, turnId: turn.turn_id, role: 'user', text: turn.text });
                rows.push({ id: turn.turn_id, turnId: turn.turn_id, taskId: turn.task_id, role: 'assistant', text: turn.output || '', state: turn.state || (turn.finished ? 'done' : 'streaming'), proactive: turn.proactive, metrics: turn.metrics, vehicleCalls: snapshotVehicleCalls(turn), lookupResults: turn.lookup_results });
                return rows;
              });
            }
            return (snapshot.history || []).filter(h => (h.role === 'user' || h.role === 'assistant') && typeof h.content === 'string' && h.content).map((h, i) => ({ id: `${h.turn_id || 'history'}-${i}`, turnId: h.turn_id, role: h.role as 'user' | 'assistant', text: h.content as string, state: 'done' }));
          });
        } else if (m.type === 'lookup.result') {
          setMessages(current => current.map(row => row.turnId === m.turn_id && row.role === 'assistant'
            ? { ...row, lookupResults: [...(row.lookupResults || []).filter(item => item.call_id !== m.call_id), {call_id: m.call_id, tool: m.tool, result: m.result}] } : row));
        } else if (m.type === 'turn.started') {
          if (invalidatedTurns.current.has(m.turn_id) || !speechEngine.current?.acceptsTurn(m.turn_id, m.client_request_id)) return;
          speechEngine.current?.turnStarted(m.turn_id, m.client_request_id);
          activeTurn.current = m.turn_id;
          setMessages(current => [...current.map(row => row.state === 'streaming' ? { ...row, state: 'interrupted' as const } : row), { id: m.turn_id, turnId: m.turn_id, taskId: m.task_id, role: 'assistant', text: '', state: 'streaming', proactive: !!m.proactive, speechMetrics: speechEngine.current?.getMetrics(m.turn_id) }]);
        } else if (m.type === 'stream.delta') {
          if (m.turn_id !== activeTurn.current || invalidatedTurns.current.has(m.turn_id)) return;
          setMessages(current => current.map(row => row.id === m.turn_id ? { ...row, text: row.text + m.text } : row));
        } else if (m.type === 'stream.done') {
          if (m.turn_id !== activeTurn.current || invalidatedTurns.current.has(m.turn_id)) return;
          setMessages(current => current.map(row => row.id === m.turn_id ? { ...row, text: m.text ?? row.text, state: 'done', metrics: m.metrics } : row));
          activeTurn.current = null;

        } else if (m.type === 'stream.interrupted') {
          invalidatedTurns.current.add(m.turn_id);
          speechEngine.current?.interrupted(m.turn_id);
          if (activeTurn.current === m.turn_id) { activeTurn.current = null; stopSpeech(); }
          setMessages(current => current.map(row => row.id === m.turn_id ? { ...row, state: 'interrupted' } : row));
        } else if (m.type === 'vehicle.function_call') {
          setMessages(current => current.map(row => row.role === 'assistant' && row.turnId === m.turn_id
            ? { ...row, vehicleCalls: mergeVehicleCalls(row.vehicleCalls, [liveVehicleCall({ call: m.call, source: m.source, parent_call_id: m.parent_call_id, action_id: m.action_id, intent_id: m.intent_id })]) }
            : row));
        } else if (m.type === 'vehicle.command') {
          void fetch(`/api/sessions/${id}/vehicle/commands/${m.command_id}/execute`, {method:'POST'})
            .then(async response => { if (!response.ok) throw new Error('模拟车端执行请求失败'); return response.json(); })
            .then(receipt => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:'vehicle.receipt', command_id:receipt.command_id, receipt_id:receipt.receipt_id})); })
            .catch(error => setError(String(error)));
          setMessages(current => current.map(row => row.role === 'assistant' && row.turnId === m.turn_id ? { ...row, vehicleCalls: bindVehicleCommand(row.vehicleCalls, { command_id: m.command_id, parent_call_id: m.parent_call_id, action_id: m.action_id }) } : row));
          setReceipts(current => current.some(r => r.id === m.command_id) ? current : [{ id: m.command_id, name: m.name, arguments: m.arguments || {}, at: new Date().toLocaleTimeString('zh-CN', { hour12: false }) }, ...current].slice(0, 20));
        } else if (m.type.startsWith('audio.')) {
          speechEngine.current?.metadata(m);
        } else if (m.type === 'vehicle.result') {
          speechEngine.current?.annotate(m.turn_id, 'vehicle', { turn_id: m.turn_id, command_id: m.command_id, result: m.result, latency_ms: m.latency_ms });
          setMessages(current => current.map(row => row.role === 'assistant' && row.turnId === m.turn_id ? { ...row, vehicleCalls: applyVehicleReceipt(row.vehicleCalls, m.command_id, m.result) } : row));
          setReceipts(current => current.map(row => row.id === m.command_id ? { ...row, result: m.result, latency: m.latency_ms } : row));
          if (m.result?.state) setVehicle(m.result.state);
        } else if (m.type === 'vehicle.state') setVehicle(m.state || {});
        else if (m.type === 'speaker.changed') setSpeakerSeat(m.speaker.seat);
        else if (m.type === 'persona.changed') { setPersona(m.persona); setPersonaVersion(m.persona_version); setPersonaOpen(false); }
        else if (m.type === 'task.event' || m.type === 'ui.card') {
          if (m.type === 'task.event' && typeof m.event_id === 'number') { if (m.event_id <= lastTaskEvent.current) return; lastTaskEvent.current = m.event_id; }
          const task = m.task as TaskCardData;
          const origin = m.origin_turn_id || m.turn_id || m.task?.context?.origin_turn_id;
          if (origin && task?.id) speechEngine.current?.annotate(origin, 'task', { task_id: task.id });
          if (task?.id) setTasks(current => { const old = current.find(t => t.id === task.id); if (old && old.revision > task.revision) return current; return [task, ...current.filter(t => t.id !== task.id)]; });
        } else if (m.type === 'error') setError(m.message || '请求未完成，请重试。');
      };
    }
    start();
    return () => { disposed = true; connectedRef.current = false; clearTimeout(reconnect); recognitionEngine.current?.dispose(); handsFreeEngine.current?.dispose(); socket?.close(); speechEngine.current?.disconnect(); };
  }, [stopSpeech]);

  async function submitInput(value = text, voiceCaptureId?: string): Promise<boolean> {
    if (!value.trim() || !connectedRef.current || ws.current?.readyState !== WebSocket.OPEN || (!voiceCaptureId && audioUnlocking)) return false;
    if (voiceCaptureId && !recognitionEngine.current?.isCurrent(voiceCaptureId) && !handsFreeEngine.current?.isCurrent(voiceCaptureId)) return false;
    if (!voiceCaptureId) { recognitionEngine.current?.cancel('text_input'); handsFreeEngine.current?.cancel('text_input'); }
    const version = ++voiceVersion.current;
    const submittedAt = performance.now();
    const requestId = crypto.randomUUID();
    const stopped = speechEngine.current?.submit(requestId, value.trim(), submittedAt, tasks);
    if (activeTurn.current) invalidatedTurns.current.add(activeTurn.current);
    activeTurn.current = null;
    await Promise.resolve(stopped);
    if (version !== voiceVersion.current || !connectedRef.current) return false;
    if (voiceCaptureId) { recognitionEngine.current?.markSubmitted(voiceCaptureId, submittedAt, requestId); handsFreeEngine.current?.markSubmitted(voiceCaptureId, submittedAt, requestId); }
    setMessages(current => [...current.map(row => row.state === 'streaming' ? { ...row, state: 'interrupted' as const } : row), { id: `user-${crypto.randomUUID()}`, role: 'user', text: value.trim() }]);
    send({ type: 'input', text: value.trim(), client_request_id: requestId });
    // The ASR preview is separate: voice submission must preserve an existing typed draft.
    if (!voiceCaptureId) { setText(''); setError(''); inputRef.current?.focus(); }
    setActiveTab('chat');
    return true;
  }
  submitInputRef.current = submitInput;
  const runningTasks = tasks.filter(t => !['completed', 'failed', 'cancelled'].includes(t.status));
  const voiceUnavailable = !connected || !config?.asr?.configured || !config?.tts?.configured || !config?.vad?.configured || audioUnlocking;
  const groups = config?.catalog.skills || [];
  const lastReceipt = receipts.find(r => r.result);
  const latestMetrics = [...messages].reverse().find(m => m.metrics && !m.proactive)?.metrics;
  const firstTextMs = latestMetrics?.first_effective_ms ?? latestMetrics?.first_effective_text_ms ?? latestMetrics?.ttft_ms ?? latestMetrics?.first_text_ms;
  const latestAudio = [...audioRecords].reverse().find(record => record.submit_perf_ms !== undefined);
  const latestAudioMs = latestAudio?.first_nonzero_pcm_perf_ms !== undefined && latestAudio.submit_perf_ms !== undefined ? latestAudio.first_nonzero_pcm_perf_ms - latestAudio.submit_perf_ms : undefined;
  const isGenerating = messages.some(m => m.state === 'streaming');

  return <div className="app-shell">
    <header className="topbar">
      <a className="brand" href="/" aria-label="Super XD 首页"><span className="brand-symbol">xd<span/></span><span>SUPER XD<small>语音座舱实验室</small></span></a>
      <div className="top-meta"><span className="simulation-tag"><Icon name="car" size={15}/>模拟车机</span><span className={`connection ${connected ? 'online' : ''}`}><i/>{connected ? '会话在线' : connecting ? '正在连接' : '连接中断'}</span><details className="session-info"><summary>会话信息</summary><div className="session-info-content"><span>SESSION <code>{sessionId || '—'}</code></span><span>Talker <code>{config?.model.talker || '—'}</code></span><span>Thinker <code>{config?.model.thinker || '—'}</code></span></div></details></div>
      <div className="status-readings" aria-label="本轮延迟">
        <span><span className="reading-label">首字</span><strong className="mono">{firstTextMs != null ? `${Math.round(Number(firstTextMs))} ms` : '—'}</strong></span>
        <span><span className="reading-label">车端回执</span><strong className="mono">{lastReceipt?.latency != null ? `${Math.round(lastReceipt.latency)} ms` : '—'}</strong></span>
        <span className={`latency-target ${pcmTargetClass(latestAudioMs)}`} title={`目标 ≤ ${QUERY_TO_NONZERO_PCM_TARGET_LABEL}`}><span className="reading-label">首非零 PCM <span className="status-target">目标 ≤ {QUERY_TO_NONZERO_PCM_TARGET_LABEL}</span></span><strong className="mono">{latestAudioMs != null ? `${Math.round(latestAudioMs)} ms` : '—'}</strong></span>
      </div>
      <div className="header-actions">
        <button className="trace-trigger" onClick={() => openTrace({ type: 'session' })} aria-expanded={traceOpen}><Icon name="trace" size={17}/><span className="trace-label-full">模型输出 / Agent 轨迹</span><span className="trace-label-short">调用轨迹</span></button>
        <button className="persona-trigger" onClick={() => setPersonaOpen(!personaOpen)} aria-expanded={personaOpen}><Icon name="settings" size={17}/><span>{persona.name}</span></button>
        <button className="new-session" onClick={() => { stopSpeech(); sessionStorage.removeItem('super-xd-session'); location.reload(); }}><span aria-hidden="true">＋</span>新建会话</button>
      </div>
    </header>
    <div className="mobile-tabs" role="tablist">{[['chat', '对话'], ['vehicle', `模拟车况${vehicleFeedback.changes.size ? ' · 已更新' : ''}`], ['tasks', `后台任务 ${runningTasks.length || ''}`]].map(([id, label]) => <button role="tab" aria-selected={activeTab === id} className={activeTab === id ? 'selected' : ''} key={id} onClick={() => setActiveTab(id as typeof activeTab)}>{label}</button>)}</div>
    {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}><Icon name="close" size={16}/></button></div>}
    <main className="workspace">
      <aside ref={vehiclePanel} className={`vehicle-panel panel ${activeTab === 'vehicle' ? 'mobile-active' : ''} ${vehicleFeedback.changes.size ? 'has-vehicle-update' : ''}`}>
        <div className="panel-heading"><div><span className="eyebrow">车辆模拟器</span><h2>车厢状态</h2></div>{vehicleFeedback.changes.size ? <span key={vehicleFeedback.sequence} className="quiet-label vehicle-update-badge" role="status"><i/>车况已更新 · {vehicleFeedback.changes.size} 项</span> : <span className="quiet-label">{Number((vehicle._meta as Record<string, unknown>)?.speed_kph || 0) > 0 ? '行驶中' : '静止'}</span>}</div>
        <div className="vehicle-overview"><Icon name="car" size={16}/><span>模拟车况 · 回执后同步</span><span className="mono">{config?.catalog.tools.length || '—'} 项车控</span></div>
        <details ref={vehicleSetup} className={`vehicle-setup ${changedGroups.has('_meta') ? 'vehicle-setup-changed' : ''}`}><summary>驾驶环境设置<span className="mono">{Number((vehicle._meta as Record<string, unknown>)?.speed_kph || 0)} km/h · {String((vehicle._meta as Record<string, unknown>)?.gear || 'P')}</span><Icon name="arrow" size={12}/></summary><VehicleSimulatorControls vehicle={vehicle} send={send}/></details>{Object.keys(metaChanges).length > 0 && <div className="vehicle-setting-changes"><StateRows value={metaChanges} path={['_meta']} changes={vehicleFeedback.changes}/></div>}
        <div className="vehicle-groups">{groups.map((skill, index) => <details key={skill.id} ref={node => { vehicleGroups.current[skill.id] = node; }} className={changedGroups.has(skill.id) ? 'vehicle-group-changed' : undefined} open={index === 0}><summary><span className="group-icon">{['❄', '♧', '▱', '◈', '☼', '⊏', '▰', '≋', '♫', '⌖'][index]}</span><span>{skill.name}</span>{changedGroups.has(skill.id) && <span key={vehicleFeedback.sequence} className="vehicle-group-update">已更新</span>}<span className="group-count mono">{skill.tool_names.length}</span><Icon name="arrow" size={13}/></summary><div className="group-state"><StateRows value={vehicle[skill.id] || { 状态: '等待车况同步' }} path={[skill.id]} changes={vehicleFeedback.changes}/>{index === 9 && <p className="simulation-note">仅模拟目的地、途经点和偏好，不计算真实路线。</p>}</div></details>)}</div>
        <details className="receipt-panel" open={receipts.length > 0}><summary>执行回执 <span className="count">{receipts.length}</span></summary><div className="receipts">{receipts.length === 0 && <p className="empty-copy">试着调节空调，执行结果会显示在这里。</p>}{receipts.map(r => <div className="receipt" key={r.id}><div><span className={r.result ? r.result.ok ? 'receipt-ok' : 'receipt-no' : 'muted'}>{r.result ? r.result.ok ? '执行成功' : '车辆拒绝' : '等待回执'}</span><time>{r.at}</time></div><strong>{config?.catalog.tools.find(t => t.name === r.name)?.description || r.name}</strong><code>{JSON.stringify(r.arguments)}</code>{!!r.result?.reason && <p>{String(r.result?.reason)}</p>}{r.latency != null && <span className="mono receipt-latency">{Math.round(r.latency)} ms · 提交至回执</span>}<details><summary>结构化回执</summary><pre>{JSON.stringify(r.result || { command_id: r.id, status: 'pending' }, null, 2)}</pre></details></div>)}</div></details>
      </aside>
      <section className={`conversation-panel panel ${activeTab === 'chat' ? 'mobile-active' : ''}`}>
        <div className="conversation-heading"><div><span className="eyebrow">随时说，随时响应</span><h1>座舱对话</h1></div><span className={`activity-badge ${isGenerating || speaking || recognizing ? 'active' : ''}`}><span className="wave"><i/><i/><i/><i/></span>{recognitionPhase === 'listening' ? (speaking ? '正在朗读 · 可随时打断' : '持续监听中') : recognitionPhase === 'recording' ? '正在听你说' : recognizing ? '处理语音输入' : speechPhase === 'synthesizing' ? '准备火山语音' : speaking ? '正在朗读' : isGenerating ? '正在回应' : '准备就绪'}</span></div>
        <div className="conversation" role="log" aria-label="对话记录" aria-live="polite">
          {messages.length === 0 && <div className="welcome"><div className="welcome-mark"><Icon name="spark" size={32}/></div><h2>车里的事，交给我。</h2><p>调温度、查天气、找地点，或者聊聊天。<br/>复杂调研会在后台继续处理。</p><div className="suggestions">{EXAMPLES.map((example, i) => <button key={example} onClick={() => submitInput(example)} disabled={!connected || audioUnlocking}><span className="suggestion-icon"><Icon name={['car', 'car', 'spark', 'clock'][i]} size={17}/></span><span>{example}</span><Icon name="arrow" size={14}/></button>)}</div></div>}
          {messages.map(message => <article key={message.id} className={`message message-${message.role}`}><div className="message-avatar">{message.role === 'user' ? '你' : <img src={bydLogo} alt="BYD"/>}</div><div className="message-body"><span className="message-name sr-only">{message.role === 'user' ? '你' : '助手'}</span><div className={`message-text ${message.state === 'streaming' ? 'streaming' : ''}`}>{message.text || (message.state === 'streaming' ? <span className="typing-dots"><i/><i/><i/></span> : message.state === 'interrupted' ? '已打断' : '')}</div>{message.state === 'interrupted' && message.text && <span className="interrupted-label">已打断</span>}{message.proactive && <span className="interrupted-label">后台任务结果</span>}{message.role === 'assistant' && !!message.vehicleCalls?.length && <VehicleCalls calls={message.vehicleCalls}/>}{!!message.lookupResults?.length && <LookupResults records={message.lookupResults}/>}
            {message.role === 'assistant' && <details className="message-diagnostics">
              <summary title="辅助信息" aria-label="辅助信息"><Icon name="arrow" size={12}/><span className="sr-only">辅助信息</span></summary>
              <div className="message-diagnostics-content">
                {message.speechMetrics && <div className="speech-metrics mono" title={`${message.speechMetrics.provider} · ${message.speechMetrics.voice}`}>火山 xiaohe2.0 · PCM{message.speechMetrics.query_to_first_nonzero_frame_ms != null && <span className={pcmTargetClass(message.speechMetrics.query_to_first_nonzero_frame_ms)} title={`目标 ≤ ${QUERY_TO_NONZERO_PCM_TARGET_LABEL}`}>提交→首非零 PCM {Math.round(message.speechMetrics.query_to_first_nonzero_frame_ms)} ms</span>}{message.speechMetrics.query_to_first_frame_ms != null && <span>提交→首 PCM {Math.round(message.speechMetrics.query_to_first_frame_ms)} ms</span>}{message.speechMetrics.query_to_first_play_scheduled_ms != null && <span>提交→首帧入队 {Math.round(message.speechMetrics.query_to_first_play_scheduled_ms)} ms</span>}{message.speechMetrics.query_to_first_render_observed_ms != null && <span>提交→渲染通知 {Math.round(message.speechMetrics.query_to_first_render_observed_ms)} ms</span>}{message.speechMetrics.query_to_first_nonzero_render_observed_ms != null && <span>首非零渲染通知 {Math.round(message.speechMetrics.query_to_first_nonzero_render_observed_ms)} ms</span>}{message.proactive && message.speechMetrics.first_frame_from_audio_started_ms != null && <span>后台音频开始→首帧 {Math.round(message.speechMetrics.first_frame_from_audio_started_ms)} ms</span>}</div>}{message.metrics && !message.proactive && <div className="message-metrics mono">{Object.entries(message.metrics).filter(([key, v]) => typeof v === 'number' && (key.includes('text') || key.includes('receipt') || key === 'ttft_ms' || key === 'first_effective_ms')).map(([key, value]) => <span key={key}>{key.includes('receipt') ? '车端回执' : '首个有效文字'} {Math.round(Number(value))} ms</span>)}</div>}{message.role === 'assistant' && <button className="message-trace-link" onClick={() => openTrace(message.turnId ? { type: 'turn', id: message.turnId } : { type: 'session' })}><Icon name="trace" size={13}/>{message.turnId ? '模型输出 / Agent 轨迹' : '查看会话轨迹'}</button>}
              </div>
            </details>}
          </div></article>)}
          <div ref={messagesEnd}/>
        </div>
        <div className="compose-area"><label className="speaker-selector"><span>当前音区</span><select aria-label="当前用户座位（模拟音区）" title="未指定位置的车控默认作用于这里；切换从下一轮生效" value={speakerSeat} disabled={!connected || recognizing || handsFreeActive} onChange={e => send({ type: 'speaker.set', seat: e.target.value })}><option value="driver">主驾</option><option value="front_passenger">副驾</option><option value="rear_left">左后排</option><option value="rear_right">右后排</option></select></label>{!config?.tts?.configured && <div className="tts-configuration">火山 xiaohe2.0 待配置：{config?.tts?.missing_fields?.join('、') || '服务端语音凭证'}</div>}{!config?.asr?.configured && <div className="asr-configuration">火山语音输入待配置{config?.asr?.missing_fields?.length ? `：${config.asr.missing_fields.join('、')}` : ''} · 可用文字或豆包输入法</div>}{(recognizing || handsFreeActive) && <div className="voice-capture" role="status" aria-live="polite"><div><span className={`capture-dot ${recognitionPhase === 'recording' ? 'recording' : ''}`}/><strong>{recognitionPhase === 'requesting' ? '等待麦克风权限…' : recognitionPhase === 'connecting' ? '正在连接语音识别…' : recognitionPhase === 'listening' ? '持续监听中，直接说话即可打断助手' : recognitionPhase === 'recording' ? '正在听，停顿后自动发送' : recognitionPhase === 'recognizing' ? '录音已结束，正在识别…' : '识别完成，准备回复…'}</strong><button type="button" onClick={() => { recognitionEngine.current?.cancel('user_cancel'); handsFreeEngine.current?.cancel('user_cancel'); voiceVersion.current++; }}>取消</button></div><p>{recognitionPreview || '麦克风在回复播放期间保持开启；停止说话约 0.64 秒后识别并发送。'}</p>{recognitionPreview && <small>识别预览 · 尚未发送</small>}</div>}<form className="composer" onSubmit={e => { e.preventDefault(); submitInput(); }}><label className="sr-only" htmlFor="chat-input">对助手说点什么</label><textarea ref={inputRef} id="chat-input" value={text} onChange={e => { recognitionEngine.current?.cancel('text_input'); handsFreeEngine.current?.cancel('text_input'); voiceVersion.current++; setText(e.target.value); }} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submitInput(); } }} placeholder="对我说点什么…" title="Enter 发送 · Shift + Enter 换行；新输入会停止当前回复与朗读" rows={1}/><div className="composer-actions"><button type="button" className={`voice-button ${handsFreeActive ? 'recording' : ''}`} disabled={!handsFreeActive && voiceUnavailable} aria-pressed={handsFreeActive} aria-label={handsFreeActive ? '停止连续语音对话' : '开始连续语音对话'} title={handsFreeActive ? '点击停止连续监听' : voiceUnavailable ? '连续语音对话尚未就绪' : '点击开始连续语音对话，停顿自动发送'} onClick={toggleHandsFree}><Icon name={handsFreeActive ? 'stop' : 'mic'} size={19}/></button>{speaking && <button type="button" className="stop-speaking" onClick={stopSpeech} aria-label="停止朗读"><Icon name="stop" size={16}/></button>}<button className="send-button" type="submit" disabled={!text.trim() || !connected || audioUnlocking} aria-label="发送消息"><Icon name="send" size={23}/></button></div></form><div className="compose-footnote"><div className="compose-topline"><span>{runningTasks.length > 0 && <><i className="task-dot"/>{runningTasks.length} 个后台任务正在处理</>}</span><button type="button" className={`handsfree-toggle ${handsFreeActive ? 'enabled' : ''}`} aria-pressed={handsFreeActive} disabled={!handsFreeActive && voiceUnavailable} onClick={toggleHandsFree}>{handsFreeActive ? '关闭免按键对话' : '开启免按键对话'}</button><label className="speech-toggle"><input type="checkbox" checked={autoSpeak} disabled={!config?.tts?.configured || !connected || audioUnlocking || recognizing || handsFreeActive} onChange={e => void toggleSpeech(e.target.checked)}/><Icon name="sound" size={15}/>{handsFreeActive ? '语音回复已开启' : recognizing ? '录音时暂停朗读' : audioUnlocking ? '解锁音频…' : '火山朗读'}</label></div><AudioMeasurements records={audioRecords} recognitionRecords={recognitionRecords} sessionId={sessionId} pageId={pageId}/></div></div>
      </section>
      <aside className={`task-panel panel ${activeTab === 'tasks' ? 'mobile-active' : ''}`}><div className="panel-heading"><div><span className="eyebrow">复杂的事，慢慢想</span><h2>后台任务 <span className="heading-count">{tasks.length}</span></h2></div><button className="add-task" aria-label="新建后台任务" onClick={() => setShowTaskCreate(!showTaskCreate)}>+</button></div><p className="task-intro">复杂规划、比较和调研会在这里继续。<br/>不影响你聊天和控制车辆。</p>{showTaskCreate && <form className="task-create" onSubmit={e => { e.preventDefault(); if (taskGoal.trim()) { send({ type: 'task.create', goal: taskGoal.trim() }); setTaskGoal(''); setShowTaskCreate(false); } }}><label htmlFor="task-goal">你想调研什么？</label><textarea id="task-goal" value={taskGoal} onChange={e => setTaskGoal(e.target.value)} placeholder="例如：找巴黎周末亲子室内活动，附来源"/><button className="small-primary" disabled={!taskGoal.trim() || !connected}>开始后台任务</button></form>}<div className="task-list">{tasks.length === 0 ? <div className="tasks-empty"><div className="task-orbit"><Icon name="clock" size={28}/></div><strong>暂时没有后台任务</strong><p>你可以直接告诉助手需求，<br/>也可以手动新建一个任务。</p><button onClick={() => setShowTaskCreate(true)} disabled={!connected}>新建调研任务 <span>↗</span></button></div> : tasks.map(task => <TaskCard key={task.id} task={task} send={send} onTrace={() => openTrace({ type: 'task', id: task.id })}/>)}</div><div className="research-note"><Icon name="link" size={16}/><p>联网信息来自实时检索，结果附来源链接和查询时间。车辆控制与导航均为模拟。</p></div></aside>
    </main>
    <TraceDrawer open={traceOpen} sessionId={sessionId} target={traceTarget} messages={messages} tasks={tasks} onClose={closeTrace}/>
    {personaOpen && <div className="modal-backdrop" onClick={() => setPersonaOpen(false)}><section className="persona-dialog" role="dialog" aria-modal="true" aria-labelledby="persona-title" onClick={e => e.stopPropagation()}><div className="dialog-heading"><div><span className="eyebrow">按你的习惯来</span><h2 id="persona-title">选择助手风格</h2></div><button className="icon-button" aria-label="关闭风格设置" onClick={() => setPersonaOpen(false)}><Icon name="close"/></button></div><p className="dialog-copy">从下一轮对话生效，正在进行的任务会继续。</p><div className="persona-options">{(config?.personas || []).map((preset, i) => <button className={persona.id === preset.id ? 'chosen' : ''} key={preset.id} onClick={() => send({ type: 'persona.set', preset: preset.id })}><span className={`preset-mark mark-${i}`}><Icon name={['settings', 'spark', 'sound'][i]} size={20}/></span><span><strong>{preset.name}</strong><small>{preset.tone}</small></span>{persona.id === preset.id && <Icon name="check" size={18}/>}</button>)}</div><button className="custom-toggle" onClick={() => setShowCustom(!showCustom)}>自定义助手 <span>{showCustom ? '−' : '+'}</span></button>{showCustom && <form className="custom-form" onSubmit={e => { e.preventDefault(); send({ type: 'persona.set', config: custom }); }}><div className="form-grid"><label>名称<input value={custom.name} maxLength={30} onChange={e => setCustom({ ...custom, name: e.target.value })} required/></label><label>句长偏好<select value={custom.sentence_length} onChange={e => setCustom({ ...custom, sentence_length: e.target.value as PersonaConfig['sentence_length'] })}><option value="short">简短</option><option value="medium">适中</option></select></label></div><label>语气描述<input value={custom.tone} maxLength={160} onChange={e => setCustom({ ...custom, tone: e.target.value })} placeholder="例如：平静、亲切，偶尔幽默" required/></label><div className="form-grid"><label>口头禅频率<select value={custom.catchphrase_frequency} onChange={e => setCustom({ ...custom, catchphrase_frequency: e.target.value as PersonaConfig['catchphrase_frequency'] })}><option value="never">从不</option><option value="low">偶尔</option><option value="normal">适量</option></select></label><label>朗读音色<input value="火山 Speech · xiaohe2.0" readOnly aria-label="朗读音色，固定火山 xiaohe2.0"/></label></div><button className="small-primary" disabled={!connected}>应用自定义风格</button></form>}<div className="persona-version mono">当前版本 v{personaVersion}</div></section></div>}
  </div>;
}
