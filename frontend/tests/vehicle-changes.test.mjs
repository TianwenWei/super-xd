import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../src/vehicleChanges.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { diffVehicleState, vehiclePathKey, mergeVehicleChanges } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('receipt state changes identify only changed leaves, including zero and false', () => {
  const before = { climate: { power: false, temperatures: { driver: 22, passenger: 22 } }, windows: { front_left: 0 } };
  const after = { climate: { power: true, temperatures: { driver: 23, passenger: 22 } }, windows: { front_left: 30 } };
  assert.deepEqual(diffVehicleState(before, after), [
    { path: ['climate', 'power'], before: false, after: true },
    { path: ['climate', 'temperatures', 'driver'], before: 22, after: 23 },
    { path: ['windows', 'front_left'], before: 0, after: 30 },
  ]);
  assert.equal(before.climate.temperatures.driver, 22);
});

test('duplicate state and reordered object keys do not signal a new change', () => {
  const before = { navigation: { waypoints: [{ name: '巴黎', id: 'one' }] }, climate: { power: true } };
  const after = { climate: { power: true }, navigation: { waypoints: [{ id: 'one', name: '巴黎' }] } };
  assert.deepEqual(diffVehicleState(before, after), []);
});

test('vehicle configuration updates are tracked like vehicle tool updates', () => {
  assert.deepEqual(diffVehicleState({ _meta: { gear: 'P', speed_kph: 0 } }, { _meta: { gear: 'D', speed_kph: 25 } }), [
    { path: ['_meta', 'gear'], before: 'P', after: 'D' },
    { path: ['_meta', 'speed_kph'], before: 0, after: 25 },
  ]);
});

test('array order, added fields and removed fields remain real structural changes', () => {
  assert.deepEqual(diffVehicleState({ navigation: { waypoints: ['A', 'B'], destination: 'A' } }, { navigation: { waypoints: ['B', 'A'], active: true } }), [
    { path: ['navigation', 'waypoints'], before: ['A', 'B'], after: ['B', 'A'] },
    { path: ['navigation', 'destination'], before: 'A', after: undefined },
    { path: ['navigation', 'active'], before: undefined, after: true },
  ]);
});

test('rapid updates retain other changed fields and show the immediately preceding value', () => {
  const first = mergeVehicleChanges(new Map(), diffVehicleState({ media: { volume: 10 }, climate: { power: false } }, { media: { volume: 20 }, climate: { power: true } }), 1);
  const second = mergeVehicleChanges(first, diffVehicleState({ media: { volume: 20 } }, { media: { volume: 10 } }), 2);
  assert.equal(second.size, 2);
  assert.deepEqual(second.get(vehiclePathKey(['media', 'volume'])), { path: ['media', 'volume'], before: 20, after: 10, sequence: 2 });
  assert.equal(second.get(vehiclePathKey(['climate', 'power'])).sequence, 1);
  assert.equal(first.get(vehiclePathKey(['media', 'volume'])).after, 20);
});

test('path identities cannot collide when field names contain punctuation', () => {
  assert.notEqual(vehiclePathKey(['a.b', 'c']), vehiclePathKey(['a', 'b.c']));
});
