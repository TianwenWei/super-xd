// Execute the real App with explicit browser/engine doubles; no live audio or services.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';

const runtimeKey = '__superXDAppVoiceTestRuntime';
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const runtimeAccess = `const runtime = () => globalThis[${JSON.stringify(runtimeKey)}];`;
const reactModule = moduleUrl(`${runtimeAccess}
  export const useState = (...args) => runtime().useState(...args);
  export const useRef = (...args) => runtime().useRef(...args);
  export const useEffect = (...args) => runtime().useEffect(...args);
  export const useCallback = (...args) => runtime().useCallback(...args);
  export const useMemo = (...args) => runtime().useMemo(...args);
`);
const jsxModule = moduleUrl(`
  export const Fragment = Symbol('Fragment');
  export const jsx = (type, props, key) => ({ type, props: props || {}, key });
  export const jsxs = jsx;
`);

const dependencyModules = {
  react: reactModule,
  'react/jsx-runtime': jsxModule,
  './StreamingSpeech': moduleUrl(`${runtimeAccess}
    export class StreamingSpeech {
      enabled = false; enables = []; cancellations = []; submissions = []; connections = [];
      constructor(callbacks) { this.callbacks = callbacks; runtime().speech = this; }
      configure() {}
      async unlock() {
        this.unlocks = (this.unlocks || 0) + 1;
        if (runtime().audioActivationBlocked) {
          const error = new Error('浏览器尚未解锁声音，请点击进入语音对话。');
          error.name = 'AudioActivationRequired'; throw error;
        }
      }
      async setEnabled(enabled) { this.enables.push(enabled); this.enabled = enabled; return enabled; }
      connect(session) { this.connections.push(session); }
      disconnect() { this.disconnections = (this.disconnections || 0) + 1; }
      cancel(reason) { this.cancellations.push(reason); this.callbacks.phase('idle'); return Promise.resolve(); }
      submit(id, text, submitted, tasks) { this.submissions.push({ id, text, submitted, tasks }); return this.cancel('new_input'); }
      acceptsTurn() { return true; }
      turnStarted() {}
      interrupted() {}
      annotate() {}
      metadata() {}
      getMetrics() { return undefined; }
    }
  `),
  './StreamingRecognition': moduleUrl(`${runtimeAccess}
    export class StreamingRecognition {
      active = false; cancellations = [];
      constructor(callbacks) { this.callbacks = callbacks; runtime().recognition = this; }
      isCurrent() { return false; }
      cancel(reason) { this.cancellations.push(reason); }
      markSubmitted() {}
      dispose() {}
    }
  `),
  './HandsFreeRecognition': moduleUrl(`${runtimeAccess}
    export class HandsFreeRecognition {
      active = false; starts = []; cancellations = []; discards = []; submissions = []; captureId = '';
      constructor(callbacks) { this.callbacks = callbacks; runtime().handsFree = this; }
      async start(session) {
        this.starts.push(session); this.active = true; this.callbacks.phase('requesting');
        try { await this.callbacks.unlockSpeech(); }
        catch (error) {
          this.callbacks.error(error.message, { kind: error.name === 'AudioActivationRequired' ? 'activation' : 'capture' });
          this.cancel('error');
        }
      }
      ready() { assertActive(this); this.callbacks.phase('listening'); }
      isCurrent(id) { return this.active && this.captureId === id; }
      capture(id, active) { this.captureId = active ? id : ''; this.callbacks.capture(id, active); }
      final(text, id) { return this.callbacks.final(text, id); }
      markSubmitted(id, at, requestId) { this.submissions.push({ id, at, requestId }); }
      discardCapture(reason) {
        this.discards.push(reason);
        const id = this.captureId; this.captureId = '';
        if (id) this.callbacks.capture(id, false);
        if (this.active) this.callbacks.phase('listening');
      }
      providerError(message) { this.callbacks.error(message, { kind: 'provider', providerCode: 'auth' }); this.cancel('error'); }
      cancel(reason) {
        this.cancellations.push(reason);
        if (!this.active) return;
        this.active = false; this.captureId = ''; this.callbacks.phase('idle');
      }
      dispose() { this.cancel('disposed'); }
    }
    function assertActive(engine) { if (!engine.active) throw new Error('The test microphone was not started.'); }
  `),
  './requestViews': moduleUrl(`
    export const groupRequestTasks = (_requests, tasks) => ({ ungrouped: tasks });
    export const mergeRequest = (current, next) => [...current.filter(item => item.request_id !== next.request_id), next];
  `),
  './DeviceQueries': moduleUrl(`${runtimeAccess}
    export class DeviceQueryRegistry {
      constructor(valid) { this.valid = valid; this.commands = []; runtime().device = this; }
      register() { return () => {}; }
      async execute(command) { this.commands.push(command); return { type: 'cockpit.receipt', command_id: command.command_id, ok: this.valid(command.turn_id) }; }
      disconnect() {}
      invalidate() {}
      dispose() {}
    }
  `),
  './MediaPlayback': moduleUrl(`${runtimeAccess}
    export class MediaPlayback {
      constructor(callbacks) { this.callbacks = callbacks; runtime().media = this; }
      setFocus() {}
      snapshot() { return { status: 'closed', desired_status: 'closed', position_seconds: 0, volume: 30 }; }
      synchronizeVersion() {}
      disconnect() {}
      invalidate() {}
      dispose() {}
    }
  `),
  './vehicleCallViews': moduleUrl(`
    export const applyVehicleReceipt = value => value;
    export const bindVehicleCommand = value => value;
    export const liveVehicleCall = value => value;
    export const mergeVehicleCalls = value => value;
    export const snapshotVehicleCalls = () => [];
  `),
  './performanceTarget': moduleUrl(`export const QUERY_TO_NONZERO_PCM_TARGET_LABEL = '2 秒'; export const pcmTargetClass = () => '';`),
  './useVehicleChanges': moduleUrl(`export const useVehicleChanges = () => ({ changes: new Map(), latestGroups: [], sequence: 0 });`),
  './vehicleChanges': moduleUrl(`export const vehiclePathKey = path => path.join('.');`),
  './assets/byd-logo.svg': moduleUrl(`export default 'test-logo.svg';`),
};

