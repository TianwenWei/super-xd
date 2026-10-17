import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const source=fs.readFileSync(new URL('../src/audioActivation.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {resumeAudioContext,AudioActivationError}=await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('audio resume is invoked synchronously in the initiating user gesture',async()=>{
  let called=0;
  const context={state:'suspended',resume(){called++;this.state='running';return Promise.resolve();}};
  const pending=resumeAudioContext(context); assert.equal(called,1); await pending;
});
test('blocked autoplay has a bounded explicit activation error, not false readiness',async()=>{
  let resolve;
  const context={state:'suspended',resume(){return new Promise(done=>{resolve=done;});}};
  await assert.rejects(resumeAudioContext(context,5),AudioActivationError);
  context.state='running'; resolve(); await Promise.resolve();
});
test('a completed resume that remains suspended is not considered unlocked',async()=>{
  await assert.rejects(resumeAudioContext({state:'suspended',resume:async()=>{}},5),AudioActivationError);
});
