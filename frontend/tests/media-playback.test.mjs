import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const source = fs.readFileSync(new URL('../src/MediaPlayback.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { MediaPlayback, playableMediaUrl } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
class Media extends EventTarget {
  paused = true; readyState = 4; ended = false; duration = 60; position = 0; volume = 1; src = ''; error = null; plays = 0; reject = false; deferred = null;
  get currentTime() { return this.position; }
  set currentTime(value) { this.position = value; queueMicrotask(() => this.dispatchEvent(new Event('seeked'))); }
  play() {
    this.plays++;
    if (this.reject) return Promise.reject(new DOMException('User gesture required', 'NotAllowedError'));
    if (this.deferred) return this.deferred.promise.then(() => { this.paused = false; this.dispatchEvent(new Event('playing')); });
    this.paused = false; queueMicrotask(() => this.dispatchEvent(new Event('playing'))); return Promise.resolve();
  }
  pause() { if (!this.paused) { this.paused = true; this.dispatchEvent(new Event('pause')); } }
  load() {} removeAttribute(name) { if (name === 'src') this.src = ''; }
}
function setup() {
  let valid = true; const events = [], audio = new Media(), video = new Media();
  const engine = new MediaPlayback({ changed: (state,version,event) => events.push({state,version,event}), valid: () => valid }, 100);
  engine.attach(audio,video);
  let index = 0;
  return {engine,audio,video,events,invalidate:()=>{valid=false;engine.invalidate('turn')}, command:(action,args={})=>({type:'cockpit.command',command_id:`cmd-${++index}`,turn_id:'turn',domain:'media',action,arguments:args,expected_state_version:engine.stateVersion})};
}
const content = {content_id:'music-a',kind:'music',title:'Local track',source_url:'/api/media/files/music-a'};
const tick = () => new Promise(resolve => setImmediate(resolve));
test('only media URLs and rooted media file endpoints are playable',()=>{
  assert.equal(playableMediaUrl('/api/media/files/track?v=1'),'/api/media/files/track?v=1');
  assert.equal(playableMediaUrl('https://example.com/radio.mp3'),'https://example.com/radio.mp3');
  for(const value of ['javascript:alert(1)','file:///music.mp3','//evil.test/api/media/files/a','/api/media/files/../sessions','https://user:pass@example.com/a',undefined]) assert.equal(playableMediaUrl(value),undefined);
});
test('real play promise and media playing event precede success receipt; duplicates do not replay',async()=>{
  const h=setup(),command=h.command('play',content); const receipt=await h.engine.execute(command);
  assert.equal(receipt.ok,true); assert.equal(receipt.state.status,'playing'); assert.equal(receipt.state_version,1);
  assert.deepEqual(await h.engine.execute(command),receipt); assert.equal(h.audio.plays,1); h.engine.dispose();
});
test('the player owns the explicitly supplied queue and reports it independently of command expectations',async()=>{
  const h=setup(),queue=[{content_id:'music-a',kind:'music',title:'A',provider:'local'}, {content_id:'music-b',kind:'music',title:'B',provider:'local'}];
  const command=h.command('play',{...content,provider:'local',queue});command.expected={queue:[{content_id:'invented'}]};
  const first=await h.engine.execute(command);assert.equal(first.ok,true);assert.deepEqual(first.state.queue,queue);assert.equal(first.state.provider,'local');
  queue[0].title='mutated caller';first.state.queue[1].title='mutated receipt';
  assert.equal(h.engine.snapshot().queue[0].title,'A');assert.equal(h.engine.snapshot().queue[1].title,'B');
  const next=await h.engine.execute(h.command('next',{...content,content_id:'music-b',provider:'local',queue:h.engine.snapshot().queue}));
  assert.equal(next.state.content_id,'music-b');assert.equal(next.state.queue[1].title,'B');h.engine.dispose();
});
test('autoplay rejection fails honestly without success or state revision',async()=>{
  const h=setup();h.audio.reject=true; const receipt=await h.engine.execute(h.command('play',content));
  assert.equal(receipt.ok,false);assert.equal(receipt.error.code,'autoplay_blocked');assert.equal(receipt.state_version,0);assert.equal(h.audio.paused,true);h.engine.dispose();
});
test('seek, pause, resume, stop and exit report actual element state',async()=>{
  const h=setup(); await h.engine.execute(h.command('play',content));
  assert.equal((await h.engine.execute(h.command('seek',{position_seconds:12}))).state.position_seconds,12);
  assert.equal((await h.engine.execute(h.command('pause'))).state.status,'paused');
  assert.equal((await h.engine.execute(h.command('resume'))).state.status,'playing');
  const stopped=await h.engine.execute(h.command('stop'));assert.equal(stopped.state.status,'stopped');assert.equal(stopped.state.position_seconds,0);
  assert.equal((await h.engine.execute(h.command('exit'))).state.status,'closed');assert.equal(h.audio.src,'');h.engine.dispose();
});
test('live radio rejects seeking and FM cannot be reported as streamed audio',async()=>{
  const h=setup();const fm=await h.engine.execute(h.command('play',{kind:'radio',frequency_mhz:98.5}));assert.equal(fm.ok,false);assert.equal(h.audio.plays,0);
  await h.engine.execute(h.command('play',{...content,kind:'radio'}));h.audio.duration=Infinity;
  assert.equal((await h.engine.execute(h.command('seek',{position_seconds:10}))).ok,false);assert.equal(h.audio.paused,false);h.engine.dispose();
});
test('speech ducks music, microphone pauses it, focus release resumes only retained play intent',async()=>{
  const h=setup();await h.engine.execute(h.command('play',content));h.engine.setFocus('assistant',true);
  assert.equal(h.audio.paused,false);assert.ok(h.audio.volume<0.2);h.engine.setFocus('capture',true);assert.equal(h.audio.paused,true);
  h.engine.setFocus('capture',false);await tick();assert.equal(h.audio.paused,false);
  h.engine.setFocus('assistant',false);assert.equal(h.audio.volume,0.3);
  h.engine.setFocus('capture',true);await h.engine.execute(h.command('pause'));h.engine.setFocus('capture',false);await tick();assert.equal(h.audio.paused,true);h.engine.dispose();
});
test('video is paused for TTS and restored only when focus is released',async()=>{
  const h=setup();await h.engine.execute(h.command('play',{...content,kind:'video'}));h.engine.setFocus('assistant',true);assert.equal(h.video.paused,true);
  h.engine.setFocus('assistant',false);await tick();assert.equal(h.video.paused,false);h.engine.dispose();
});
test('late automatic resume cannot undo a later manual pause',async()=>{
  const h=setup();await h.engine.execute(h.command('play',content));h.engine.setFocus('capture',true);
  let resolve;h.audio.deferred={promise:new Promise(done=>{resolve=done})};h.engine.setFocus('capture',false);
  await h.engine.execute(h.command('pause'));resolve();await tick();assert.equal(h.audio.paused,true);assert.equal(h.engine.snapshot().desired_status,'paused');h.engine.dispose();
});
test('expired turn and state conflict reject before HTML media executes',async()=>{
  const h=setup();const old=h.command('play',content);old.expected_state_version=7;assert.equal((await h.engine.execute(old)).error.code,'state_conflict');
  h.invalidate();assert.equal((await h.engine.execute(h.command('play',content))).error.code,'stale_command');assert.equal(h.audio.plays,0);h.engine.dispose();
});
test('disconnect pauses playback and does not automatically resume on focus release',async()=>{
  const h=setup();await h.engine.execute(h.command('play',content));h.engine.setFocus('capture',true);h.engine.disconnect();h.engine.setFocus('capture',false);await tick();assert.equal(h.audio.paused,true);h.engine.dispose();
});

test('volume can be set before content exists and resumed playing is idempotent',async()=>{
  const h=setup();const idle=await h.engine.execute(h.command('volume',{volume:55}));assert.equal(idle.ok,true);assert.equal(idle.state.status,'closed');
  await h.engine.execute(h.command('play',content));assert.equal(h.audio.volume,0.55);
  const plays=h.audio.plays;assert.equal((await h.engine.execute(h.command('resume'))).ok,true);assert.equal(h.audio.plays,plays);h.engine.dispose();
});

test('expired playback deadline does not execute or acknowledge successful playback',async()=>{
  const h=setup();const c=h.command('play',content);c.deadline_at=new Date(Date.now()-1).toISOString();
  const receipt=await h.engine.execute(c);assert.equal(receipt.error.code,'device_timeout');assert.equal(h.audio.plays,0);h.engine.dispose();
});

test('an external browser pause is user intent and cannot be undone by a later focus transition',async()=>{
  const h=setup();await h.engine.execute(h.command('play',content));h.audio.pause();
  assert.equal(h.engine.snapshot().desired_status,'paused');h.engine.setFocus('assistant',true);h.engine.setFocus('assistant',false);await tick();assert.equal(h.audio.paused,true);h.engine.dispose();
});


test('natural media end reports an observed ended event and preserves stopped intent across speech focus',async()=>{
  const h=setup();await h.engine.execute(h.command('play',content));h.audio.paused=true;h.audio.ended=true;h.audio.position=60;
  h.audio.dispatchEvent(new Event('ended'));const observed=h.events.at(-1);
  assert.equal(observed.event,'ended');assert.equal(observed.version,1);assert.equal(observed.state.status,'stopped');assert.equal(observed.state.desired_status,'stopped');
  h.engine.setFocus('assistant',true);h.engine.setFocus('assistant',false);await tick();assert.equal(h.audio.plays,1);h.engine.dispose();
});