const source = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
let compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const ast = ts.createSourceFile('App.js', compiled, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS);
for (const statement of ast.statements) {
  if (!ts.isImportDeclaration(statement)) continue;
  const name = statement.moduleSpecifier.text;
  const replacement = dependencyModules[name] || (name.endsWith('.css')
    ? moduleUrl('export {};')
    : moduleUrl(`export default function TestChild() { return null; }`));
  const original = statement.getText(ast);
  compiled = compiled.replace(original, original.replace(statement.moduleSpecifier.getText(ast), JSON.stringify(replacement)));
}
const { default: App } = await import(moduleUrl(compiled));

const sameDependencies = (before, after) => before && after && before.length === after.length && before.every((value, index) => Object.is(value, after[index]));
function elements(node, predicate) {
  if (Array.isArray(node)) return node.flatMap(child => elements(child, predicate));
  if (!node || typeof node !== 'object') return [];
  return [...(predicate(node) ? [node] : []), ...elements(node.props?.children, predicate)];
}
function textContent(node) {
  if (Array.isArray(node)) return node.map(textContent).join('');
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node !== 'object') return String(node);
  return textContent(node.props?.children);
}
const hasClass = (node, name) => typeof node.props?.className === 'string' && node.props.className.split(' ').includes(name);
function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    emit(name, event = {}) { for (const callback of listeners.get(name) || []) callback(event); },
  };
}

