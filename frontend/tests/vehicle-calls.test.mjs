// UI-only views and receipts; no API, model, or browser requests.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const source = fs.readFileSync(new URL('../src/vehicleCallViews.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { snapshotVehicleCalls, liveVehicleCall, mergeVehicleCalls, bindVehicleCommand, applyVehicleReceipt, vehicleCallStatus } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const action = { id: 'a1', intent_id: 'i1', name: 'set_mirror_heating', arguments: { enabled: true } };
const plan = (parent = 'parent', extra = {}) => ({ parent_call_id: parent, plan: { actions: [action], ok: true, simulation: { ok: true, executed: false } }, ...extra });

test('cold Pro action survives an empty native snapshot without inventing execution', () => {
  const turn = { vehicle_calls: [], vehicle_plans: [plan()], finished: true };
  const before = JSON.stringify(turn), calls = snapshotVehicleCalls(turn);
  assert.equal(calls.length, 1); assert.equal(calls[0].id, 'parent:a1');
  assert.equal(calls[0].source, 'vehicle_plan'); assert.equal(calls[0].receipt, undefined);
  assert.equal(vehicleCallStatus(calls[0]), '已规划 / 待回执');
  assert.deepEqual(JSON.parse(calls[0].function.arguments), { enabled: true });
  assert.equal(JSON.stringify(turn), before);
});

test('live top-level provenance and snapshot action deduplicate by full parent/action ID', () => {
  const live = liveVehicleCall({ call: { id: 'parent:a1', function: { name: action.name, arguments: '{"enabled":true}' } }, source: 'vehicle_plan', parent_call_id: 'parent', action_id: 'a1', intent_id: 'i1' });
  assert.equal(live.source, 'vehicle_plan');
  const calls = snapshotVehicleCalls({ vehicle_calls: [], vehicle_plans: [plan()] }, [live]);
  assert.equal(calls.length, 1); assert.equal(calls[0].parent_call_id, 'parent');
  assert.equal(snapshotVehicleCalls({ vehicle_plans: [plan('other')] }, calls).length, 2);
});

test('only per-action actual receipts establish success, independent of overall ledger flags', () => {
  const calls = snapshotVehicleCalls({ vehicle_plans: [plan('parent', { ledger: { ok: true, executed: true }, action_receipts: [] })] });
  assert.equal(vehicleCallStatus(calls[0]), '已规划 / 待回执');
  const recorded = snapshotVehicleCalls({ vehicle_plans: [plan('parent', { action_receipts: [{ action_id: 'a1', status: 'completed', result: { ok: true, value: true } }] })] });
  assert.equal(vehicleCallStatus(recorded[0]), '回执成功');
  const incomplete = snapshotVehicleCalls({ vehicle_plans: [plan('parent', { action_receipts: [{ action_id: 'a1', status: 'completed' }] })] });
  assert.equal(vehicleCallStatus(incomplete[0]), '回执未确认');
});

test('partial failed, blocked and interrupted actions remain distinct and are not called executed', () => {
  for (const [status, label] of [['failed', '回执失败'], ['blocked', '依赖阻断 / 未执行'], ['interrupted', '已打断 / 未执行']]) {
    const calls = snapshotVehicleCalls({ vehicle_plans: [plan('parent', { action_receipts: [{ action_id: 'a1', status, result: { ok: false, executed: false } }] })] });
    assert.equal(vehicleCallStatus(calls[0]), label);
  }
});

test('receipt lookup stays in its own parent plan and uses ledger only when direct receipts are missing', () => {
  const receipt = { action_id: 'a1', status: 'completed', result: { ok: true } };
  const calls = snapshotVehicleCalls({ vehicle_plans: [plan('one', { ledger: { action_receipts: [receipt] } }), plan('two', { action_receipts: [], ledger: { action_receipts: [receipt] } })] });
  assert.equal(vehicleCallStatus(calls[0]), '回执成功');
  assert.equal(vehicleCallStatus(calls[1]), '已规划 / 待回执');
});

test('native calls and legacy snapshots preserve identities without fabricating a Pro source', () => {
  const native = { id: 'hot-call', function: { name: 'set_volume', arguments: '{"volume":30}' } };
  assert.deepEqual(snapshotVehicleCalls({ vehicle_calls: [native] }), [{ ...native, receipt: undefined }]);
  assert.equal(snapshotVehicleCalls({}, [native])[0].source, undefined);
  assert.deepEqual(snapshotVehicleCalls({ lookup_results: [{ call_id: 'maps', result: {} }] }), []);
});

test('real command identity and result update only the matching Pro action', () => {
  const calls = snapshotVehicleCalls({ vehicle_plans: [plan('one'), plan('two')] });
  const bound = bindVehicleCommand(calls, { command_id: 'command', parent_call_id: 'two', action_id: 'a1' });
  assert.equal(bound[0].command_id, undefined); assert.equal(bound[1].command_id, 'command');
  assert.equal(vehicleCallStatus(bound[1]), '已规划 / 待回执');
  const done = applyVehicleReceipt(bound, 'command', { ok: true, executed: true });
  assert.equal(vehicleCallStatus(done[0]), '已规划 / 待回执'); assert.equal(vehicleCallStatus(done[1]), '回执成功');
  const restored = snapshotVehicleCalls({ vehicle_calls: [], vehicle_plans: [plan('one'), plan('two')] }, done);
  assert.equal(vehicleCallStatus(restored[1]), '回执成功');
});

test('merge retains an actual live receipt when a later partial view lacks one', () => {
  const calls = snapshotVehicleCalls({ vehicle_plans: [plan()] });
  const live = applyVehicleReceipt(bindVehicleCommand(calls, { command_id: 'c', parent_call_id: 'parent', action_id: 'a1' }), 'c', { ok: false, reason: 'rejected' });
  assert.equal(mergeVehicleCalls(live, calls).length, 1);
  assert.equal(vehicleCallStatus(mergeVehicleCalls(live, calls)[0]), '回执失败');
});