async function mount(t, { preferences = {}, audioActivationBlocked = false } = {}) {
  const previous = new Map();
  const replaceGlobal = (key, value) => {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  };
  const h = { slots: [], cursor: 0, dirty: true, effects: [], timers: new Map(), nextTimer: 1, sockets: [], fetches: [], reloads: 0, audioActivationBlocked };
  h.useState = initial => {
    const index = h.cursor++;
    if (!h.slots[index]) h.slots[index] = { value: typeof initial === 'function' ? initial() : initial };
    const slot = h.slots[index];
    return [slot.value, update => { const value = typeof update === 'function' ? update(slot.value) : update; if (!Object.is(value, slot.value)) { slot.value = value; h.dirty = true; } }];
  };
  h.useRef = initial => {
    const index = h.cursor++;
    h.slots[index] ??= { current: initial };
    return h.slots[index];
  };
  h.useEffect = (effect, dependencies) => {
    const index = h.cursor++;
    const slot = h.slots[index] ??= {};
    if (!sameDependencies(slot.dependencies, dependencies)) { slot.dependencies = dependencies; h.effects.push({ slot, effect }); }
  };
  h.useMemo = (factory, dependencies) => {
    const index = h.cursor++;
    const slot = h.slots[index] ??= {};
    if (!sameDependencies(slot.dependencies, dependencies)) { slot.dependencies = dependencies; slot.value = factory(); }
    return slot.value;
  };
  h.useCallback = (callback, dependencies) => h.useMemo(() => callback, dependencies);
  h.render = () => {
    h.cursor = 0; h.dirty = false; h.tree = App();
    const pending = h.effects.splice(0);
    for (const { slot, effect } of pending) { slot.cleanup?.(); slot.cleanup = effect(); }
  };
  h.flush = async () => {
    for (let index = 0; index < 30; index++) { if (h.dirty) h.render(); await Promise.resolve(); }
    if (h.dirty) h.render();
  };
  h.find = predicate => { const matches = elements(h.tree, predicate); assert.equal(matches.length, 1, 'Expected one visible element.'); return matches[0]; };
  h.voiceButton = () => h.find(node => node.type === 'button' && hasClass(node, 'voice-button'));
  h.chatInput = () => h.find(node => node.type === 'textarea' && node.props.id === 'chat-input');
  h.composer = () => h.find(node => node.type === 'form' && hasClass(node, 'composer'));
  h.speechToggle = () => elements(h.find(node => node.type === 'label' && hasClass(node, 'speech-toggle')), node => node.type === 'input')[0];
  h.badge = () => h.find(node => hasClass(node, 'activity-badge'));
  const setTimer = (callback, delay = 0) => { const id = h.nextTimer++; h.timers.set(id, { callback, delay }); return id; };
  const clearTimer = id => h.timers.delete(id);
  const window = { ...eventTarget(), setTimeout: setTimer, clearTimeout: clearTimer, setInterval: setTimer, clearInterval: clearTimer, matchMedia: () => ({ matches: true }) };
  const document = { ...eventTarget(), visibilityState: 'visible', hidden: false };
  const storage = new Map(Object.entries(preferences).map(([key, value]) => [key, String(value)]));
  replaceGlobal(runtimeKey, h);
  replaceGlobal('window', window); replaceGlobal('document', document);
  replaceGlobal('location', { protocol: 'http:', host: 'localhost', reload() { h.reloads++; } });
  replaceGlobal('sessionStorage', { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) });
  replaceGlobal('navigator', { permissions: { query: async () => ({ ...eventTarget(), state: 'granted' }) }, mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) } });
  replaceGlobal('setTimeout', setTimer); replaceGlobal('clearTimeout', clearTimer);
  replaceGlobal('WebSocket', class {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 3;
    readyState = 0; sent = [];
    constructor(url) { this.url = url; h.sockets.push(this); }
    send(value) { this.sent.push(JSON.parse(value)); }
    open() { this.readyState = 1; this.onopen?.(); }
    message(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  });
  replaceGlobal('fetch', async (url, options) => {
    h.fetches.push({ url, options });
    if (url === '/api/config') return { ok: true, json: async () => ({ model: { talker: 'explicit-test-mini', thinker: 'explicit-test-pro' }, catalog: { skills: [], tools: [] }, personas: [], asr: { configured: true }, tts: { configured: true, provider: 'explicit-test-tts', voice: 'explicit-test-voice' }, vad: { configured: true } }) };
    if (url === '/api/sessions' && options?.method === 'POST') return { ok: true, json: async () => ({ session_id: 'app-voice-session' }) };
    throw new Error(`Unexpected network call in offline test: ${url}`);
  });
  h.connect = async () => { h.socket = h.sockets.at(-1); assert.ok(h.socket); h.socket.open(); await h.flush(); };
  h.activate = async () => {
    if (!h.handsFree.active) { assert.equal(h.voiceButton().props.disabled, false); h.voiceButton().props.onClick(); await h.flush(); }
    h.handsFree.ready(); await h.flush();
    assert.equal(h.handsFree.active, true);
  };
  h.reconnect = async () => {
    h.socket.close(); await h.flush();
    const pending = [...h.timers].find(([, value]) => value.delay === 2000);
    assert.ok(pending, 'Control disconnect must schedule a reconnect.');
    h.timers.delete(pending[0]); pending[1].callback(); await h.flush(); await h.connect();
  };
  t.after(() => {
    for (const slot of h.slots) slot?.cleanup?.();
    for (const [key, descriptor] of previous) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; }
  });
  await h.flush();
  return h;
}

test('default speech intent is checked without claiming an unlocked listening microphone', async t => {
  const h = await mount(t);
  assert.equal(h.speechToggle().props.checked, true, 'Default speech preference should be enabled.');
  assert.equal(h.voiceButton().props['aria-pressed'], false, 'Connecting/permission pending is not a ready microphone.');
  assert.equal(textContent(h.badge()).includes('持续监听中'), false);
});

test('configured fresh control connection starts continuous intent and waits for actual listening phase', async t => {
  const h = await mount(t); await h.connect();
  assert.equal(h.handsFree.starts.length, 1, 'Fresh online connection should honor the default continuous intent.');
  assert.equal(h.voiceButton().props['aria-pressed'], false, 'Startup must not report ready before the engine phase.');
  h.handsFree.ready(); await h.flush();
  assert.equal(h.voiceButton().props['aria-pressed'], true);
});

test('typing a draft preserves the continuous microphone', async t => {
  const h = await mount(t); await h.connect(); await h.activate();
  h.chatInput().props.onChange({ target: { value: '文字草稿' } }); await h.flush();
  assert.equal(h.chatInput().props.value, '文字草稿');
  assert.equal(h.handsFree.active, true, 'Typing must not permanently stop the continuous listener.');
});

test('submitting typed text keeps continuous listening available for the next utterance', async t => {
  const h = await mount(t);
  h.chatInput().props.onChange({ target: { value: '显式离线文字输入' } }); await h.flush();
  await h.connect(); await h.activate();
  h.composer().props.onSubmit({ preventDefault() {} }); await h.flush();
  assert.ok(h.socket.sent.some(message => message.type === 'input' && message.text === '显式离线文字输入'));
  assert.equal(h.handsFree.active, true, 'Text submission must preserve continuous intent and its listener.');
});

test('control websocket reconnect restores continuous intent after releasing the old listener', async t => {
  const h = await mount(t); await h.connect(); await h.activate();
  const starts = h.handsFree.starts.length;
  await h.reconnect();
  assert.equal(h.handsFree.starts.length, starts + 1, 'A fresh control connection must restore continuous intent.');
  h.handsFree.ready(); await h.flush();
  assert.equal(h.voiceButton().props['aria-pressed'], true);
});

test('control disconnect clears actual speech readiness while preserving the selected speech intent', async t => {
  const h = await mount(t); await h.connect(); await h.activate();
  const speechLabel = () => h.find(node => node.type === 'label' && hasClass(node, 'speech-toggle'));
  assert.ok(textContent(speechLabel()).includes('语音回复已就绪'));
  h.socket.close(); await h.flush();
  assert.equal(h.handsFree.active, false);
  assert.equal(h.speechToggle().props.checked, true);
  assert.equal(textContent(speechLabel()).includes('语音回复已就绪'), false, 'Offline audio must not retain a ready label.');
  assert.ok(textContent(speechLabel()).includes('等待启用'));
});

test('VAD onset stops audio while the server turn and device-query identity remain valid until final input', async t => {
  const h = await mount(t); await h.connect(); await h.activate();
  h.socket.message({ type: 'turn.started', turn_id: 'assistant-before-vad' });
  h.socket.message({ type: 'stream.delta', turn_id: 'assistant-before-vad', text: '已有片段' }); await h.flush();
  h.handsFree.capture('voice-vad', true); await h.flush();
  assert.ok(h.speech.cancellations.includes('voice_activity'));
  h.socket.message({ type: 'stream.delta', turn_id: 'assistant-before-vad', text: '后续片段' }); await h.flush();
  const log = h.find(node => node.props?.role === 'log');
  assert.ok(textContent(log).includes('已有片段后续片段'), 'VAD alone must not invalidate the server turn.');
  assert.equal(h.device.valid('assistant-before-vad'), true, 'VAD alone must not revoke the turn used by device queries.');
  h.socket.message({ type: 'cockpit.command', kind: 'query', command_id: 'query-after-vad', request_id: 'query-after-vad',
    session_id: 'app-voice-session', target_device: 'browser:app-voice-session', turn_id: 'assistant-before-vad',
    domain: 'media', action: 'get_status', arguments: {} });
  await h.flush();
  assert.equal(h.device.commands.length, 1);
  assert.equal(h.socket.sent.find(message => message.type === 'cockpit.receipt' && message.command_id === 'query-after-vad')?.ok, true);
});

test('continuous ASR final submits once with enabled TTS and leaves the microphone open', async t => {
  const h = await mount(t); await h.connect(); await h.activate();
  h.handsFree.capture('voice-final', true); await h.flush();
  await h.handsFree.final('显式离线最终识别', 'voice-final'); await h.flush();
  assert.equal(h.speech.enabled, true);
  assert.equal(h.socket.sent.filter(message => message.type === 'input' && message.text === '显式离线最终识别').length, 1);
  assert.equal(h.handsFree.submissions.length, 1);
  assert.equal(h.handsFree.active, true);
});

test('provider authentication failure does not retry continuously on error or unrelated rerenders', async t => {
  const h = await mount(t); await h.connect();
  assert.equal(h.handsFree.starts.length, 1, 'Default continuous intent should attempt once at a fresh connection.');
  h.handsFree.providerError('显式离线语音提供商鉴权失败'); await h.flush();
  assert.equal(h.handsFree.active, false);
  assert.ok(textContent(h.find(node => node.props?.role === 'alert')).includes('显式离线语音提供商鉴权失败'));
  const entry = h.find(node => hasClass(node, 'voice-entry'));
  assert.equal(textContent(elements(entry, node => node.type === 'button')[0]), '重试语音连接');
  h.chatInput().props.onChange({ target: { value: '错误之后的文字草稿' } }); await h.flush();
  assert.equal(h.handsFree.starts.length, 1, 'Authentication errors must not cause automatic retry loops.');
});

test('explicitly stopping continuous mode survives a later control reconnect', async t => {
  const h = await mount(t); await h.connect(); await h.activate();
  h.voiceButton().props.onClick(); await h.flush();
  assert.equal(h.handsFree.active, false);
  const starts = h.handsFree.starts.length;
  await h.reconnect();
  assert.equal(h.handsFree.starts.length, starts, 'Reconnect must preserve the user decision to stop continuous mode.');
  assert.equal(h.voiceButton().props['aria-pressed'], false);
});

test('stored opt-out prevents automatic voice and a new session preserves both explicit preferences', async t => {
  const preferences = { 'super-xd-voice-mode': false, 'super-xd-speech-replies': false };
  const h = await mount(t, { preferences }); await h.connect();
  assert.equal(h.handsFree.starts.length, 0);
  assert.equal(h.speechToggle().props.checked, false);
  assert.equal(h.voiceButton().props['aria-pressed'], false);
  assert.equal(sessionStorage.getItem('super-xd-session'), 'app-voice-session');
  h.find(node => node.type === 'button' && hasClass(node, 'new-session')).props.onClick();
  assert.equal(h.reloads, 1);
  assert.equal(sessionStorage.getItem('super-xd-session'), null);
  assert.equal(sessionStorage.getItem('super-xd-voice-mode'), 'false');
  assert.equal(sessionStorage.getItem('super-xd-speech-replies'), 'false');
});

test('blocked automatic audio activation keeps voice intent pending until an explicit retry becomes ready', async t => {
  const h = await mount(t, { audioActivationBlocked: true }); await h.connect();
  assert.equal(h.handsFree.starts.length, 1);
  assert.equal(h.handsFree.active, false);
  assert.equal(h.speech.enabled, false);
  assert.equal(h.speechToggle().props.checked, true, 'Browser activation failure must not erase the speech preference.');
  assert.equal(h.find(node => node.type === 'button' && hasClass(node, 'handsfree-toggle')).props['aria-pressed'], true);
  assert.equal(h.voiceButton().props['aria-pressed'], false);
  assert.equal(textContent(h.badge()).includes('持续监听中'), false);
  const entry = h.find(node => hasClass(node, 'voice-entry'));
  const retry = elements(entry, node => node.type === 'button')[0];
  assert.equal(textContent(retry), '进入语音对话');
  assert.equal(retry.props.disabled, false);
  h.audioActivationBlocked = false;
  retry.props.onClick(); await h.flush();
  assert.equal(h.handsFree.starts.length, 2);
  assert.equal(h.voiceButton().props['aria-pressed'], false, 'Retry startup still waits for the real listening phase.');
  h.handsFree.ready(); await h.flush();
  assert.equal(h.voiceButton().props['aria-pressed'], true);
  assert.equal(h.speech.enabled, true);
});
